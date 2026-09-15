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
    # Temperaturas realistas: dos lecturas seguidas separadas por 2 °C serían
    # la sonda fuera del agua, y el resumen las excluiría a propósito.
    _lectura(cliente, "piscina-1", 27.4, od=3.8)
    _lectura(cliente, "piscina-1", 27.6, od=6.2)
    j = cliente.get("/api/stats?dias=7&dispositivo=piscina-1").json()
    assert len(j["dias"]) == 1
    d = j["dias"][0]
    assert d["n"] == 2
    assert d["od_min"] == pytest.approx(3.8)
    assert d["od_max"] == pytest.approx(6.2)
    assert d["od_prom"] == pytest.approx(5.0)
    assert d["temp_min"] == pytest.approx(27.4)
    assert d["temp_max"] == pytest.approx(27.6)


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


def test_export_csv_neutraliza_formulas(cliente):
    # Un deviceName malicioso no debe llegar a Excel como fórmula ejecutable.
    cliente.post("/usr/webhook?token=prueba",
                 json={"deviceName": "=HYPERLINK(\"http://malo\")", "Temperature": 25.0})
    texto = cliente.get("/api/export.csv").text
    assert "'=HYPERLINK" in texto.replace('"', "")


# --- Endurecimiento ----------------------------------------------------------

def test_webhook_sin_auth_token_configurado_se_niega(tmp_path, monkeypatch):
    """Si el servidor arranca sin AUTH_TOKEN, el webhook no acepta nada."""
    import importlib
    monkeypatch.setenv("DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.delenv("AUTH_TOKEN", raising=False)
    import main
    importlib.reload(main)
    with TestClient(main.app) as c:
        r = c.post("/usr/webhook", json={"Temperature": 25.0})
    assert r.status_code == 503


def test_lectura_de_fallo_no_rompe_latest_ni_stats(cliente):
    _lectura(cliente, "piscina-1", 26.0, od=5.0)
    cliente.post("/usr/webhook?token=prueba",
                 json={"deviceName": "piscina-1", "error": "sonda sin respuesta"})
    assert cliente.get("/api/latest").json()["temperatura"] == pytest.approx(26.0)
    assert cliente.get("/api/stats?dias=7").json()["dias"][0]["n"] == 1


# --- Estado operativo: zona, tendencia y margen -------------------------------

def _sembrar(mod, serie, dispositivo="piscina-1"):
    """Inserta lecturas de OD espaciadas 2 min hacia atrás desde ahora."""
    from datetime import datetime, timezone, timedelta
    ahora = datetime.now(timezone.utc)
    with mod.db() as con:
        for i, od in enumerate(serie):
            t = (ahora - timedelta(minutes=2 * (len(serie) - 1 - i))).isoformat()
            con.execute(
                "INSERT INTO lecturas (recibido_en, medido_en, dispositivo,"
                " oxigeno_disuelto, temperatura, saturacion, payload)"
                " VALUES (?,?,?,?,?,?,?)",
                (t, t, dispositivo, od, 27.5, od / 7.8 * 100, "{}"))


def test_estado_sin_datos(cliente):
    assert cliente.get("/api/estado").json() == {"hay_datos": False}


def test_estado_clasifica_la_zona(cliente):
    import main
    _sembrar(main, [3.5] * 6)
    e = cliente.get("/api/estado").json()
    assert e["hay_datos"] is True
    # 3.5 está bajo el aviso (4.0) pero sobre el crítico (3.0)
    assert e["zona"] == "aviso"
    assert e["umbrales"] == {"od_aviso": 4.0, "od_critico": 3.0}


def test_estado_calcula_pendiente_y_margen(cliente):
    import main
    # 30 lecturas cada 2 min bajando 0.05 mg/L => -1.5 mg/L por hora
    _sembrar(main, [6.0 - i * 0.05 for i in range(30)])
    e = cliente.get("/api/estado").json()
    assert -1.6 < e["pendiente_od_hora"] < -1.4
    # de 4.55 al crítico 3.0, cayendo 1.5/h => ~1 h
    assert 0.8 < e["horas_a_critico"] < 1.4
    assert e["muestras_tendencia"] == 30


def test_estado_no_inventa_margen_si_el_oxigeno_sube(cliente):
    import main
    _sembrar(main, [4.0 + i * 0.05 for i in range(20)])
    e = cliente.get("/api/estado").json()
    assert e["pendiente_od_hora"] > 0
    # Subiendo no hay "llega al crítico en X": estimarlo sería inventar.
    assert e["horas_a_critico"] is None


def test_estado_respeta_umbrales_propios_de_la_piscina(cliente):
    import main
    _sembrar(main, [5.5] * 6)
    cliente.put("/api/umbrales/piscina-1?token=prueba",
                json={"od_aviso": 6.0, "od_critico": 5.0})
    e = cliente.get("/api/estado").json()
    # 5.5 es normal con los umbrales por defecto, pero aviso con los de esta piscina
    assert e["zona"] == "aviso"


def test_zona_od_unitaria():
    import main
    u = {"od_aviso": 4.0, "od_critico": 3.0}
    assert main._zona_od(5.0, u) == "normal"
    assert main._zona_od(3.5, u) == "aviso"
    assert main._zona_od(2.5, u) == "critico"
    assert main._zona_od(None, u) == "desconocida"


def test_pendiente_necesita_muestras_suficientes():
    import main
    from datetime import datetime, timezone, timedelta
    ahora = datetime.now(timezone.utc)
    pocas = [{"recibido_en": (ahora - timedelta(minutes=i)).isoformat(),
              "oxigeno_disuelto": 5.0} for i in range(3)]
    # Con tres puntos una pendiente sería ruido presentado como dato.
    assert main._pendiente_por_hora(pocas) is None


def test_panel_muestra_estado_y_bandas_de_umbral(cliente):
    html = cliente.get("/").text
    assert 'id="estado"' in html and 'id="tendencia"' in html
    assert "/api/estado" in html
    # Las bandas de umbral y la franja nocturna se dibujan en la gráfica de OD.
    assert "banda-crit" in html and "banda-aviso" in html
    assert "banda-noche" in html


# --- Vista de conjunto: varias piscinas --------------------------------------

def _sembrar_finca(mod):
    """Cinco piscinas en situaciones distintas, incluida una muda."""
    from datetime import datetime, timezone, timedelta
    ahora = datetime.now(timezone.utc)
    escenarios = {
        "p-sana":    [7.0] * 40,
        "p-aviso":   [3.6] * 40,
        "p-critica": [2.4] * 40,
        "p-cayendo": [6.0 - i * 0.05 for i in range(40)],
    }
    with mod.db() as con:
        for nombre, serie in escenarios.items():
            for i, od in enumerate(serie):
                t = (ahora - timedelta(minutes=len(serie) - 1 - i)).isoformat()
                con.execute(
                    "INSERT INTO lecturas (recibido_en, medido_en, dispositivo,"
                    " oxigeno_disuelto, temperatura, saturacion, payload)"
                    " VALUES (?,?,?,?,?,?,?)",
                    (t, t, nombre, od, 27.5, od / 7.8 * 100, "{}"))
        muda = (ahora - timedelta(hours=2)).isoformat()
        con.execute(
            "INSERT INTO lecturas (recibido_en, medido_en, dispositivo,"
            " oxigeno_disuelto, temperatura, saturacion, payload)"
            " VALUES (?,?,?,?,?,?,?)",
            (muda, muda, "p-muda", 6.5, 27.5, 83.0, "{}"))


def test_estados_ordena_por_urgencia(cliente):
    import main
    _sembrar_finca(main)
    zonas = [p["zona"] for p in cliente.get("/api/estados").json()["piscinas"]]
    # Primero lo que exige acción; en orden alfabético la que se muere
    # quedaría enterrada entre las sanas.
    assert zonas[0] == "critico"
    assert zonas[1] == "aviso"
    assert zonas[-1] == "normal"


def test_estados_resume_la_finca(cliente):
    import main
    _sembrar_finca(main)
    r = cliente.get("/api/estados").json()["resumen"]
    assert r == {"total": 5, "critico": 1, "aviso": 1, "sin_datos": 1, "normal": 2}


def test_piscina_muda_no_reporta_zona_por_su_ultimo_valor(cliente):
    import main
    _sembrar_finca(main)
    p = next(x for x in cliente.get("/api/estados").json()["piscinas"]
             if x["dispositivo"] == "p-muda")
    # Su última lectura fue 6.5 mg/L, pero fue hace dos horas: eso no es "normal".
    assert p["zona"] == "sin_datos"
    assert p["horas_a_critico"] is None


def test_estados_dentro_del_grupo_ordena_por_margen(cliente):
    import main
    _sembrar_finca(main)
    normales = [p for p in cliente.get("/api/estados").json()["piscinas"]
                if p["zona"] == "normal"]
    # La que está cayendo va antes que la estable, aunque ambas sean "normal".
    assert normales[0]["dispositivo"] == "p-cayendo"
    assert normales[0]["horas_a_critico"] is not None


def test_estados_trae_chispa_y_nombre_legible(cliente):
    import main
    _sembrar_finca(main)
    with main.db() as con:
        con.execute("INSERT INTO dispositivos (nombre, lat, lng, descripcion)"
                    " VALUES (?,?,?,?)", ("p-sana", None, None, "Piscina 1 — Norte"))
    p = next(x for x in cliente.get("/api/estados").json()["piscinas"]
             if x["dispositivo"] == "p-sana")
    assert p["nombre"] == "Piscina 1 — Norte"
    assert len(p["chispa"]) >= 2          # suficiente para dibujar la línea


def test_panel_trae_la_vista_de_conjunto(cliente):
    html = cliente.get("/").text
    assert 'id="rejilla"' in html and 'id="resumen-finca"' in html
    assert "/api/estados" in html
    # El color del marcador sale de la zona, no de la conectividad.
    assert "COLOR_ZONA" in html
    assert '"#008300"' not in html


# --- Registro de sondas desde la interfaz ------------------------------------

def test_registrar_sonda_sin_ubicacion(cliente):
    """Se puede dar de alta una sonda solo con nombre y descripción."""
    r = cliente.put("/api/dispositivos/piscina-3?token=prueba",
                    json={"descripcion": "Piscina nueva del fondo"})
    assert r.status_code == 200
    lista = cliente.get("/api/dispositivos").json()["dispositivos"]
    assert len(lista) == 1
    assert lista[0]["nombre"] == "piscina-3"
    assert lista[0]["descripcion"] == "Piscina nueva del fondo"
    assert lista[0]["lat"] is None and lista[0]["ultima"] is None


def test_actualizar_descripcion_conserva_ubicacion(cliente):
    cliente.put("/api/dispositivos/piscina-3?token=prueba",
                json={"lat": -2.19, "lng": -79.88, "descripcion": "vieja"})
    cliente.put("/api/dispositivos/piscina-3?token=prueba",
                json={"descripcion": "nueva"})
    d = cliente.get("/api/dispositivos").json()["dispositivos"][0]
    assert d["descripcion"] == "nueva"
    assert d["lat"] == pytest.approx(-2.19)


def test_lat_sin_lng_es_error(cliente):
    r = cliente.put("/api/dispositivos/piscina-3?token=prueba", json={"lat": -2.19})
    assert r.status_code == 422


def test_home_tiene_boton_registrar(cliente):
    assert 'id="btn-registrar"' in cliente.get("/").text


def test_home_tiene_mapa_satelital_con_selector(cliente):
    html = cliente.get("/").text
    assert "World_Imagery" in html            # tiles satelitales de Esri
    assert "control.layers" in html           # selector Satélite / Mapa
    assert "tile.openstreetmap.org" in html   # el mapa de calles sigue disponible


def test_home_tiene_salud_del_sistema_y_reloj(cliente):
    html = cliente.get("/").text
    assert 'id="salud"' in html      # sistema/agente en línea o sondas sin datos
    assert 'id="reloj"' in html      # hora local con zona horaria explícita
    assert "UTC" in html


# --- Una sola autoridad para la frescura --------------------------------------

def _sembrar_viejo(mod, minutos, od=6.5, dispositivo="piscina-1"):
    from datetime import datetime, timezone, timedelta
    t = (datetime.now(timezone.utc) - timedelta(minutes=minutos)).isoformat()
    with mod.db() as con:
        con.execute(
            "INSERT INTO lecturas (recibido_en, medido_en, dispositivo,"
            " oxigeno_disuelto, temperatura, saturacion, payload)"
            " VALUES (?,?,?,?,?,?,?)",
            (t, t, dispositivo, od, 27.5, 83.0, "{}"))


def test_estado_singular_tambien_marca_sin_datos(cliente):
    import main
    _sembrar_viejo(main, minutos=30)
    e = cliente.get("/api/estado").json()
    # Su última lectura fue 6.5 mg/L, pero fue hace media hora.
    assert e["zona"] == "sin_datos"


def test_los_dos_endpoints_coinciden_en_la_zona(cliente):
    import main
    _sembrar_viejo(main, minutos=30)
    uno = cliente.get("/api/estado").json()["zona"]
    todas = cliente.get("/api/estados").json()["piscinas"][0]["zona"]
    # Dos fuentes para el mismo hecho garantizan que un día discrepen.
    assert uno == todas == "sin_datos"


def test_no_se_estima_margen_sobre_datos_frios(cliente):
    import main
    from datetime import datetime, timezone, timedelta
    ahora = datetime.now(timezone.utc)
    # Serie descendente clara, pero toda ella de hace más de media hora.
    with main.db() as con:
        for i in range(30):
            t = (ahora - timedelta(minutes=90 - i)).isoformat()
            od = 6.0 - i * 0.05
            con.execute(
                "INSERT INTO lecturas (recibido_en, medido_en, dispositivo,"
                " oxigeno_disuelto, temperatura, saturacion, payload)"
                " VALUES (?,?,?,?,?,?,?)",
                (t, t, "piscina-1", od, 27.5, 70.0, "{}"))
    e = cliente.get("/api/estado").json()
    assert e["zona"] == "sin_datos"
    # Proyectar "llega al crítico en 1.2 h" desde datos viejos suena preciso
    # y no significa nada.
    assert e["horas_a_critico"] is None


def test_el_umbral_de_frescura_se_publica(cliente):
    import main
    _sembrar_viejo(main, minutos=1)
    assert cliente.get("/api/estado").json()["sin_datos_tras_segundos"] == main.SEGUNDOS_SIN_DATOS
    assert cliente.get("/api/estados").json()["sin_datos_tras_segundos"] == main.SEGUNDOS_SIN_DATOS


def test_el_panel_no_calcula_su_propio_umbral(cliente):
    html = cliente.get("/").text
    # El navegador lee la zona que decide el servidor; si vuelve a aparecer un
    # umbral propio, la cabecera y la franja discreparán otra vez.
    assert "edad_segundos > 90" not in html
    assert "edad_segundos > 180" not in html


# --- Manipulación de la sonda -------------------------------------------------

def _serie(mod, temps, od=7.0, dispositivo="p1", paso_min=1, desde_min=60):
    from datetime import datetime, timezone, timedelta
    ahora = datetime.now(timezone.utc)
    with mod.db() as con:
        for i, t_c in enumerate(temps):
            t = (ahora - timedelta(minutes=desde_min - i * paso_min)).isoformat()
            con.execute(
                "INSERT INTO lecturas (recibido_en, medido_en, dispositivo,"
                " oxigeno_disuelto, temperatura, saturacion, payload, manipulacion)"
                " VALUES (?,?,?,?,?,?,?,0)",
                (t, t, dispositivo, od, t_c, 90.0, "{}"))


def test_detecta_sonda_fuera_del_agua(cliente):
    import main
    from datetime import datetime, timezone, timedelta
    with main.db() as con:
        ahora = datetime.now(timezone.utc)
        con.execute(
            "INSERT INTO lecturas (recibido_en, medido_en, dispositivo,"
            " oxigeno_disuelto, temperatura, saturacion, payload, manipulacion)"
            " VALUES (?,?,?,?,?,?,?,0)",
            *[((ahora - timedelta(minutes=2)).isoformat(),
               (ahora - timedelta(minutes=2)).isoformat(),
               "p1", 7.0, 27.5, 90.0, "{}")])
        # Cinco grados en dos minutos: el agua de una piscina no hace eso.
        assert main._es_manipulacion(con, "p1", ahora, 32.5) is True


def test_el_ruido_del_sensor_no_cuenta_como_manipulacion(cliente):
    import main
    from datetime import datetime, timezone, timedelta
    with main.db() as con:
        ahora = datetime.now(timezone.utc)
        hace1s = (ahora - timedelta(seconds=1)).isoformat()
        con.execute(
            "INSERT INTO lecturas (recibido_en, medido_en, dispositivo,"
            " oxigeno_disuelto, temperatura, saturacion, payload, manipulacion)"
            " VALUES (?,?,?,?,?,?,?,0)",
            (hace1s, hace1s, "p1", 7.0, 27.50, 90.0, "{}"))
        # 0.05 °C en un segundo son 3 °C/min de pendiente, pero es ruido:
        # sin el mínimo absoluto, cada lectura rápida sería una falsa alarma.
        assert main._es_manipulacion(con, "p1", ahora, 27.55) is False


def test_el_resumen_diario_excluye_manipulaciones(cliente):
    import main
    _serie(main, [27.5] * 20, od=7.0)              # minutos 60..41
    _serie(main, [33.0, 31.0, 30.0], od=0.3, desde_min=40)   # contiguas
    import backfill_manipulacion as bf
    with main.db() as con:
        bf.marcar(con, main.GRADIENTE_MANIPULACION,
                  main.DELTA_MINIMO_MANIPULACION, main.VENTANA_MANIPULACION)
    d = cliente.get("/api/stats?dispositivo=p1").json()["dias"][0]
    # El 0.3 mg/L medido en aire no debe aparecer como mínimo del día.
    assert d["od_min"] == pytest.approx(7.0)
    assert d["temp_max"] == pytest.approx(27.5)


def test_manipulacion_no_dispara_alarma(cliente, monkeypatch):
    import main
    avisos = []
    monkeypatch.setattr(main.alertas, "notificar_telegram", lambda t: avisos.append(t))
    from datetime import datetime, timezone, timedelta
    ahora = datetime.now(timezone.utc)
    with main.db() as con:
        t = (ahora - timedelta(minutes=1)).isoformat()
        con.execute(
            "INSERT INTO lecturas (recibido_en, medido_en, dispositivo,"
            " oxigeno_disuelto, temperatura, saturacion, payload, manipulacion)"
            " VALUES (?,?,?,?,?,?,?,0)",
            (t, t, "p1", 7.0, 27.5, 90.0, "{}"))
    # Sonda fuera del agua: OD de 0.3 con un salto térmico de 5 °C.
    r = cliente.post("/usr/webhook?token=prueba", json={
        "deviceName": "p1", "Dissolved_Oxygen": 0.3,
        "Temperature": 32.5, "DO_Saturation": 3.0})
    assert r.status_code == 200
    # Sin esta supresión, sacar la sonda manda un "🔴 CRÍTICO" por Telegram.
    assert avisos == []


def test_la_grafica_sombrea_las_manipulaciones(cliente):
    html = cliente.get("/").text
    assert "banda-manip" in html
    assert "manipulacion" in html
