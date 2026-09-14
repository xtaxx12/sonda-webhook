"""
Motor de alarmas de la sonda: umbrales por piscina, transiciones y anti-spam.

Reglas:
  - od < od_critico  -> alarma crítica (por defecto 3.0 mg/L)
  - od < od_aviso    -> alarma de aviso (por defecto 4.0 mg/L)
  - Se notifica al ENTRAR en alarma, se repite cada REAVISO_MIN mientras siga,
    y se notifica la recuperación al salir.
  - Un módulo que deja de reportar dispara la alarma "sin_datos"
    (revisar_mudas se llama periódicamente desde el servidor).

Los envíos van por la API de Telegram (notificar_telegram), sin dependencias.
"""

import json
import os
import urllib.error
import urllib.request
from datetime import datetime
from typing import List, Optional

REAVISO_MIN = 30.0          # minutos entre repeticiones de una alarma activa
OD_AVISO_DEFECTO = 4.0      # mg/L: estrés en camarón
OD_CRITICO_DEFECTO = 3.0    # mg/L: peligro


# --- Tablas ------------------------------------------------------------------

def preparar_tablas(con) -> None:
    con.execute("""
        CREATE TABLE IF NOT EXISTS umbrales (
            dispositivo TEXT PRIMARY KEY,
            od_aviso    REAL NOT NULL,
            od_critico  REAL NOT NULL
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS alarmas (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            dispositivo  TEXT NOT NULL,
            tipo         TEXT NOT NULL,   -- od_bajo | od_critico | sin_datos
            valor        REAL,
            iniciada_en  TEXT NOT NULL,
            ultima_notif TEXT NOT NULL,
            resuelta_en  TEXT
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS ultimas_vistas (
            dispositivo TEXT PRIMARY KEY,
            visto_en    TEXT NOT NULL
        )
    """)
    con.commit()


# --- Umbrales ----------------------------------------------------------------

def umbrales_de(con, dispositivo: str) -> dict:
    fila = con.execute(
        "SELECT od_aviso, od_critico FROM umbrales WHERE dispositivo = ?", (dispositivo,)
    ).fetchone()
    if fila:
        return {"od_aviso": fila["od_aviso"], "od_critico": fila["od_critico"]}
    return {"od_aviso": OD_AVISO_DEFECTO, "od_critico": OD_CRITICO_DEFECTO}


def fijar_umbrales(con, dispositivo: str, od_aviso: float, od_critico: float) -> None:
    con.execute(
        """INSERT INTO umbrales (dispositivo, od_aviso, od_critico) VALUES (?, ?, ?)
           ON CONFLICT(dispositivo) DO UPDATE SET od_aviso=excluded.od_aviso,
                                                  od_critico=excluded.od_critico""",
        (dispositivo, od_aviso, od_critico),
    )
    con.commit()


# --- Consultas ---------------------------------------------------------------

def alarmas_activas(con) -> list:
    return [dict(f) for f in con.execute(
        "SELECT * FROM alarmas WHERE resuelta_en IS NULL ORDER BY id DESC")]


def historial(con, limite: int = 50) -> list:
    return [dict(f) for f in con.execute(
        "SELECT * FROM alarmas ORDER BY id DESC LIMIT ?", (limite,))]


def registrar_ultima(con, dispositivo: str, ahora: datetime) -> None:
    con.execute(
        """INSERT INTO ultimas_vistas (dispositivo, visto_en) VALUES (?, ?)
           ON CONFLICT(dispositivo) DO UPDATE SET visto_en=excluded.visto_en""",
        (dispositivo, ahora.isoformat()),
    )
    con.commit()


def _activa(con, dispositivo: str, tipos: tuple):
    marcas = ",".join("?" * len(tipos))
    return con.execute(
        f"SELECT * FROM alarmas WHERE dispositivo = ? AND resuelta_en IS NULL"
        f" AND tipo IN ({marcas}) ORDER BY id DESC LIMIT 1",
        (dispositivo, *tipos),
    ).fetchone()


def _abrir(con, dispositivo: str, tipo: str, valor, ahora: datetime) -> None:
    iso = ahora.isoformat()
    con.execute(
        "INSERT INTO alarmas (dispositivo, tipo, valor, iniciada_en, ultima_notif) VALUES (?, ?, ?, ?, ?)",
        (dispositivo, tipo, valor, iso, iso),
    )
    con.commit()


def _resolver(con, id_alarma: int, ahora: datetime) -> None:
    con.execute("UPDATE alarmas SET resuelta_en = ? WHERE id = ?", (ahora.isoformat(), id_alarma))
    con.commit()


def _toca_reavisar(fila, ahora: datetime) -> bool:
    ultima = datetime.fromisoformat(fila["ultima_notif"])
    return (ahora - ultima).total_seconds() >= REAVISO_MIN * 60


def _mensaje_od(tipo: str, dispositivo: str, od: float, u: dict) -> str:
    if tipo == "od_critico":
        return f"🔴 CRÍTICO: oxígeno {od:.1f} mg/L en {dispositivo} (umbral {u['od_critico']:g})"
    return f"🟠 Oxígeno bajo en {dispositivo}: {od:.1f} mg/L (umbral {u['od_aviso']:g})"


# --- Evaluación --------------------------------------------------------------

def evaluar_lectura(con, dispositivo: str, od: Optional[float], ahora: datetime) -> List[str]:
    """Procesa una lectura nueva. Devuelve los mensajes que hay que notificar."""
    avisos: List[str] = []

    # Una fila de fallo (od None) NO cuenta como señal de vida de la sonda:
    # el agente puede estar vivo con la sonda muerta, y eso debe alarmar igual.
    if od is None:
        return avisos

    registrar_ultima(con, dispositivo, ahora)

    muda = _activa(con, dispositivo, ("sin_datos",))
    if muda:
        _resolver(con, muda["id"], ahora)
        avisos.append(f"✅ {dispositivo} volvió a reportar")

    u = umbrales_de(con, dispositivo)
    activa = _activa(con, dispositivo, ("od_bajo", "od_critico"))
    tipo = ("od_critico" if od < u["od_critico"]
            else "od_bajo" if od < u["od_aviso"]
            else None)

    if tipo is None:
        if activa:
            _resolver(con, activa["id"], ahora)
            avisos.append(f"✅ {dispositivo} recuperada: oxígeno {od:.1f} mg/L")
        return avisos

    if not activa:
        _abrir(con, dispositivo, tipo, od, ahora)
        avisos.append(_mensaje_od(tipo, dispositivo, od, u))
    elif tipo == "od_critico" and activa["tipo"] == "od_bajo":
        con.execute("UPDATE alarmas SET tipo=?, valor=?, ultima_notif=? WHERE id=?",
                    (tipo, od, ahora.isoformat(), activa["id"]))
        con.commit()
        avisos.append(_mensaje_od(tipo, dispositivo, od, u))
    else:
        con.execute("UPDATE alarmas SET valor=? WHERE id=?", (od, activa["id"]))
        if _toca_reavisar(activa, ahora):
            con.execute("UPDATE alarmas SET ultima_notif=? WHERE id=?",
                        (ahora.isoformat(), activa["id"]))
            avisos.append(_mensaje_od(activa["tipo"] if tipo == "od_bajo" else tipo,
                                      dispositivo, od, u))
        con.commit()

    return avisos


def revisar_mudas(con, ahora: datetime, limite_min: float = 10.0) -> List[str]:
    """Abre la alarma 'sin_datos' para los módulos que llevan demasiado callados."""
    avisos: List[str] = []
    for fila in con.execute("SELECT dispositivo, visto_en FROM ultimas_vistas").fetchall():
        visto = datetime.fromisoformat(fila["visto_en"])
        minutos = (ahora - visto).total_seconds() / 60
        if minutos < limite_min:
            continue
        texto = f"🔕 {fila['dispositivo']} sin datos desde hace {round(minutos)} min"
        activa = _activa(con, fila["dispositivo"], ("sin_datos",))
        if not activa:
            _abrir(con, fila["dispositivo"], "sin_datos", None, ahora)
            avisos.append(texto)
        elif _toca_reavisar(activa, ahora):
            con.execute("UPDATE alarmas SET ultima_notif=? WHERE id=?",
                        (ahora.isoformat(), activa["id"]))
            con.commit()
            avisos.append(texto)
    return avisos


# --- Telegram ----------------------------------------------------------------

def notificar_telegram(texto: str, token: Optional[str] = None,
                       chat_id: Optional[str] = None,
                       url_base: Optional[str] = None) -> bool:
    """Manda un mensaje por el bot de Telegram. False si no está configurado o falla."""
    token = token if token is not None else os.environ.get("TELEGRAM_TOKEN", "").strip()
    chat_id = chat_id if chat_id is not None else os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        return False
    url = f"{url_base or 'https://api.telegram.org'}/bot{token}/sendMessage"
    peticion = urllib.request.Request(
        url,
        data=json.dumps({"chat_id": chat_id, "text": texto}).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(peticion, timeout=10) as resp:
            return resp.status == 200
    except (urllib.error.URLError, OSError, TimeoutError):
        return False
