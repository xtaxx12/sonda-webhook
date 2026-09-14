"""Tests de la página del panel (auto-refresco + gráficas)."""

import importlib

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def cliente(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("AUTH_TOKEN", "prueba")
    monkeypatch.delenv("MODBUS_TCP_PORT", raising=False)
    import main
    importlib.reload(main)
    with TestClient(main.app) as c:
        yield c


def test_home_tiene_graficas_y_autorefresco(cliente):
    html = cliente.get("/").text
    # Tres gráficas, una por variable — nunca doble eje.
    assert 'data-grafica="oxigeno_disuelto"' in html
    assert 'data-grafica="temperatura"' in html
    assert 'data-grafica="saturacion"' in html
    # El JS consulta la API periódicamente.
    assert "/api/readings" in html
    assert "setInterval" in html or "setTimeout" in html


def test_home_tiene_vista_de_tabla(cliente):
    # Vista de tabla accesible (regla de relieve del contraste).
    assert "<table" in cliente.get("/").text


def test_home_tiene_selector_de_rango(cliente):
    html = cliente.get("/").text
    assert 'data-rango="1"' in html
    assert 'data-rango="24"' in html


def test_readings_acepta_limite_de_24h_a_15s(cliente):
    # 24 h a una lectura cada 15 s = 5760 filas; el límite debe permitirlo.
    r = cliente.get("/api/readings?limit=6000")
    assert r.status_code == 200


# --- Dispositivos y mapa -----------------------------------------------------

def test_dispositivos_vacio(cliente):
    r = cliente.get("/api/dispositivos")
    assert r.status_code == 200
    assert r.json() == {"dispositivos": []}


def test_ubicar_dispositivo_requiere_token(cliente):
    r = cliente.put("/api/dispositivos/sonda-1", json={"lat": -2.19, "lng": -79.88})
    assert r.status_code == 401


def test_ubicar_y_listar_dispositivo(cliente):
    r = cliente.put(
        "/api/dispositivos/sonda-1?token=prueba",
        json={"lat": -2.19, "lng": -79.88, "descripcion": "Piscina 1"},
    )
    assert r.status_code == 200

    # Una lectura de ese dispositivo enriquece el listado.
    cliente.post(
        "/usr/webhook?token=prueba",
        json={"deviceName": "sonda-1", "Temperature": 26.9, "Dissolved_Oxygen": 7.4,
              "DO_Saturation": 94.5},
    )

    lista = cliente.get("/api/dispositivos").json()["dispositivos"]
    assert len(lista) == 1
    d = lista[0]
    assert d["nombre"] == "sonda-1"
    assert d["lat"] == pytest.approx(-2.19)
    assert d["lng"] == pytest.approx(-79.88)
    assert d["descripcion"] == "Piscina 1"
    assert d["ultima"]["temperatura"] == pytest.approx(26.9)


def test_dispositivo_con_lecturas_sin_ubicacion_aparece(cliente):
    cliente.post(
        "/usr/webhook?token=prueba",
        json={"deviceName": "sonda-sin-mapa", "Temperature": 25.0},
    )
    lista = cliente.get("/api/dispositivos").json()["dispositivos"]
    assert [d["nombre"] for d in lista] == ["sonda-sin-mapa"]
    assert lista[0]["lat"] is None


def test_home_incluye_mapa(cliente):
    html = cliente.get("/").text
    assert 'id="mapa"' in html
    assert "leaflet" in html.lower()


def test_home_tiene_boton_de_gps(cliente):
    html = cliente.get("/").text
    assert 'id="btn-gps"' in html
    assert "geolocation" in html


def test_quitar_dispositivo_requiere_token(cliente):
    cliente.put("/api/dispositivos/sonda-1?token=prueba", json={"lat": -2.19, "lng": -79.88})
    r = cliente.delete("/api/dispositivos/sonda-1")
    assert r.status_code == 401


def test_quitar_dispositivo_del_mapa(cliente):
    cliente.put("/api/dispositivos/sonda-1?token=prueba", json={"lat": -2.19, "lng": -79.88})
    r = cliente.delete("/api/dispositivos/sonda-1?token=prueba")
    assert r.status_code == 200
    # Sin lecturas y sin ubicación, ya no aparece en el listado.
    assert cliente.get("/api/dispositivos").json()["dispositivos"] == []


def test_quitar_conserva_las_lecturas(cliente):
    cliente.post("/usr/webhook?token=prueba", json={"deviceName": "sonda-1", "Temperature": 26.9})
    cliente.put("/api/dispositivos/sonda-1?token=prueba", json={"lat": -2.19, "lng": -79.88})
    cliente.delete("/api/dispositivos/sonda-1?token=prueba")
    lista = cliente.get("/api/dispositivos").json()["dispositivos"]
    # El dispositivo sigue (tiene lecturas) pero ya sin ubicación en el mapa.
    assert lista[0]["nombre"] == "sonda-1"
    assert lista[0]["lat"] is None
    assert lista[0]["ultima"]["temperatura"] == pytest.approx(26.9)
