"""Tests del motor de alarmas (alertas.py): umbrales, transiciones y anti-spam."""

import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone

import pytest

import alertas


@pytest.fixture()
def con():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    alertas.preparar_tablas(c)
    yield c
    c.close()


T0 = datetime(2026, 9, 14, 3, 0, tzinfo=timezone.utc)


def test_umbrales_por_defecto(con):
    u = alertas.umbrales_de(con, "piscina-1")
    assert u["od_aviso"] == pytest.approx(4.0)
    assert u["od_critico"] == pytest.approx(3.0)


def test_fijar_umbrales(con):
    alertas.fijar_umbrales(con, "piscina-1", od_aviso=5.0, od_critico=3.5)
    u = alertas.umbrales_de(con, "piscina-1")
    assert u["od_aviso"] == pytest.approx(5.0)
    assert u["od_critico"] == pytest.approx(3.5)


def test_od_normal_no_alarma(con):
    avisos = alertas.evaluar_lectura(con, "piscina-1", 6.5, T0)
    assert avisos == []
    assert alertas.alarmas_activas(con) == []


def test_od_bajo_dispara_una_vez(con):
    avisos = alertas.evaluar_lectura(con, "piscina-1", 3.6, T0)
    assert len(avisos) == 1 and "3.6" in avisos[0] and "piscina-1" in avisos[0]
    # Sigue bajo 2 minutos después: sin nuevo aviso (anti-spam).
    assert alertas.evaluar_lectura(con, "piscina-1", 3.5, T0 + timedelta(minutes=2)) == []
    assert len(alertas.alarmas_activas(con)) == 1


def test_reaviso_tras_30_minutos(con):
    alertas.evaluar_lectura(con, "piscina-1", 3.6, T0)
    avisos = alertas.evaluar_lectura(con, "piscina-1", 3.4, T0 + timedelta(minutes=31))
    assert len(avisos) == 1


def test_escala_a_critico(con):
    alertas.evaluar_lectura(con, "piscina-1", 3.6, T0)
    avisos = alertas.evaluar_lectura(con, "piscina-1", 2.7, T0 + timedelta(minutes=5))
    assert len(avisos) == 1 and "CRÍTICO" in avisos[0].upper()
    activas = alertas.alarmas_activas(con)
    assert len(activas) == 1 and activas[0]["tipo"] == "od_critico"


def test_recuperacion_resuelve_y_avisa(con):
    alertas.evaluar_lectura(con, "piscina-1", 3.6, T0)
    avisos = alertas.evaluar_lectura(con, "piscina-1", 5.2, T0 + timedelta(minutes=20))
    assert len(avisos) == 1 and "recuper" in avisos[0].lower()
    assert alertas.alarmas_activas(con) == []
    # Y queda en el historial, resuelta.
    hist = alertas.historial(con, 10)
    assert len(hist) == 1 and hist[0]["resuelta_en"] is not None


def test_piscinas_independientes(con):
    alertas.evaluar_lectura(con, "piscina-1", 3.6, T0)
    assert alertas.evaluar_lectura(con, "piscina-2", 6.0, T0) == []
    assert len(alertas.alarmas_activas(con)) == 1


def test_sonda_muda(con):
    alertas.evaluar_lectura(con, "piscina-1", 6.0, T0)
    alertas.registrar_ultima(con, "piscina-1", T0)
    avisos = alertas.revisar_mudas(con, T0 + timedelta(minutes=12), limite_min=10)
    assert len(avisos) == 1 and "sin datos" in avisos[0].lower()
    # No repite enseguida.
    assert alertas.revisar_mudas(con, T0 + timedelta(minutes=13), limite_min=10) == []
    # Una lectura nueva la resuelve.
    avisos = alertas.evaluar_lectura(con, "piscina-1", 6.0, T0 + timedelta(minutes=20))
    assert any("volvió" in a or "recuper" in a.lower() for a in avisos)
    assert alertas.alarmas_activas(con) == []


def test_telegram_sin_configurar_no_hace_nada():
    assert alertas.notificar_telegram("hola", token="", chat_id="") is False


def test_telegram_envia(monkeypatch):
    import http.server
    recibidos = []

    class Falso(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            cuerpo = self.rfile.read(int(self.headers["Content-Length"]))
            recibidos.append((self.path, json.loads(cuerpo)))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"ok":true}')

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), Falso)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    ok = alertas.notificar_telegram(
        "⚠ prueba", token="tok123", chat_id="999",
        url_base=f"http://127.0.0.1:{srv.server_address[1]}",
    )
    srv.shutdown()
    assert ok is True
    path, cuerpo = recibidos[0]
    assert "bottok123" in path and path.endswith("/sendMessage")
    assert cuerpo["chat_id"] == "999"
    assert cuerpo["text"] == "⚠ prueba"
