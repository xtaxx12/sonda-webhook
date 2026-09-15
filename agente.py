"""
Agente local: consulta la sonda por Modbus TCP y empuja al webhook por HTTPS.

Corre en cualquier máquina de la misma LAN que el módulo USR (configurado en
modo "Modbus TCP<=>Modbus RTU", Server, puerto 8899). Nada queda expuesto a
internet: el agente hace la consulta localmente y empuja hacia afuera.

Solo librería estándar — no requiere pip install.

    SONDA_HOST=192.168.3.157 SONDA_PORT=8899 \
    WEBHOOK_URL=https://TU-APP.fly.dev/usr/webhook WEBHOOK_TOKEN=... \
    INTERVALO=15 python3 agente.py

Si el webhook no responde (se cayó el internet), las lecturas quedan en un
buffer SQLite local y se reenvían cuando vuelva la conexión.

Detalles del protocolo (verificados contra el hardware real):
  - Petición:  MBAP + esclavo 1, función 03, 6 registros desde 0x0000
  - Floats en DCBA (little endian) — con otro orden salen números astronómicos
  - Silencio de la sonda = no se guarda nada (nunca un cero falso)
  - Cada lectura se valida con Benson-Krause: la saturación reportada debe
    cuadrar con OD/temperatura, o se avisa (byte order malo, trama corrupta
    o sonda descalibrada).
"""

import json
import math
import os
import socket
import sqlite3
import struct
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

# --- Protocolo Modbus TCP ----------------------------------------------------

def construir_peticion(esclavo: int = 1, inicio: int = 0, cantidad: int = 6) -> bytes:
    """MBAP (transacción 1, protocolo 0, largo 6) + PDU de lectura fc 03."""
    return struct.pack(">HHHBBHH", 1, 0, 6, esclavo, 0x03, inicio, cantidad)


def parsear_respuesta(datos: bytes):
    """
    Extrae los tres floats de una respuesta Modbus TCP. Byte order DCBA.
    Devuelve None si la trama es corta, es una excepción Modbus o no es fc 03.
    """
    if len(datos) < 21 or datos[7] != 0x03 or datos[8] != 12:
        return None
    od, temp, sat = struct.unpack("<fff", datos[9:21])
    return {"oxigeno_disuelto": od, "temperatura": temp, "saturacion": sat}


def leer_sonda(host: str, puerto: int, timeout: float = 6.0, esclavo: int = 1) -> dict:
    """
    Consulta la sonda una vez. Lanza TimeoutError si no contesta
    (el equivalente al err:1 de USR) y ValueError si la trama no es válida.

    `esclavo` es la dirección Modbus de la sonda (1 por defecto). Si la sonda
    de una piscina está configurada con otra dirección, el módulo conecta pero
    la sonda ignora las preguntas dirigidas a otro esclavo -> timeout.
    """
    with socket.create_connection((host, puerto), timeout=timeout) as s:
        s.settimeout(timeout)
        s.sendall(construir_peticion(esclavo=esclavo))
        datos = b""
        while len(datos) < 21:
            trozo = s.recv(256)
            if not trozo:
                raise TimeoutError("la sonda cerró sin responder")
            datos += trozo
    campos = parsear_respuesta(datos)
    if campos is None:
        raise ValueError(f"trama inválida: {datos.hex(' ')}")
    return campos


# --- Coherencia física -------------------------------------------------------

def saturacion_teorica(temp_c: float) -> float:
    """Concentración de O2 a saturación (mg/L) según Benson-Krause, 1 atm."""
    t = temp_c + 273.15
    ln_c = (-139.34411
            + 1.575701e5 / t
            - 6.642308e7 / t**2
            + 1.243800e10 / t**3
            - 8.621949e11 / t**4)
    return math.exp(ln_c)


def coherencia(od: float, temp: float, sat: float, tolerancia: float = 5.0) -> bool:
    """
    ¿La saturación reportada cuadra con OD y temperatura?
    Detecta byte order equivocado, tramas corruptas o sonda descalibrada.
    """
    if not (0.0 <= od <= 60.0 and -5.0 <= temp <= 60.0 and 0.0 <= sat <= 300.0):
        return False
    sat_calculada = od / saturacion_teorica(temp) * 100.0
    return abs(sat_calculada - sat) <= tolerancia


# --- Buffer local ------------------------------------------------------------

def preparar_buffer(ruta: str) -> None:
    with sqlite3.connect(ruta) as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS cola (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                creado_en         TEXT NOT NULL,
                oxigeno_disuelto  REAL,
                temperatura       REAL,
                saturacion        REAL,
                enviado           INTEGER NOT NULL DEFAULT 0,
                error             TEXT
            )
        """)
        # Migración de buffers creados antes de que existiera `error`.
        columnas = [c[1] for c in con.execute("PRAGMA table_info(cola)")]
        if "error" not in columnas:
            con.execute("ALTER TABLE cola ADD COLUMN error TEXT")


def _redondear(v):
    # La sonda es float32 con resolución real de centésimas: más dígitos es ruido.
    return None if v is None else round(v, 2)


def encolar(ruta: str, campos, error: str = None) -> None:
    """Encola una lectura, o un fallo (campos=None) con su motivo en `error`."""
    campos = campos or {}
    with sqlite3.connect(ruta) as con:
        con.execute(
            "INSERT INTO cola (creado_en, oxigeno_disuelto, temperatura, saturacion, error)"
            " VALUES (?, ?, ?, ?, ?)",
            (
                datetime.now(timezone.utc).isoformat(),
                _redondear(campos.get("oxigeno_disuelto")),
                _redondear(campos.get("temperatura")),
                _redondear(campos.get("saturacion")),
                error,
            ),
        )


def pendientes(ruta: str) -> list:
    with sqlite3.connect(ruta) as con:
        return con.execute(
            "SELECT id, creado_en, oxigeno_disuelto, temperatura, saturacion, error"
            " FROM cola WHERE enviado = 0 ORDER BY id"
        ).fetchall()


def marcar_enviadas(ruta: str, ids: list) -> None:
    with sqlite3.connect(ruta) as con:
        con.executemany("UPDATE cola SET enviado = 1 WHERE id = ?", [(i,) for i in ids])


# --- Envío al webhook --------------------------------------------------------

DISPOSITIVO = os.environ.get("SONDA_NOMBRE", "sonda-od-agente")


def enviar_pendientes(ruta: str, url: str, token: str, timeout: float = 5.0) -> int:
    """
    Empuja las lecturas pendientes al webhook, más antiguas primero.
    Se detiene al primer fallo (se reintentará en el siguiente ciclo).
    Devuelve cuántas se enviaron.
    """
    separador = "&" if "?" in url else "?"
    destino = f"{url}{separador}token={token}" if token else url
    enviadas = 0
    for fila in pendientes(ruta):
        id_, creado_en, od, temp, sat, error = fila
        mensaje = {"deviceName": DISPOSITIVO, "time": creado_en}
        if error:
            mensaje["error"] = error
        else:
            mensaje.update({"Dissolved_Oxygen": od, "Temperature": temp, "DO_Saturation": sat})
        cuerpo = json.dumps(mensaje).encode()
        peticion = urllib.request.Request(
            destino, data=cuerpo, headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(peticion, timeout=timeout) as resp:
                if resp.status != 200:
                    break
        except (urllib.error.URLError, OSError, TimeoutError):
            break
        marcar_enviadas(ruta, [id_])
        enviadas += 1
    return enviadas


# --- Bucle principal ---------------------------------------------------------

def bucle():
    host = os.environ.get("SONDA_HOST", "192.168.3.157")
    puerto = int(os.environ.get("SONDA_PORT", "8899"))
    esclavo = int(os.environ.get("SONDA_ESCLAVO", "1"))
    url = os.environ.get("WEBHOOK_URL", "").strip()
    token = os.environ.get("WEBHOOK_TOKEN", "").strip()
    intervalo = float(os.environ.get("INTERVALO", "15"))
    buffer_db = os.environ.get("BUFFER_DB", os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "agente_buffer.db"))

    if not url:
        sys.exit("Define WEBHOOK_URL (p. ej. https://TU-APP.fly.dev/usr/webhook)")

    preparar_buffer(buffer_db)
    print(f"agente: sonda {host}:{puerto} (esclavo {esclavo}), cada {intervalo:g}s -> {url}")
    print(f"agente: buffer en {buffer_db}")

    while True:
        marca = time.monotonic()
        try:
            campos = leer_sonda(host, puerto, esclavo=esclavo)
            if not coherencia(campos["oxigeno_disuelto"], campos["temperatura"],
                              campos["saturacion"]):
                print(f"agente: AVISO lectura incoherente {campos} — se guarda igual, "
                      "revisa byte order o calibración")
            encolar(buffer_db, campos)
            print(f"agente: OD={campos['oxigeno_disuelto']:.2f} mg/L "
                  f"T={campos['temperatura']:.2f} °C Sat={campos['saturacion']:.2f} %")
        except TimeoutError:
            print("agente: la sonda no respondió (¿A/B sueltos?)")
            encolar(buffer_db, None, error="sonda sin respuesta (timeout)")
        except (OSError, ValueError) as e:
            print(f"agente: error leyendo la sonda: {e}")
            encolar(buffer_db, None, error=str(e)[:200])

        n = len(pendientes(buffer_db))
        if n:
            enviadas = enviar_pendientes(buffer_db, url, token)
            if enviadas < n:
                print(f"agente: webhook inalcanzable, {n - enviadas} lectura(s) en buffer")

        time.sleep(max(0.0, intervalo - (time.monotonic() - marca)))


if __name__ == "__main__":
    try:
        bucle()
    except KeyboardInterrupt:
        pass
