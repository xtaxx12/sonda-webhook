"""
Tests del agente local (agente.py): consulta la sonda por Modbus TCP,
decodifica DCBA, valida coherencia física y empuja al webhook con buffer.

Vector de verdad: respuesta real capturada del módulo el 2026-09-14:
  00 01 00 00 00 0f 01 03 0c 7d 07 6a 40 f4 9a d1 41 76 77 36 42
  → OD 3.66 mg/L, Temp 26.20 °C, Sat 45.62 %
"""

import json
import socket
import sqlite3
import struct
import threading

import pytest

from agente import (
    coherencia,
    construir_peticion,
    encolar,
    enviar_pendientes,
    leer_sonda,
    marcar_enviadas,
    parsear_respuesta,
    pendientes,
    preparar_buffer,
    saturacion_teorica,
)

TRAMA_REAL = bytes.fromhex("000100000006010300000006")
RESPUESTA_REAL = bytes.fromhex("00010000000f01030c7d076a40f49ad141767736 42".replace(" ", ""))


# --- Protocolo Modbus TCP ----------------------------------------------------

def test_construir_peticion():
    assert construir_peticion() == TRAMA_REAL


def test_parsear_respuesta_real_dcba():
    campos = parsear_respuesta(RESPUESTA_REAL)
    assert campos is not None
    assert campos["oxigeno_disuelto"] == pytest.approx(3.66, abs=0.01)
    assert campos["temperatura"] == pytest.approx(26.20, abs=0.01)
    assert campos["saturacion"] == pytest.approx(45.62, abs=0.01)


def test_parsear_respuesta_corta():
    assert parsear_respuesta(RESPUESTA_REAL[:12]) is None


def test_parsear_respuesta_excepcion_modbus():
    # Función con bit de error (0x83) = excepción del esclavo.
    trama = bytes.fromhex("000100000003018302")
    assert parsear_respuesta(trama) is None


# --- Coherencia física (Benson-Krause) --------------------------------------

def test_saturacion_teorica_valores_de_tabla():
    assert saturacion_teorica(25.0) == pytest.approx(8.26, abs=0.03)
    assert saturacion_teorica(26.2) == pytest.approx(8.08, abs=0.03)


def test_coherencia_lectura_real():
    # 3.66 mg/L a 26.2 °C ≈ 45.3 % — la sonda dice 45.62: coherente.
    assert coherencia(3.66, 26.2, 45.62)


def test_coherencia_detecta_byte_order_malo():
    # Con byte order equivocado salen números astronómicos.
    assert not coherencia(3.1e20, 26.2, 45.62)
    assert not coherencia(3.66, 26.2, 99.9)


# --- Lectura por socket ------------------------------------------------------

def _modulo_falso(respuesta: bytes):
    """Servidor TCP de un solo uso que imita al módulo USR en modo Modbus TCP."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    puerto = srv.getsockname()[1]

    def atender():
        con, _ = srv.accept()
        con.recv(64)
        con.sendall(respuesta)
        con.close()
        srv.close()

    threading.Thread(target=atender, daemon=True).start()
    return puerto


def test_leer_sonda_extremo_a_extremo():
    puerto = _modulo_falso(RESPUESTA_REAL)
    campos = leer_sonda("127.0.0.1", puerto, timeout=3)
    assert campos["temperatura"] == pytest.approx(26.20, abs=0.01)


def test_leer_sonda_sin_respuesta():
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    puerto = srv.getsockname()[1]

    def atender():  # acepta pero calla, como sonda desconectada
        con, _ = srv.accept()
        con.recv(64)
        # no responde nada

    threading.Thread(target=atender, daemon=True).start()
    with pytest.raises(TimeoutError):
        leer_sonda("127.0.0.1", puerto, timeout=0.5)
    srv.close()


# --- Buffer local y envío ----------------------------------------------------

def test_buffer_encola_y_marca(tmp_path):
    ruta = str(tmp_path / "buffer.db")
    preparar_buffer(ruta)
    encolar(ruta, {"oxigeno_disuelto": 3.66, "temperatura": 26.2, "saturacion": 45.62})
    encolar(ruta, {"oxigeno_disuelto": 3.70, "temperatura": 26.3, "saturacion": 46.0})

    filas = pendientes(ruta)
    assert len(filas) == 2

    marcar_enviadas(ruta, [filas[0][0]])
    assert len(pendientes(ruta)) == 1


def test_enviar_pendientes_al_webhook(tmp_path):
    """Las lecturas encoladas llegan al webhook como JSON plano con el token."""
    import http.server

    recibidas = []

    class Receptor(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            cuerpo = self.rfile.read(int(self.headers["Content-Length"]))
            recibidas.append((self.path, json.loads(cuerpo)))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"ok":true}')

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), Receptor)
    puerto = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    ruta = str(tmp_path / "buffer.db")
    preparar_buffer(ruta)
    encolar(ruta, {"oxigeno_disuelto": 3.66, "temperatura": 26.2, "saturacion": 45.62})

    enviadas = enviar_pendientes(ruta, f"http://127.0.0.1:{puerto}/usr/webhook", "tok123")
    srv.shutdown()

    assert enviadas == 1
    assert pendientes(ruta) == []
    path, cuerpo = recibidas[0]
    assert "token=tok123" in path
    assert cuerpo["Dissolved_Oxygen"] == pytest.approx(3.66)
    assert cuerpo["Temperature"] == pytest.approx(26.2)
    assert cuerpo["DO_Saturation"] == pytest.approx(45.62)
    assert cuerpo["deviceName"]
    assert cuerpo["time"]


def test_enviar_pendientes_conserva_si_falla(tmp_path):
    """Si el webhook no responde, la lectura queda en el buffer para reintento."""
    ruta = str(tmp_path / "buffer.db")
    preparar_buffer(ruta)
    encolar(ruta, {"oxigeno_disuelto": 3.66, "temperatura": 26.2, "saturacion": 45.62})

    enviadas = enviar_pendientes(ruta, "http://127.0.0.1:1/usr/webhook", "tok", timeout=0.5)
    assert enviadas == 0
    assert len(pendientes(ruta)) == 1


# --- Registro de fallos y redondeo -------------------------------------------

def test_encolar_redondea_a_centesimas(tmp_path):
    ruta = str(tmp_path / "buffer.db")
    preparar_buffer(ruta)
    encolar(ruta, {"oxigeno_disuelto": 3.6567891, "temperatura": 26.294828, "saturacion": 45.51000213})
    fila = pendientes(ruta)[0]
    assert fila[2] == pytest.approx(3.66)
    assert fila[3] == pytest.approx(26.29)
    assert fila[4] == pytest.approx(45.51)


def test_fallo_queda_registrado_y_se_envia(tmp_path):
    """Cuando la sonda no responde, se guarda una fila con el motivo y llega al webhook."""
    import http.server

    recibidas = []

    class Receptor(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            cuerpo = self.rfile.read(int(self.headers["Content-Length"]))
            recibidas.append(json.loads(cuerpo))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"ok":true}')

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), Receptor)
    puerto = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    ruta = str(tmp_path / "buffer.db")
    preparar_buffer(ruta)
    encolar(ruta, None, error="sonda sin respuesta (timeout)")

    enviadas = enviar_pendientes(ruta, f"http://127.0.0.1:{puerto}/usr/webhook", "tok")
    srv.shutdown()

    assert enviadas == 1 and pendientes(ruta) == []
    cuerpo = recibidas[0]
    assert cuerpo["error"] == "sonda sin respuesta (timeout)"
    assert "Temperature" not in cuerpo
    assert cuerpo["deviceName"] and cuerpo["time"]
