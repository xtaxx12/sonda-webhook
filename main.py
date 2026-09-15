"""
Receptor de datos de la sonda de oxígeno disuelto / temperatura.

Recibe los POST que envía el Rule engine de USR Cloud, guarda cada lectura
en SQLite y las expone por HTTP para consultarlas o graficarlas.

Endpoints:
    GET  /                  Estado y última lectura (HTML legible)
    POST /usr/webhook       Lo que USR Cloud llama. Acepta cualquier JSON.
    GET  /api/latest        Última lectura (JSON)
    GET  /api/readings      Histórico (JSON), ?limit=N&since=ISO8601
    GET  /api/raw           Últimos payloads crudos, para depurar el formato
    GET  /health            Healthcheck para Fly

Variables de entorno:
    AUTH_TOKEN        Si la defines, exige ?token=... o header X-Auth-Token
                      en el webhook. Muy recomendable.
    DB_PATH           Ruta de la base. Por defecto /data/readings.db si existe
                      el directorio, si no ./readings.db
    MODBUS_TCP_PORT   Si la defines, abre además un socket TCP en ese puerto
                      para que el módulo USR se conecte directo (modo
                      transparente / doble socket) y sondea la sonda por
                      Modbus RTU sin pasar por USR Cloud.
    MODBUS_INTERVALO  Segundos entre sondeos Modbus (por defecto 60).
    MODBUS_REGISTRO   Si la defines, el paquete de registro del módulo debe
                      contener este texto (p. ej. su SN) o la conexión se
                      rechaza. Ponla en producción: el socket va sin cifrar.
"""

import asyncio
import json
import os
import re
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from typing import Any, Optional

import alertas
import modbus

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

# --- Configuración -----------------------------------------------------------

AUTH_TOKEN = os.environ.get("AUTH_TOKEN", "").strip()

def _default_db_path() -> str:
    if os.path.isdir("/data"):
        return "/data/readings.db"
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "readings.db")

DB_PATH = os.environ.get("DB_PATH", "").strip() or _default_db_path()

# Nombres que USR Cloud usa para cada variable, y su forma normalizada.
CAMPOS = {
    "dissolved_oxygen": "oxigeno_disuelto",
    "dissolvedoxygen": "oxigeno_disuelto",
    "do": "oxigeno_disuelto",
    "temperature": "temperatura",
    "temp": "temperatura",
    "do_saturation": "saturacion",
    "dosaturation": "saturacion",
    "saturation": "saturacion",
}

_lock = threading.Lock()
app = FastAPI(title="Sonda OD", docs_url="/docs")


# --- Base de datos -----------------------------------------------------------

@contextmanager
def db():
    con = sqlite3.connect(DB_PATH, timeout=10)
    con.row_factory = sqlite3.Row
    try:
        yield con
        con.commit()
    finally:
        con.close()


def init_db() -> None:
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    with db() as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS lecturas (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                recibido_en       TEXT NOT NULL,
                medido_en         TEXT,
                dispositivo       TEXT,
                oxigeno_disuelto  REAL,
                temperatura       REAL,
                saturacion        REAL,
                payload           TEXT NOT NULL
            )
        """)
        con.execute("CREATE INDEX IF NOT EXISTS ix_recibido ON lecturas(recibido_en)")
        cols = {f["name"] for f in con.execute("PRAGMA table_info(lecturas)")}
        if "manipulacion" not in cols:
            con.execute("ALTER TABLE lecturas ADD COLUMN manipulacion INTEGER DEFAULT 0")
        con.execute("""
            CREATE TABLE IF NOT EXISTS dispositivos (
                nombre      TEXT PRIMARY KEY,
                lat         REAL,
                lng         REAL,
                descripcion TEXT
            )
        """)
        alertas.preparar_tablas(con)


# --- Extracción de valores ---------------------------------------------------

def _num(valor: Any) -> Optional[float]:
    """Convierte a float lo que se pueda; None si no es un número."""
    if isinstance(valor, bool):
        return None
    if isinstance(valor, (int, float)):
        return float(valor)
    if isinstance(valor, str):
        m = re.search(r"-?\d+(?:\.\d+)?", valor)
        if m:
            try:
                return float(m.group())
            except ValueError:
                return None
    return None


def _aplanar(obj: Any, prefijo: str = "") -> dict:
    """Aplana un JSON anidado a un dict plano de clave -> valor."""
    plano = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            clave = f"{prefijo}{k}"
            if isinstance(v, (dict, list)):
                plano.update(_aplanar(v, f"{clave}."))
            else:
                plano[clave] = v
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            plano.update(_aplanar(v, f"{prefijo}{i}."))
    return plano


def extraer(payload: Any) -> dict:
    """
    Saca las tres variables del payload sin asumir una estructura exacta.

    El Rule engine de USR puede mandar el JSON de varias formas
    (plano, anidado, o como lista de {name, value}). Esto cubre las tres.
    """
    resultado = {
        "oxigeno_disuelto": None,
        "temperatura": None,
        "saturacion": None,
        "dispositivo": None,
        "medido_en": None,
    }

    plano = _aplanar(payload)

    # Caso lista de variables: [{"name": "Temperature", "value": 27.8}, ...]
    def _recorrer_listas(obj):
        if isinstance(obj, list):
            for item in obj:
                if isinstance(item, dict):
                    nombre = item.get("name") or item.get("variableName") or item.get("key")
                    valor = item.get("value") if "value" in item else item.get("val")
                    if nombre and valor is not None:
                        destino = CAMPOS.get(str(nombre).lower().replace(" ", "_"))
                        if destino and resultado[destino] is None:
                            resultado[destino] = _num(valor)
                _recorrer_listas(item)
        elif isinstance(obj, dict):
            for v in obj.values():
                _recorrer_listas(v)

    _recorrer_listas(payload)

    # Caso claves directas, en cualquier nivel de anidamiento
    for clave, valor in plano.items():
        hoja = clave.split(".")[-1].lower().replace(" ", "_")
        destino = CAMPOS.get(hoja)
        if destino and resultado[destino] is None:
            n = _num(valor)
            if n is not None:
                resultado[destino] = n

        if resultado["dispositivo"] is None and hoja in ("devicename", "device_name", "sn", "deviceid", "device_id"):
            resultado["dispositivo"] = str(valor)

        if resultado["medido_en"] is None and hoja in ("time", "timestamp", "updatetime", "update_time", "ts"):
            resultado["medido_en"] = str(valor)

    return resultado


# --- Alarmas -----------------------------------------------------------------

MUDA_MIN = float(os.environ.get("MUDA_MIN", "10") or 10)


def _procesar_alarmas(dispositivo: Optional[str], od: Optional[float]) -> None:
    """Evalúa umbrales tras guardar una lectura y despacha notificaciones."""
    nombre = dispositivo or "sonda"
    with _lock, db() as con:
        avisos = alertas.evaluar_lectura(con, nombre, od, datetime.now(timezone.utc))
    for texto in avisos:
        threading.Thread(target=alertas.notificar_telegram, args=(texto,), daemon=True).start()


async def _vigilar_mudas() -> None:
    while True:
        await asyncio.sleep(60)
        try:
            with _lock, db() as con:
                avisos = alertas.revisar_mudas(con, datetime.now(timezone.utc), MUDA_MIN)
            for texto in avisos:
                threading.Thread(target=alertas.notificar_telegram, args=(texto,), daemon=True).start()
        except Exception as e:
            print(f"alarmas: error vigilando mudas: {e}")


# --- Colector Modbus TCP -----------------------------------------------------

MODBUS_TCP_PORT = int(os.environ.get("MODBUS_TCP_PORT", "0") or 0)
MODBUS_INTERVALO = float(os.environ.get("MODBUS_INTERVALO", "60") or 60)
MODBUS_REGISTRO = os.environ.get("MODBUS_REGISTRO", "").strip()


async def guardar_lectura_modbus(campos: dict, dispositivo: Optional[str]) -> None:
    """Guarda una lectura obtenida por sondeo Modbus en la misma tabla."""
    marca = datetime.now(timezone.utc)
    ahora = marca.isoformat()
    payload = json.dumps({"origen": "modbus_tcp", "dispositivo": dispositivo, **campos})
    with _lock, db() as con:
        manipulada = _es_manipulacion(con, dispositivo, marca, campos.get("temperatura"))
        con.execute(
            """INSERT INTO lecturas
               (recibido_en, medido_en, dispositivo, oxigeno_disuelto, temperatura,
                saturacion, payload, manipulacion)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                ahora,
                ahora,  # aquí la medición es nuestra, no diferida por la nube
                dispositivo,
                None if campos.get("oxigeno_disuelto") is None else round(campos["oxigeno_disuelto"], 2),
                None if campos.get("temperatura") is None else round(campos["temperatura"], 2),
                None if campos.get("saturacion") is None else round(campos["saturacion"], 2),
                payload,
                1 if manipulada else 0,
            ),
        )
    # Sin esto, cada vez que alguien saca la sonda del agua llega un Telegram
    # de "oxígeno crítico". Tres de esos y la gente deja de mirar las alarmas.
    if not manipulada:
        _procesar_alarmas(dispositivo, campos.get("oxigeno_disuelto"))


async def _arrancar_modbus() -> None:
    server = await asyncio.start_server(
        lambda r, w: modbus.atender_modulo(
            r, w, guardar=guardar_lectura_modbus, intervalo=MODBUS_INTERVALO,
            registro_esperado=MODBUS_REGISTRO,
        ),
        "0.0.0.0",
        MODBUS_TCP_PORT,
    )
    print(f"modbus: escuchando en puerto {MODBUS_TCP_PORT}, sondeo cada {MODBUS_INTERVALO:g}s")
    asyncio.ensure_future(server.serve_forever())


# --- Endpoints ---------------------------------------------------------------

@app.on_event("startup")
async def _startup():
    init_db()
    asyncio.ensure_future(_vigilar_mudas())
    if MODBUS_TCP_PORT:
        await _arrancar_modbus()


@app.get("/health")
def health():
    return {"ok": True, "db": DB_PATH}


@app.post("/usr/webhook")
async def webhook(request: Request, token: str = Query(default="")):
    if not AUTH_TOKEN:
        # Sin token configurado el webhook queda deshabilitado: cualquier proceso
        # podría inyectar lecturas falsas (y con alarmas eso es peligroso).
        raise HTTPException(status_code=503, detail="AUTH_TOKEN no configurado en el servidor")
    entregado = token or request.headers.get("X-Auth-Token", "")
    if entregado != AUTH_TOKEN:
        raise HTTPException(status_code=401, detail="token inválido")

    crudo = await request.body()
    texto = crudo.decode("utf-8", errors="replace")

    try:
        payload = json.loads(texto) if texto.strip() else {}
    except json.JSONDecodeError:
        # Si no viene JSON, lo guardamos igual para poder ver qué manda USR.
        payload = {"_texto_plano": texto}

    campos = extraer(payload)
    marca = datetime.now(timezone.utc)
    ahora = marca.isoformat()

    with _lock, db() as con:
        manipulada = _es_manipulacion(con, campos["dispositivo"], marca, campos["temperatura"])
        con.execute(
            """INSERT INTO lecturas
               (recibido_en, medido_en, dispositivo, oxigeno_disuelto, temperatura,
                saturacion, payload, manipulacion)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                ahora,
                campos["medido_en"],
                campos["dispositivo"],
                campos["oxigeno_disuelto"],
                campos["temperatura"],
                campos["saturacion"],
                texto[:20000],
                1 if manipulada else 0,
            ),
        )

    if not manipulada:
        _procesar_alarmas(campos["dispositivo"], campos["oxigeno_disuelto"])
    return {"ok": True, "recibido_en": ahora, "extraido": campos}


SQL_ULTIMA_VALIDA = """
    SELECT * FROM lecturas
    WHERE (oxigeno_disuelto IS NOT NULL
        OR temperatura IS NOT NULL
        OR saturacion IS NOT NULL)
    ORDER BY id DESC LIMIT 1
"""


@app.get("/api/latest")
def latest(dispositivo: str = Query(default="")):
    sql = SQL_ULTIMA_VALIDA
    params: list = []
    if dispositivo:
        sql = sql.replace("ORDER BY", "AND dispositivo = ? ORDER BY")
        params.append(dispositivo)
    with db() as con:
        fila = con.execute(sql, params).fetchone()
    if not fila:
        return JSONResponse({"error": "todavía no llega ninguna lectura"}, status_code=404)
    d = dict(fila)
    d.pop("payload", None)
    return d


@app.get("/api/readings")
def readings(limit: int = Query(default=100, ge=1, le=20000), since: str = Query(default=""),
             dispositivo: str = Query(default="")):
    sql = ("SELECT id, recibido_en, medido_en, dispositivo, oxigeno_disuelto,"
           " temperatura, saturacion, COALESCE(manipulacion, 0) manipulacion FROM lecturas")
    condiciones, params = [], []
    if since:
        condiciones.append("recibido_en >= ?")
        params.append(since)
    if dispositivo:
        condiciones.append("dispositivo = ?")
        params.append(dispositivo)
    if condiciones:
        sql += " WHERE " + " AND ".join(condiciones)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)

    with db() as con:
        filas = con.execute(sql, params).fetchall()
    return {"total": len(filas), "lecturas": [dict(f) for f in filas]}


@app.get("/api/raw")
def raw(limit: int = Query(default=5, ge=1, le=50)):
    """Payloads crudos tal como llegaron. Úsalo para ajustar el parseo."""
    with db() as con:
        filas = con.execute(
            "SELECT id, recibido_en, payload FROM lecturas ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return {"payloads": [dict(f) for f in filas]}


STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")


def _leer_static(nombre: str) -> str:
    with open(os.path.join(STATIC_DIR, nombre), encoding="utf-8") as f:
        return f.read()


def render_panel() -> str:
    """
    El panel vive en static/ (HTML, CSS y JS por separado, editables con
    resaltado de sintaxis) pero se sirve como UNA página con todo incrustado:
    una sola petición, sin problemas de caché en el navegador de la finca.
    Se lee en cada petición: son unos KB y así los cambios se ven al instante.
    """
    html = _leer_static("index.html")
    html = html.replace("<!-- CSS -->", "<style>\n" + _leer_static("app.css") + "\n</style>")
    html = html.replace("<!-- JS -->", "<script>\n" + _leer_static("app.js") + "\n</script>")
    return html


@app.get("/api/dispositivos")
def listar_dispositivos():
    """Módulos conocidos: los ubicados en el mapa y los vistos en lecturas."""
    with db() as con:
        conf = {f["nombre"]: dict(f) for f in con.execute("SELECT * FROM dispositivos")}
        ultimas = {}
        for f in con.execute("""
            SELECT l.dispositivo, l.recibido_en, l.oxigeno_disuelto, l.temperatura, l.saturacion
            FROM lecturas l
            JOIN (SELECT dispositivo, MAX(id) mid FROM lecturas
                  WHERE dispositivo IS NOT NULL GROUP BY dispositivo) u
              ON l.id = u.mid
        """):
            ultimas[f["dispositivo"]] = {
                "recibido_en": f["recibido_en"],
                "oxigeno_disuelto": f["oxigeno_disuelto"],
                "temperatura": f["temperatura"],
                "saturacion": f["saturacion"],
            }
    nombres = sorted(set(conf) | set(ultimas))
    return {"dispositivos": [
        {
            "nombre": n,
            "lat": conf.get(n, {}).get("lat"),
            "lng": conf.get(n, {}).get("lng"),
            "descripcion": conf.get(n, {}).get("descripcion") or "",
            "ultima": ultimas.get(n),
        }
        for n in nombres
    ]}


@app.put("/api/dispositivos/{nombre}")
async def ubicar_dispositivo(nombre: str, request: Request, token: str = Query(default="")):
    """Fija la ubicación (y descripción) de un módulo en el mapa. Exige el token."""
    if AUTH_TOKEN:
        entregado = token or request.headers.get("X-Auth-Token", "")
        if entregado != AUTH_TOKEN:
            raise HTTPException(status_code=401, detail="token inválido")

    cuerpo = await request.json()
    lat, lng = cuerpo.get("lat"), cuerpo.get("lng")
    if (lat is None) != (lng is None):
        raise HTTPException(status_code=422, detail="lat y lng van juntos")
    if lat is not None:
        try:
            lat, lng = float(lat), float(lng)
        except (TypeError, ValueError):
            raise HTTPException(status_code=422, detail="lat y lng deben ser numéricos")
    descripcion = None if "descripcion" not in cuerpo else str(cuerpo.get("descripcion") or "")

    # Los campos que no vienen en el cuerpo se conservan: así se puede
    # registrar una sonda solo con descripción, o reubicarla sin tocarla.
    with _lock, db() as con:
        con.execute(
            """INSERT INTO dispositivos (nombre, lat, lng, descripcion) VALUES (?, ?, ?, ?)
               ON CONFLICT(nombre) DO UPDATE SET
                 lat         = COALESCE(excluded.lat, dispositivos.lat),
                 lng         = COALESCE(excluded.lng, dispositivos.lng),
                 descripcion = COALESCE(excluded.descripcion, dispositivos.descripcion)""",
            (nombre, lat, lng, descripcion),
        )
    return {"ok": True, "nombre": nombre, "lat": lat, "lng": lng}


UTC_OFFSET_HORAS = float(os.environ.get("STATS_UTC_OFFSET", "-5") or -5)  # Ecuador


@app.get("/api/stats")
def stats(dias: int = Query(default=7, ge=1, le=90), dispositivo: str = Query(default="")):
    """Resumen por día (hora local) y piscina: mín/máx/promedio de OD y temperatura."""
    corrimiento = f"{UTC_OFFSET_HORAS:+g} hours"
    sql = f"""
        SELECT date(recibido_en, ?) fecha, dispositivo, COUNT(*) n,
               MIN(oxigeno_disuelto) od_min, MAX(oxigeno_disuelto) od_max,
               AVG(oxigeno_disuelto) od_prom,
               MIN(temperatura) temp_min, MAX(temperatura) temp_max,
               AVG(temperatura) temp_prom
        FROM lecturas
        WHERE date(recibido_en, ?) >= date('now', ?, ?)
          AND (oxigeno_disuelto IS NOT NULL OR temperatura IS NOT NULL)
          AND COALESCE(manipulacion, 0) = 0
    """
    params: list = [corrimiento, corrimiento, corrimiento, f"-{dias - 1} days"]
    if dispositivo:
        sql += " AND dispositivo = ?"
        params.append(dispositivo)
    sql += " GROUP BY fecha, dispositivo ORDER BY fecha DESC, dispositivo"
    with db() as con:
        filas = [dict(f) for f in con.execute(sql, params)]
    return {"dias": filas}


@app.get("/api/export.csv")
def export_csv(since: str = Query(default=""), hasta: str = Query(default=""),
               dispositivo: str = Query(default="")):
    """Histórico en CSV (se abre directo en Excel)."""
    import csv
    import io
    sql = ("SELECT recibido_en, dispositivo, oxigeno_disuelto, temperatura, saturacion"
           " FROM lecturas")
    condiciones, params = [], []
    if since:
        condiciones.append("recibido_en >= ?")
        params.append(since)
    if hasta:
        condiciones.append("recibido_en <= ?")
        params.append(hasta)
    if dispositivo:
        condiciones.append("dispositivo = ?")
        params.append(dispositivo)
    if condiciones:
        sql += " WHERE " + " AND ".join(condiciones)
    sql += " ORDER BY id"
    def _seguro(v):
        # Neutraliza inyección de fórmulas en Excel (deviceName viene de fuera).
        if isinstance(v, str) and v and v[0] in ("=", "+", "-", "@", "\t", "\r"):
            return "'" + v
        return v

    salida = io.StringIO()
    w = csv.writer(salida)
    w.writerow(["recibido_en", "dispositivo", "oxigeno_disuelto_mg_l", "temperatura_c", "saturacion_pct"])
    with db() as con:
        for f in con.execute(sql, params):
            w.writerow([_seguro(f["recibido_en"]), _seguro(f["dispositivo"]),
                        f["oxigeno_disuelto"], f["temperatura"], f["saturacion"]])
    return Response(salida.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": "attachment; filename=lecturas_sonda.csv"})


@app.get("/api/alarmas")
def api_alarmas(limit: int = Query(default=50, ge=1, le=500)):
    with db() as con:
        return {"activas": alertas.alarmas_activas(con),
                "historial": alertas.historial(con, limit)}


ORDEN_URGENCIA = {"critico": 0, "aviso": 1, "sin_datos": 2, "manipulacion": 3,
                  "desconocida": 4, "normal": 5}


@app.get("/api/estados")
def api_estados():
    """
    Estado de todas las piscinas de una vez, ordenadas por urgencia.

    Con una sonda daba igual consultarlas una por una; con doce serían doce
    peticiones cada diez segundos. Aquí las agregaciones se hacen en SQL
    —una consulta por concepto, no una por piscina— para que añadir sondas
    no multiplique el trabajo de la base.
    """
    ahora = datetime.now(timezone.utc)
    t_hora = ahora - timedelta(hours=1)
    t_seis = ahora - timedelta(hours=6)

    with db() as con:
        ultimas = con.execute("""
            SELECT l.dispositivo, l.recibido_en, l.oxigeno_disuelto, l.temperatura,
                   l.saturacion, COALESCE(l.manipulacion, 0) manipulacion
            FROM lecturas l
            JOIN (SELECT dispositivo, MAX(id) mid FROM lecturas
                  WHERE dispositivo IS NOT NULL GROUP BY dispositivo) u ON l.id = u.mid
        """).fetchall()

        # Regresión por mínimos cuadrados hecha con sumas en SQL: el tiempo se
        # mide en horas desde el inicio de la ventana para no perder precisión
        # elevando al cuadrado epochs de diez cifras.
        base = int(t_hora.timestamp())
        pend = {}
        for f in con.execute("""
            SELECT dispositivo, COUNT(*) n,
                   SUM((strftime('%s', recibido_en) - ?) / 3600.0) sx,
                   SUM(oxigeno_disuelto) sy,
                   SUM((strftime('%s', recibido_en) - ?) / 3600.0 * oxigeno_disuelto) sxy,
                   SUM(((strftime('%s', recibido_en) - ?) / 3600.0) *
                       ((strftime('%s', recibido_en) - ?) / 3600.0)) sxx
            FROM lecturas
            WHERE recibido_en >= ? AND oxigeno_disuelto IS NOT NULL
              AND dispositivo IS NOT NULL AND COALESCE(manipulacion, 0) = 0
            GROUP BY dispositivo
        """, (base, base, base, base, t_hora.isoformat())):
            n = f["n"]
            if n < 5:
                continue
            den = f["sxx"] - f["sx"] * f["sx"] / n
            if den and abs(den) > 1e-12:
                pend[f["dispositivo"]] = round((f["sxy"] - f["sx"] * f["sy"] / n) / den, 3)

        # Chispa: OD promediado en cubos de 15 min sobre 6 h. Agregar en SQL
        # evita traer miles de filas al navegador solo para dibujar 24 puntos.
        chispas: dict = {}
        for f in con.execute("""
            SELECT dispositivo, CAST(strftime('%s', recibido_en) / 900 AS INTEGER) cubo,
                   AVG(oxigeno_disuelto) od
            FROM lecturas
            WHERE recibido_en >= ? AND oxigeno_disuelto IS NOT NULL
              AND dispositivo IS NOT NULL AND COALESCE(manipulacion, 0) = 0
            GROUP BY dispositivo, cubo ORDER BY dispositivo, cubo
        """, (t_seis.isoformat(),)):
            chispas.setdefault(f["dispositivo"], []).append(round(f["od"], 2))

        conf = {f["nombre"]: f["descripcion"] for f in
                con.execute("SELECT nombre, descripcion FROM dispositivos")}

        lecturas_hora = con.execute(
            """SELECT COUNT(*) FROM lecturas WHERE recibido_en >= ?
               AND (oxigeno_disuelto IS NOT NULL OR temperatura IS NOT NULL)""",
            (t_hora.isoformat(),)).fetchone()[0]

        piscinas = []
        for f in ultimas:
            nombre = f["dispositivo"]
            umbrales = alertas.umbrales_de(con, nombre)
            try:
                edad = (ahora - datetime.fromisoformat(f["recibido_en"])).total_seconds()
            except (TypeError, ValueError):
                edad = None

            # Una piscina muda no está "normal": está sin datos, que para
            # decidir si te levantas es tan accionable como un aviso.
            zona = _zona_operativa(f["oxigeno_disuelto"], umbrales, edad,
                                   bool(f["manipulacion"]))
            callada = zona in ("sin_datos", "manipulacion")

            p = pend.get(nombre)
            horas = None
            if p is not None and p < -0.05 and f["oxigeno_disuelto"] is not None and not callada:
                margen = f["oxigeno_disuelto"] - umbrales["od_critico"]
                if margen > 0:
                    horas = round(margen / -p, 1)

            piscinas.append({
                "dispositivo": nombre,
                "nombre": conf.get(nombre) or nombre,
                "zona": zona,
                "oxigeno_disuelto": f["oxigeno_disuelto"],
                "temperatura": f["temperatura"],
                "saturacion": f["saturacion"],
                "umbrales": umbrales,
                "pendiente_od_hora": p,
                "horas_a_critico": horas,
                "edad_segundos": round(edad) if edad is not None else None,
                "chispa": chispas.get(nombre, []),
            })

    # Primero lo que exige acción, y dentro de cada grupo lo que menos margen
    # tiene. En orden alfabético la piscina que se muere queda en la fila cuatro.
    piscinas.sort(key=lambda p: (
        ORDEN_URGENCIA.get(p["zona"], 9),
        p["horas_a_critico"] if p["horas_a_critico"] is not None else 1e9,
        p["oxigeno_disuelto"] if p["oxigeno_disuelto"] is not None else 1e9,
    ))

    resumen = {"total": len(piscinas), "lecturas_hora": lecturas_hora}
    for z in ("critico", "aviso", "sin_datos", "manipulacion", "normal", "desconocida"):
        n = sum(1 for p in piscinas if p["zona"] == z)
        if n or z in ("critico", "aviso"):
            resumen[z] = n
    return {"resumen": resumen, "piscinas": piscinas,
            "sin_datos_tras_segundos": SEGUNDOS_SIN_DATOS}


@app.get("/api/estado")
def api_estado(dispositivo: str = Query(default="")):
    """
    Estado operativo de una piscina: en qué zona está el oxígeno, hacia dónde
    va y cuánto margen queda antes del umbral crítico.

    Responde la pregunta de las 3 de la mañana — "¿tengo que levantarme a
    prender los aireadores?" — sin que nadie tenga que interpretar una gráfica.
    La lógica de zonas vive aquí y no en el navegador, para que el panel y las
    alarmas nunca puedan discrepar sobre qué es «crítico».
    """
    sql = SQL_ULTIMA_VALIDA
    params: list = []
    if dispositivo:
        sql = sql.replace("ORDER BY", "AND dispositivo = ? ORDER BY")
        params.append(dispositivo)

    with db() as con:
        ult = con.execute(sql, params).fetchone()
        if not ult:
            return {"hay_datos": False}

        nombre = ult["dispositivo"] or "sonda"
        umbrales = alertas.umbrales_de(con, nombre)

        desde = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        historia = con.execute(
            """SELECT recibido_en, oxigeno_disuelto FROM lecturas
               WHERE recibido_en >= ? AND oxigeno_disuelto IS NOT NULL
                 AND COALESCE(manipulacion, 0) = 0
                 AND dispositivo IS ? ORDER BY recibido_en""",
            (desde, ult["dispositivo"]),
        ).fetchall()

    od = ult["oxigeno_disuelto"]
    try:
        edad = (datetime.now(timezone.utc)
                - datetime.fromisoformat(ult["recibido_en"])).total_seconds()
    except (TypeError, ValueError):
        edad = None

    zona = _zona_operativa(od, umbrales, edad, bool(ult["manipulacion"]))
    pendiente = _pendiente_por_hora(historia)

    # Margen: a este ritmo de caída, cuánto falta para tocar el crítico.
    # Solo tiene sentido si está bajando de verdad; el ruido de la sonda
    # produce pendientes minúsculas que darían estimaciones absurdas.
    # Sin datos frescos no se estima margen: proyectar desde una lectura vieja
    # da una hora concreta que suena precisa y no significa nada.
    horas_a_critico = None
    if (pendiente is not None and pendiente < -0.05 and od is not None
            and zona not in ("sin_datos", "manipulacion")):
        margen = od - umbrales["od_critico"]
        if margen > 0:
            horas_a_critico = round(margen / -pendiente, 1)

    return {
        "hay_datos": True,
        "dispositivo": nombre,
        "zona": zona,
        "oxigeno_disuelto": od,
        "temperatura": ult["temperatura"],
        "saturacion": ult["saturacion"],
        "umbrales": umbrales,
        "pendiente_od_hora": pendiente,
        "horas_a_critico": horas_a_critico,
        "edad_segundos": round(edad) if edad is not None else None,
        "medido_en": ult["recibido_en"],
        "muestras_tendencia": len(historia),
        "sin_datos_tras_segundos": SEGUNDOS_SIN_DATOS,
    }


# El agua de una piscina de camarón tarda horas en moverse un grado: un salto
# de más de esto entre lecturas consecutivas solo puede ser la sonda saliendo o
# entrando al agua. No es una medición de la piscina.
GRADIENTE_MANIPULACION = 1.0      # °C por minuto
DELTA_MINIMO_MANIPULACION = 0.8   # °C absolutos, para que el ruido no dispare
VENTANA_MANIPULACION = 10 * 60    # s que siguen sospechosos mientras se estabiliza


def _es_manipulacion(con, dispositivo: Optional[str], ahora: datetime,
                     temperatura: Optional[float]) -> bool:
    """
    ¿Esta lectura cae dentro de un episodio de manipulación de la sonda?

    Se calcula desde la base y no desde memoria: así sobrevive a un reinicio
    del servidor a mitad de episodio, que es justo cuando se manipula el equipo.
    """
    if temperatura is None:
        return False

    desde = (ahora - timedelta(seconds=VENTANA_MANIPULACION)).isoformat()
    previas = con.execute(
        """SELECT recibido_en, temperatura FROM lecturas
           WHERE dispositivo IS ? AND recibido_en >= ? AND temperatura IS NOT NULL
           ORDER BY recibido_en""",
        (dispositivo, desde),
    ).fetchall()
    if not previas:
        return False

    # Un salto brusco en cualquier punto de la ventana contamina lo que sigue:
    # tras sacarla del agua, la sonda tarda minutos en volver a equilibrarse.
    serie = [(f["recibido_en"], f["temperatura"]) for f in previas]
    serie.append((ahora.isoformat(), temperatura))
    for i in range(1, len(serie)):
        try:
            t0 = datetime.fromisoformat(serie[i - 1][0])
            t1 = datetime.fromisoformat(serie[i][0])
        except (TypeError, ValueError):
            continue
        minutos = (t1 - t0).total_seconds() / 60
        if minutos <= 0:
            continue
        salto = abs(serie[i][1] - serie[i - 1][1])
        # Se exigen las dos cosas. Solo con la pendiente, dos lecturas separadas
        # por un segundo convierten 0.05 °C de ruido del sensor en 3 °C/min.
        if salto > DELTA_MINIMO_MANIPULACION and salto / minutos > GRADIENTE_MANIPULACION:
            return True
    return False


# Una lectura más vieja que esto ya no describe el estado del agua. El número
# vive aquí y se publica en la API: el navegador no debe tener su propia copia,
# o la cabecera y la franja acaban discrepando unos segundos.
SEGUNDOS_SIN_DATOS = 180


def _zona_operativa(od: Optional[float], umbrales: dict, edad: Optional[float],
                    manipulada: bool = False) -> str:
    """
    Zona que se muestra: la del oxígeno, salvo que el dato esté frío.

    Una piscina muda no está "normal" — está sin datos, y para decidir si te
    levantas eso es tan accionable como un aviso. Esta función es la única
    autoridad: la usan los dos endpoints y el panel lee su resultado.
    """
    if edad is not None and edad > SEGUNDOS_SIN_DATOS:
        return "sin_datos"
    # La sonda fuera del agua mide el aire correctamente; simplemente no está
    # midiendo la piscina. Decir "crítico" ahí sería una alarma falsa.
    if manipulada:
        return "manipulacion"
    return _zona_od(od, umbrales)


def _zona_od(od: Optional[float], umbrales: dict) -> str:
    """normal | aviso | critico | desconocida. Mismo criterio que las alarmas."""
    if od is None:
        return "desconocida"
    if od < umbrales["od_critico"]:
        return "critico"
    if od < umbrales["od_aviso"]:
        return "aviso"
    return "normal"


def _pendiente_por_hora(filas) -> Optional[float]:
    """
    Tendencia del oxígeno en mg/L por hora, por mínimos cuadrados.

    Una regresión sobre la última hora resiste mucho mejor el ruido que restar
    la primera y la última lectura, que es justo lo que haría que la tendencia
    saltara de signo entre refrescos.
    """
    puntos = []
    for f in filas:
        try:
            t = datetime.fromisoformat(f["recibido_en"]).timestamp() / 3600.0
        except (TypeError, ValueError):
            continue
        if f["oxigeno_disuelto"] is not None:
            puntos.append((t, f["oxigeno_disuelto"]))

    n = len(puntos)
    if n < 5:
        return None

    tm = sum(p[0] for p in puntos) / n
    vm = sum(p[1] for p in puntos) / n
    num = sum((t - tm) * (v - vm) for t, v in puntos)
    den = sum((t - tm) ** 2 for t, _ in puntos)
    if den == 0:
        return None
    return round(num / den, 3)


@app.get("/api/umbrales/{nombre}")
def api_umbrales(nombre: str):
    with db() as con:
        return alertas.umbrales_de(con, nombre)


@app.put("/api/umbrales/{nombre}")
async def fijar_umbrales(nombre: str, request: Request, token: str = Query(default="")):
    if AUTH_TOKEN:
        entregado = token or request.headers.get("X-Auth-Token", "")
        if entregado != AUTH_TOKEN:
            raise HTTPException(status_code=401, detail="token inválido")
    cuerpo = await request.json()
    try:
        od_aviso = float(cuerpo["od_aviso"])
        od_critico = float(cuerpo["od_critico"])
    except (KeyError, TypeError, ValueError):
        raise HTTPException(status_code=422, detail="od_aviso y od_critico numéricos requeridos")
    if od_critico > od_aviso:
        raise HTTPException(status_code=422, detail="od_critico debe ser <= od_aviso")
    with _lock, db() as con:
        alertas.fijar_umbrales(con, nombre, od_aviso, od_critico)
    return {"ok": True, "od_aviso": od_aviso, "od_critico": od_critico}


@app.delete("/api/dispositivos/{nombre}")
def quitar_dispositivo(nombre: str, request: Request, token: str = Query(default="")):
    """Quita la ubicación de un módulo del mapa. Sus lecturas no se tocan."""
    if AUTH_TOKEN:
        entregado = token or request.headers.get("X-Auth-Token", "")
        if entregado != AUTH_TOKEN:
            raise HTTPException(status_code=401, detail="token inválido")
    with _lock, db() as con:
        con.execute("DELETE FROM dispositivos WHERE nombre = ?", (nombre,))
    return {"ok": True, "nombre": nombre}


@app.get("/", response_class=HTMLResponse)
def home():
    return render_panel()
