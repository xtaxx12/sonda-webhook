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


# --- Multi-sonda -------------------------------------------------------------

def _lectura(cliente, nombre, temp, od=5.0):
    cliente.post("/usr/webhook?token=prueba",
                 json={"deviceName": nombre, "Temperature": temp,
                       "Dissolved_Oxygen": od, "DO_Saturation": 60.0})


def test_latest_filtra_por_dispositivo(cliente):
    _lectura(cliente, "piscina-1", 26.0)
    _lectura(cliente, "piscina-2", 29.0)
    r = cliente.get("/api/latest?dispositivo=piscina-1").json()
    assert r["dispositivo"] == "piscina-1"
    assert r["temperatura"] == pytest.approx(26.0)
    # Sin filtro devuelve la más reciente de todas.
    assert cliente.get("/api/latest").json()["dispositivo"] == "piscina-2"


def test_readings_filtra_por_dispositivo(cliente):
    _lectura(cliente, "piscina-1", 26.0)
    _lectura(cliente, "piscina-2", 29.0)
    _lectura(cliente, "piscina-1", 26.5)
    j = cliente.get("/api/readings?dispositivo=piscina-1").json()
    assert j["total"] == 2
    assert all(l["dispositivo"] == "piscina-1" for l in j["lecturas"])


def test_home_tiene_selector_de_piscina(cliente):
    assert 'id="piscinas"' in cliente.get("/").text


# --- Alarmas integradas ------------------------------------------------------

def test_lectura_baja_crea_alarma(cliente):
    _lectura(cliente, "piscina-1", 27.0, od=3.2)
    j = cliente.get("/api/alarmas").json()
    assert len(j["activas"]) == 1
    assert j["activas"][0]["tipo"] == "od_bajo"
    assert j["activas"][0]["dispositivo"] == "piscina-1"


def test_lectura_normal_resuelve_alarma(cliente):
    _lectura(cliente, "piscina-1", 27.0, od=3.2)
    _lectura(cliente, "piscina-1", 27.0, od=5.5)
    j = cliente.get("/api/alarmas").json()
    assert j["activas"] == []
    assert len(j["historial"]) == 1


def test_umbrales_get_y_put(cliente):
    assert cliente.get("/api/umbrales/piscina-1").json()["od_aviso"] == pytest.approx(4.0)
    r = cliente.put("/api/umbrales/piscina-1", json={"od_aviso": 5.0, "od_critico": 3.5})
    assert r.status_code == 401
    r = cliente.put("/api/umbrales/piscina-1?token=prueba",
                    json={"od_aviso": 5.0, "od_critico": 3.5})
    assert r.status_code == 200
    assert cliente.get("/api/umbrales/piscina-1").json()["od_critico"] == pytest.approx(3.5)
    # Con el umbral subido a 5.0, una lectura de 4.5 ya dispara alarma.
    _lectura(cliente, "piscina-1", 27.0, od=4.5)
    assert len(cliente.get("/api/alarmas").json()["activas"]) == 1


def test_home_tiene_banner_y_umbrales(cliente):
    html = cliente.get("/").text
    assert 'id="alarmas"' in html
    assert 'id="btn-umbrales"' in html


# --- Estadísticas y exportación ----------------------------------------------

def test_stats_resumen_diario(cliente):
    _lectura(cliente, "piscina-1", 26.0, od=3.8)
    _lectura(cliente, "piscina-1", 28.0, od=6.2)
    j = cliente.get("/api/stats?dias=7&dispositivo=piscina-1").json()
    assert len(j["dias"]) == 1
    d = j["dias"][0]
    assert d["n"] == 2
    assert d["od_min"] == pytest.approx(3.8)
    assert d["od_max"] == pytest.approx(6.2)
    assert d["od_prom"] == pytest.approx(5.0)
    assert d["temp_min"] == pytest.approx(26.0)
    assert d["temp_max"] == pytest.approx(28.0)


def test_stats_filtra_dispositivo(cliente):
    _lectura(cliente, "piscina-1", 26.0, od=5.0)
    _lectura(cliente, "piscina-2", 30.0, od=7.0)
    j = cliente.get("/api/stats?dias=7&dispositivo=piscina-2").json()
    assert len(j["dias"]) == 1
    assert j["dias"][0]["od_min"] == pytest.approx(7.0)


def test_export_csv(cliente):
    _lectura(cliente, "piscina-1", 26.0, od=5.0)
    _lectura(cliente, "piscina-2", 30.0, od=7.0)
    r = cliente.get("/api/export.csv")
    assert r.status_code == 200
    assert "text/csv" in r.headers["content-type"]
    lineas = r.text.strip().splitlines()
    assert lineas[0].startswith("recibido_en,")
    assert len(lineas) == 3
    # Filtro por dispositivo
    r = cliente.get("/api/export.csv?dispositivo=piscina-1")
    assert len(r.text.strip().splitlines()) == 2
    assert "piscina-1" in r.text and "piscina-2" not in r.text


def test_home_tiene_resumen_y_csv(cliente):
    html = cliente.get("/").text
    assert 'id="resumen"' in html
    assert "/api/export.csv" in html
