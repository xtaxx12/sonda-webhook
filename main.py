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


PAGINA = """<!doctype html>
<html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Sonda OD</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"
      integrity="sha256-p4NxAoJBhIIN+hmNHrzRCf9tD/miZyoHS5obTRR9BMY=" crossorigin="anonymous">
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"
        integrity="sha256-20nQCchB9co0qIjJZRGuk2/Z9VM+kNiyxNV1lvTlZBo=" crossorigin="anonymous"></script>
<style>
  :root { color-scheme: light dark; }
  body {
    --fondo:#fbfbfa; --tinta:#1a1a18; --tinta2:#6b6a66; --borde:#e5e4e0;
    --tarjeta:#ffffff; --aviso:#b45309;
    --z-ok:#158a60; --z-aviso:#b45309; --z-crit:#c0322f;
    --z-aviso-bg:rgba(180,83,9,.09); --z-crit-bg:rgba(192,50,47,.10);
    --s-od:#2a78d6; --s-temp:#eb6834; --s-sat:#1baf7a;
    font-family: system-ui, -apple-system, sans-serif; margin:0;
    padding-block: 2rem; padding-inline: 1.25rem; max-width: 46rem;
    margin-inline:auto; background:var(--fondo); color:var(--tinta);
  }
  @media (prefers-color-scheme: dark) { body {
    --fondo:#191917; --tinta:#f0efec; --tinta2:#a3a29b; --borde:#35342f;
    --tarjeta:#232320; --aviso:#f59e0b;
    --z-ok:#38c496; --z-aviso:#f59e0b; --z-crit:#f07676;
    --z-aviso-bg:rgba(245,158,11,.10); --z-crit-bg:rgba(240,118,118,.12);
    --s-od:#3987e5; --s-temp:#d95926; --s-sat:#199e70;
  } }
  [hidden] { display:none !important; }  /* que hidden gane a display:flex */
  .cabecera { display:flex; justify-content:space-between; align-items:baseline;
              flex-wrap:wrap; gap:.35rem 1rem; margin:0 0 1.25rem; }
  h1 { font-size:1.15rem; font-weight:600; letter-spacing:-.01em; margin:0; }
  .cab-estado { display:flex; gap:.9rem; align-items:baseline; flex-wrap:wrap; font-size:.8rem; color:var(--tinta2); }
  .salud { font-weight:500; }
  .salud[data-nivel=ok]   { color:var(--z-ok); }
  .salud[data-nivel=mal]  { color:var(--aviso); }
  .reloj { font-variant-numeric:tabular-nums; }
  .grid { display:grid; gap:.75rem; grid-template-columns:repeat(2,1fr); }
  .tarjeta.principal { grid-column:1/-1; }
  .tarjeta.principal .valor { font-size:2.9rem; line-height:1.05; }
  .tarjeta.principal[data-zona=aviso]   .valor { color:var(--z-aviso); }
  .tarjeta.principal[data-zona=critico] .valor { color:var(--z-crit); }
  /* Un dato frío no debe verse tan vivo como uno de hace cinco segundos:
     la cifra grande es lo que capta la vista periférica. */
  .tarjeta.principal[data-zona=sin_datos] .valor,
  .tarjeta.principal[data-zona=manipulacion] .valor,
  .tarjeta.principal[data-zona=sin_datos] + .tendencia { opacity:.45; }
  .tarjeta.principal[data-zona=sin_datos] .valor { text-decoration:line-through;
      text-decoration-thickness:1px; text-decoration-color:var(--tinta2); }
  .tarjeta { background:var(--tarjeta); border:1px solid var(--borde); border-radius:.6rem; padding:1rem 1.1rem; }
  .tarjeta h2 { font-size:.72rem; font-weight:500; text-transform:uppercase;
                letter-spacing:.06em; color:var(--tinta2); margin:0 0 .4rem;
                display:flex; align-items:center; gap:.45em; }
  .punto { width:.55em; height:.55em; border-radius:50%; flex:none; }
  .valor { font-size:1.9rem; font-weight:600; margin:0; letter-spacing:-.02em; }
  .valor span { font-size:.85rem; font-weight:400; color:var(--tinta2); margin-left:.25rem; }
  .meta { margin:.9rem 0 1.4rem; font-size:.82rem; line-height:1.7; color:var(--tinta2); }
  .meta .alerta { color:var(--aviso); font-weight:500; }
  .rangos { display:flex; gap:.4rem; align-items:center; margin:0 0 .6rem; flex-wrap:wrap; }
  .rangos span { font-size:.78rem; color:var(--tinta2); margin-right:.2rem; }
  .rangos button { font:inherit; font-size:.8rem; padding:.25rem .7rem; border-radius:1rem;
                   border:1px solid var(--borde); background:var(--tarjeta); color:var(--tinta); cursor:pointer; }
  .rangos button.activo { border-color:var(--tinta); font-weight:600; }
  .bloque { background:var(--tarjeta); border:1px solid var(--borde); border-radius:.6rem;
            padding:.8rem 1rem .6rem; margin-bottom:.75rem; }
  .bloque header { display:flex; justify-content:space-between; align-items:baseline; margin-bottom:.3rem; }
  .bloque h2 { font-size:.72rem; font-weight:500; text-transform:uppercase; letter-spacing:.06em;
               color:var(--tinta2); margin:0; display:flex; align-items:center; gap:.45em; }
  .bloque .actual { font-size:.85rem; font-weight:600; }
  [data-grafica] svg { display:block; width:100%; height:130px; }
  .gridline { stroke:var(--borde); stroke-width:1; }
  .ejey, .ejex { font-size:10px; fill:var(--tinta2); }
  .cruz { stroke:var(--tinta2); stroke-width:1; stroke-dasharray:3 3; }
  .vacio { font-size:.9rem; color:var(--tinta2); padding:1.6rem 0; text-align:center; }
  #alarmas { border:1px solid #e34948; background:rgba(227,73,72,.1); border-radius:.6rem;
             padding:.7rem 1rem; margin-bottom:1rem; font-size:.88rem; line-height:1.8; }
  #alarmas strong { color:#e34948; }
  @media (prefers-color-scheme: dark) { #alarmas strong { color:#e66767; } #alarmas { border-color:#e66767; } }
  /* Estado operativo: lo primero que se lee, y lo unico que importa de madrugada. */
  .estado { display:flex; align-items:center; gap:.6rem; flex-wrap:wrap;
            border:1px solid var(--borde); background:var(--tarjeta);
            border-radius:.6rem; padding:.6rem .9rem; margin-bottom:.75rem; font-size:.9rem; }
  .estado .pill { font-weight:600; display:flex; align-items:center; gap:.5em; }
  .estado .pill::before { content:""; width:.62em; height:.62em; border-radius:50%;
                          background:currentColor; flex:none; }
  .estado .frescura { margin-left:auto; color:var(--tinta2); font-size:.82rem; }
  .estado[data-zona=normal]  { border-color:var(--z-ok);    color:var(--z-ok); }
  .estado[data-zona=aviso]   { border-color:var(--z-aviso); color:var(--z-aviso); background:var(--z-aviso-bg); }
  .estado[data-zona=critico] { border-color:var(--z-crit);  color:var(--z-crit);  background:var(--z-crit-bg); }
  .estado[data-zona=manipulacion] { border-color:var(--aviso); color:var(--aviso); }
  .estado[data-frio=si] { border-color:var(--aviso); color:var(--aviso); }
  .estado[data-zona=critico] .pill { animation:latido 1.4s ease-in-out infinite; }
  @keyframes latido { 50% { opacity:.4 } }
  @media (prefers-reduced-motion: reduce) { .estado[data-zona=critico] .pill { animation:none } }

  /* Vista de conjunto: con una sonda sobra, con doce es la pantalla principal. */
  .resumen-finca { margin:0 0 .6rem; font-size:.86rem; color:var(--tinta2);
                   display:flex; gap:.85rem; flex-wrap:wrap; align-items:baseline; }
  .resumen-finca .c-critico  { color:var(--z-crit);  font-weight:600; }
  .resumen-finca .c-aviso    { color:var(--z-aviso); font-weight:600; }
  .resumen-finca .c-sin      { color:var(--aviso);   font-weight:600; }
  .rejilla { display:grid; gap:.6rem; margin-bottom:1.3rem;
             grid-template-columns:repeat(auto-fill,minmax(10.5rem,1fr)); }
  .piscina { text-align:left; font:inherit; cursor:pointer; color:var(--tinta);
             background:var(--tarjeta); border:1px solid var(--borde);
             border-radius:.6rem; padding:.7rem .8rem; }
  .piscina:hover { border-color:var(--tinta2); }
  .piscina.activa { border-color:var(--tinta); box-shadow:inset 0 0 0 1px var(--tinta); }
  .piscina[data-zona=critico]   { border-color:var(--z-crit);  background:var(--z-crit-bg); }
  .piscina[data-zona=aviso]     { border-color:var(--z-aviso); background:var(--z-aviso-bg); }
  .piscina[data-zona=sin_datos] { border-style:dashed; }
  .piscina .nom { font-size:.75rem; color:var(--tinta2); margin:0 0 .25rem;
                  display:flex; align-items:center; gap:.4em;
                  overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .piscina .od { font-size:1.45rem; font-weight:600; margin:0; letter-spacing:-.02em; }
  .piscina .od small { font-size:.7rem; font-weight:400; color:var(--tinta2); margin-left:.2rem; }
  .piscina[data-zona=critico] .od { color:var(--z-crit); }
  .piscina[data-zona=aviso]   .od { color:var(--z-aviso); }
  .piscina svg { display:block; width:100%; height:26px; margin-top:.3rem; }
  .piscina .pie { margin:.25rem 0 0; font-size:.71rem; color:var(--tinta2);
                  display:flex; justify-content:space-between; gap:.4rem; }

  .tendencia { margin:.55rem 0 0; font-size:.88rem; color:var(--tinta2);
               display:flex; gap:.9rem; flex-wrap:wrap; align-items:baseline; }
  .tendencia .margen { color:var(--z-aviso); font-weight:600; }
  .tendencia .umbrales { margin-left:auto; font-size:.8rem; }

  .banda-crit  { fill:var(--z-crit);  opacity:.13; }
  .banda-aviso { fill:var(--z-aviso); opacity:.11; }
  .banda-noche { fill:var(--tinta);   opacity:.055; }
  .banda-manip { fill:var(--aviso);   opacity:.16; }
  .etq-manip   { font-size:9px; fill:var(--aviso); }
  .linea-umbral { stroke:var(--z-crit); stroke-width:1; stroke-dasharray:4 3; opacity:.6; }
  .etq-umbral { font-size:9px; fill:var(--z-crit); opacity:.85; }

  /* Escritorio: a 46rem centradas, una pantalla de 1900px deja el 60% en negro.
     A partir de 1100px el cuerpo pasa a dos columnas y solo las piezas de
     estado siguen ocupando el ancho completo. */
  @media (min-width: 1100px) {
    body { max-width: 76rem; display:grid; gap:0 .75rem;
           grid-template-columns: repeat(2, minmax(0, 1fr));
           align-content: start; }
    body > .cabecera, body > #alarmas, body > #piscinas, body > #finca,
    body > .estado, body > #tarjetas, body > #meta, body > #rangos,
    body > details, body > nav, body > noscript { grid-column: 1 / -1; }
    body > .bloque { grid-column: auto; margin-bottom:.75rem; }
    [data-grafica] svg { height:170px; }
    #mapa { height:100%; min-height:320px; }
    /* El mapa y el resumen conviven bien uno al lado del otro. */
    body > .bloque:has(#mapa) { grid-row: span 1; }
  }
  @media (min-width: 1500px) {
    body { max-width: 92rem; grid-template-columns: repeat(3, minmax(0, 1fr)); }
  }

  #tooltip { position:fixed; pointer-events:none; background:var(--tarjeta); color:var(--tinta);
             border:1px solid var(--borde); border-radius:.45rem; padding:.5rem .7rem;
             font-size:.78rem; line-height:1.6; box-shadow:0 2px 10px rgba(0,0,0,.12);
             z-index:10; }
  #tooltip .fila { display:flex; align-items:center; gap:.45em; }
  #tooltip .fila b { margin-left:auto; padding-left:1em; }
  #mapa { height:320px; border-radius:.45rem; }
  #mapa.ubicando { cursor:crosshair; }
  .leaflet-container { background:var(--fondo); font:inherit; }
  @media (prefers-color-scheme: dark) {
    /* Solo el mapa de calles se invierte; una foto satelital invertida no sirve. */
    #mapa .leaflet-layer.capa-osm, #mapa .leaflet-control-zoom, #mapa .leaflet-control-attribution
      { filter: invert(1) hue-rotate(180deg) brightness(.9) contrast(.9); }
    .leaflet-popup-content-wrapper, .leaflet-popup-tip { background:var(--tarjeta); color:var(--tinta); }
    .leaflet-control-layers { background:var(--tarjeta); color:var(--tinta); border-color:var(--borde); }
  }
  .leaflet-control-layers { font:inherit; font-size:.78rem; border-radius:.45rem; }
  .leaflet-control-layers label { display:flex; align-items:center; gap:.3em; margin:.15em 0; }
  .mapa-cab { display:flex; justify-content:space-between; align-items:center; margin-bottom:.5rem; }
  .mapa-cab button { font:inherit; font-size:.8rem; padding:.3rem .8rem; border-radius:1rem;
                     border:1px solid var(--borde); background:var(--tarjeta); color:var(--tinta); cursor:pointer; }
  .mapa-cab button.activo { border-color:var(--tinta); font-weight:600; }
  .mapa-nota { font-size:.78rem; color:var(--tinta2); margin:.4rem 0 0; }
  .popup-dato { font-size:.8rem; line-height:1.7; }
  .popup-dato b { font-size:.95rem; }
  .popup-quitar { font:inherit; font-size:.75rem; margin-top:.35rem; padding:.2rem .6rem;
                  border-radius:1rem; border:1px solid var(--borde); background:var(--tarjeta);
                  color:var(--aviso); cursor:pointer; }
  details { margin-top:1.2rem; font-size:.82rem; }
  summary { cursor:pointer; color:var(--tinta2); }
  table { border-collapse:collapse; width:100%; margin-top:.6rem; font-size:.78rem; }
  th, td { text-align:right; padding:.3rem .5rem; border-bottom:1px solid var(--borde); }
  th:first-child, td:first-child { text-align:left; }
  th { color:var(--tinta2); font-weight:500; }
  .tabla-scroll { overflow-x:auto; }
  code { background:rgba(128,128,128,.15); padding:.15em .4em; border-radius:.25rem; font-size:.85em; }
  nav { margin-top:1.6rem; font-size:.82rem; color:var(--tinta2); }
  nav a { color:inherit; margin-right:1rem; }
</style></head>
<body>
  <div class="cabecera">
    <h1>Sonda de oxígeno disuelto</h1>
    <div class="cab-estado">
      <span id="salud" class="salud">○ Conectando…</span>
      <span id="reloj" class="reloj" title="Hora local de Ecuador (UTC−5)">— (UTC−5)</span>
    </div>
  </div>

  <div id="alarmas" hidden></div>

  <div class="rangos" id="piscinas" hidden><span>Piscina:</span></div>

  <div id="finca" hidden>
    <p class="resumen-finca" id="resumen-finca"></p>
    <div class="rejilla" id="rejilla"></div>
  </div>

  <div class="estado" id="estado" hidden>
    <span class="pill" id="estado-txt">—</span>
    <span class="frescura" id="frescura"></span>
  </div>

  <div class="grid" id="tarjetas">
    <div class="tarjeta principal" id="tarjeta-od">
      <h2><span class="punto" style="background:var(--s-od)"></span>Oxígeno disuelto</h2>
      <p class="valor" id="v-od">—<span>mg/L</span></p>
      <p class="tendencia" id="tendencia"></p></div>
    <div class="tarjeta"><h2><span class="punto" style="background:var(--s-temp)"></span>Temperatura</h2>
      <p class="valor" id="v-temp">—<span>°C</span></p></div>
    <div class="tarjeta"><h2><span class="punto" style="background:var(--s-sat)"></span>Saturación</h2>
      <p class="valor" id="v-sat">—<span>%</span></p></div>
  </div>

  <p class="meta" id="meta">Cargando…</p>

  <div class="rangos" id="rangos">
    <span>Histórico:</span>
    <button data-rango="1" class="activo">1 h</button>
    <button data-rango="6">6 h</button>
    <button data-rango="24">24 h</button>
    <button id="btn-umbrales" style="margin-left:auto"
            title="Umbrales de alarma de oxígeno de la piscina seleccionada">⚙ Umbrales</button>
  </div>

  <div class="bloque"><header>
      <h2><span class="punto" style="background:var(--s-od)"></span>Oxígeno disuelto (mg/L)</h2>
      <span class="actual" id="a-od"></span></header>
    <div data-grafica="oxigeno_disuelto"></div></div>
  <div class="bloque"><header>
      <h2><span class="punto" style="background:var(--s-temp)"></span>Temperatura (°C)</h2>
      <span class="actual" id="a-temp"></span></header>
    <div data-grafica="temperatura"></div></div>
  <div class="bloque"><header>
      <h2><span class="punto" style="background:var(--s-sat)"></span>Saturación (%)</h2>
      <span class="actual" id="a-sat"></span></header>
    <div data-grafica="saturacion"></div></div>

  <div class="bloque">
    <div class="mapa-cab">
      <h2 style="font-size:.72rem;font-weight:500;text-transform:uppercase;letter-spacing:.06em;color:var(--tinta2);margin:0">Ubicación de los módulos</h2>
      <span style="display:flex;gap:.4rem;flex-wrap:wrap">
        <button id="btn-registrar" title="Da de alta una sonda nueva con su nombre y descripción">➕ Registrar sonda</button>
        <button id="btn-gps" title="Usa el GPS de este dispositivo: párate junto al módulo y púlsalo">📡 Mi ubicación</button>
        <button id="btn-ubicar" title="Haz clic aquí y luego en el mapa para fijar dónde está el módulo">📍 En el mapa</button>
      </span>
    </div>
    <div id="mapa"></div>
    <p class="mapa-nota" id="mapa-nota"></p>
  </div>

  <div class="bloque">
    <div class="mapa-cab">
      <h2 style="font-size:.72rem;font-weight:500;text-transform:uppercase;letter-spacing:.06em;color:var(--tinta2);margin:0">Resumen diario (7 días)</h2>
      <a id="btn-csv" href="/api/export.csv" download
         style="font-size:.8rem;padding:.3rem .8rem;border-radius:1rem;border:1px solid var(--borde);background:var(--tarjeta);color:var(--tinta);text-decoration:none">⬇ Descargar CSV</a>
    </div>
    <div class="tabla-scroll"><table>
      <thead><tr><th>Día</th><th>Piscina</th><th>Lecturas</th>
        <th>OD mín</th><th>OD máx</th><th>OD prom</th>
        <th>T° mín</th><th>T° máx</th></tr></thead>
      <tbody id="resumen"></tbody>
    </table></div>
  </div>

  <details>
    <summary>Ver tabla de lecturas recientes</summary>
    <div class="tabla-scroll"><table>
      <thead><tr><th>Hora</th><th>OD (mg/L)</th><th>Temp (°C)</th><th>Sat (%)</th><th>Origen</th></tr></thead>
      <tbody id="tabla"></tbody>
    </table></div>
  </details>

  <details>
    <summary>Historial de alarmas</summary>
    <div class="tabla-scroll"><table>
      <thead><tr><th>Piscina</th><th>Tipo</th><th>Valor</th><th>Inició</th><th>Resuelta</th></tr></thead>
      <tbody id="tabla-alarmas"></tbody>
    </table></div>
  </details>

  <div id="tooltip" hidden></div>

  <noscript><p class="vacio">Esta página necesita JavaScript; usa <a href="/api/latest">/api/latest</a>.</p></noscript>

  <nav>
    <a href="/api/latest">última</a>
    <a href="/api/readings">histórico</a>
    <a href="/api/raw">crudo</a>
    <a href="/docs">API</a>
  </nav>

<script>
"use strict";
const SERIES = [
  { campo: "oxigeno_disuelto", color: "var(--s-od)",   dec: 2, unidad: "mg/L", nombre: "Oxígeno" },
  { campo: "temperatura",      color: "var(--s-temp)", dec: 2, unidad: "°C",   nombre: "Temperatura" },
  { campo: "saturacion",       color: "var(--s-sat)",  dec: 1, unidad: "%",    nombre: "Saturación" },
];
let rangoHoras = 1;
let datos = [];            // ascendente en el tiempo
let ultimaCarga = null;
let piscina = "";          // dispositivo seleccionado ("" = el único / todos)
let umbrales = null;       // { od_aviso, od_critico } de la piscina mostrada
let finca = {};            // dispositivo -> estado, para el mapa y la rejilla
try { piscina = localStorage.getItem("piscina_panel") || ""; } catch (e) {}

const fmtHora = t => t.toLocaleTimeString("es", { hour: "2-digit", minute: "2-digit" });

const COLOR_ZONA = {
  normal: "--z-ok", aviso: "--z-aviso", critico: "--z-crit",
  sin_datos: "--tinta2", manipulacion: "--tinta2", desconocida: "--tinta2",
};
const cssVar = n => getComputedStyle(document.body).getPropertyValue(n).trim() || "#888";

function chispa(valores, color) {
  if (!valores || valores.length < 2) return "";
  const w = 100, h = 26, min = Math.min(...valores), max = Math.max(...valores);
  const rango = (max - min) || 1;
  const d = valores.map((v, i) =>
    `${i ? "L" : "M"}${(i / (valores.length - 1) * w).toFixed(1)},` +
    `${(h - 2 - (v - min) / rango * (h - 5)).toFixed(1)}`).join("");
  return `<svg viewBox="0 0 ${w} ${h}" preserveAspectRatio="none" aria-hidden="true">` +
         `<path d="${d}" fill="none" stroke="${color}" stroke-width="1.6" ` +
         `vector-effect="non-scaling-stroke" stroke-linejoin="round"/></svg>`;
}

// --- Salud del sistema y reloj (cabecera) -------------------------------------
// "En línea" se deriva de la frescura de las lecturas: si todas las sondas
// reportaron hace menos de 90 s, el agente y la red están vivos.
function actualizarSalud(lista) {
  const el = document.getElementById("salud");
  if (!lista.length) { el.textContent = "○ Sin sondas todavía"; el.dataset.nivel = ""; return; }
  // La zona ya viene decidida por el servidor; un umbral propio aquí haría
  // que la cabecera y la franja discreparan durante unos segundos.
  const mudas = lista.filter(p => p.zona === "sin_datos");
  if (mudas.length) {
    el.textContent = `⚠ ${mudas.length === 1 ? mudas[0].nombre + " sin datos" : mudas.length + " sondas sin datos"}`;
    el.dataset.nivel = "mal";
  } else {
    el.textContent = `● Sistema en línea · ${lista.length === 1 ? "sonda reportando" : lista.length + " sondas reportando"}`;
    el.dataset.nivel = "ok";
  }
}

const FMT_RELOJ = new Intl.DateTimeFormat("es-EC", {
  timeZone: "America/Guayaquil", weekday: "short", day: "numeric", month: "short",
  hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false,
});
function actualizarReloj() {
  document.getElementById("reloj").textContent = FMT_RELOJ.format(new Date()) + " (UTC−5)";
}
actualizarReloj();
setInterval(actualizarReloj, 1000);

// Vista de conjunto: con doce piscinas, entrar viendo el detalle de una
// —la de lectura más reciente, que era el criterio— responde la pregunta
// equivocada. Primero cuál necesita atención; el detalle después.
async function cargarFinca() {
  let j;
  try { j = await (await fetch("/api/estados")).json(); } catch (e) { return; }
  const lista = j.piscinas || [];
  finca = Object.fromEntries(lista.map(p => [p.dispositivo, p]));
  actualizarSalud(lista);

  const cont = document.getElementById("finca");
  if (lista.length < 2) { cont.hidden = true; return; }   // una sola: sería ruido
  cont.hidden = false;

  const r = j.resumen || {};
  const trozos = [`<span><b>${r.total}</b> piscinas</span>`];
  if (r.critico)   trozos.push(`<span class="c-critico">${r.critico} en crítico</span>`);
  if (r.aviso)     trozos.push(`<span class="c-aviso">${r.aviso} en aviso</span>`);
  if (r.sin_datos) trozos.push(`<span class="c-sin">${r.sin_datos} sin datos</span>`);
  if (r.normal)    trozos.push(`<span>${r.normal} normales</span>`);
  document.getElementById("resumen-finca").innerHTML = trozos.join("");

  document.getElementById("rejilla").innerHTML = lista.map(p => {
    const od = p.oxigeno_disuelto === null || p.oxigeno_disuelto === undefined
      ? "—" : p.oxigeno_disuelto.toFixed(2);
    const pend = p.pendiente_od_hora;
    const tend = p.zona === "sin_datos" ? "sin reportar"
      : pend === null || pend === undefined ? "—"
      : Math.abs(pend) < 0.05 ? "→ estable"
      : `${pend < 0 ? "↓" : "↑"} ${Math.abs(pend).toFixed(2)}/h`;
    const margen = p.horas_a_critico !== null && p.horas_a_critico !== undefined
      ? `<span class="c-aviso">~${p.horas_a_critico < 1 ? Math.round(p.horas_a_critico*60)+" min" : p.horas_a_critico.toFixed(1)+" h"}</span>`
      : `<span>${haceCuanto(p.edad_segundos)}</span>`;
    return `<button class="piscina${p.dispositivo === piscina ? " activa" : ""}" ` +
      `data-zona="${p.zona}" data-ir="${encodeURIComponent(p.dispositivo)}">` +
      `<p class="nom"><span class="punto" style="background:${cssVar(COLOR_ZONA[p.zona])}"></span>` +
      `${p.nombre}</p>` +
      `<p class="od">${od}<small>mg/L</small></p>` +
      chispa(p.chispa, cssVar(COLOR_ZONA[p.zona])) +
      `<p class="pie"><span>${tend}</span>${margen}</p></button>`;
  }).join("");
}

document.getElementById("rejilla").addEventListener("click", ev => {
  const b = ev.target.closest("button[data-ir]");
  if (!b) return;
  piscina = decodeURIComponent(b.dataset.ir);
  try { localStorage.setItem("piscina_panel", piscina); } catch (e) {}
  cargarFinca();
  pintarPiscinas();
  cargarEstado().then(cargar);
  cargarResumen();
  document.getElementById("estado").scrollIntoView({ behavior: "smooth", block: "start" });
});

const ETIQUETA_ZONA = {
  normal:  "Oxígeno normal",
  aviso:   "Oxígeno bajo",
  critico: "Oxígeno crítico",
  sin_datos: "Sin datos recientes",
  manipulacion: "Sonda en manipulación",
  desconocida: "Sin lectura de oxígeno",
};

function haceCuanto(seg) {
  if (seg === null || seg === undefined) return "";
  if (seg < 90) return `hace ${Math.max(0, Math.round(seg))} s`;
  if (seg < 5400) return `hace ${Math.round(seg / 60)} min`;
  return `hace ${Math.round(seg / 3600)} h`;
}

// El estado vive en el servidor (/api/estado) para que el panel y las alarmas
// no puedan discrepar sobre qué cuenta como crítico.
async function cargarEstado() {
  const franja = document.getElementById("estado");
  const tarjeta = document.getElementById("tarjeta-od");
  const tend = document.getElementById("tendencia");
  let e;
  try {
    const r = await fetch("/api/estado" + (piscina ? `?dispositivo=${encodeURIComponent(piscina)}` : ""));
    e = await r.json();
  } catch (err) { return; }

  if (!e.hay_datos) { franja.hidden = true; tend.textContent = ""; return; }

  umbrales = e.umbrales;
  franja.hidden = false;
  franja.dataset.zona = e.zona;
  // Un dato viejo no es un estado: si la sonda lleva rato muda, eso es lo
  // que hay que gritar, no el último valor que se alcanzó a leer.
  const frio = e.zona === "sin_datos" || e.zona === "manipulacion";
  franja.dataset.frio = frio ? "si" : "no";
  document.getElementById("estado-txt").textContent =
    ETIQUETA_ZONA[e.zona] || e.zona;
  document.getElementById("frescura").textContent =
    haceCuanto(e.edad_segundos) + (e.dispositivo ? ` · ${e.dispositivo}` : "");

  tarjeta.dataset.zona = e.zona;

  const partes = [];
  const p = e.pendiente_od_hora;
  if (p === null || p === undefined) {
    partes.push("<span>Tendencia: pocas lecturas todavía</span>");
  } else if (Math.abs(p) < 0.05) {
    partes.push("<span>→ estable</span>");
  } else {
    partes.push(`<span>${p < 0 ? "↓" : "↑"} ${Math.abs(p).toFixed(2)} mg/L por hora</span>`);
  }
  if (e.horas_a_critico !== null && e.horas_a_critico !== undefined) {
    const h = e.horas_a_critico;
    const cuando = h < 1 ? `${Math.round(h * 60)} min` : `${h.toFixed(1)} h`;
    partes.push(`<span class="margen">Llega al crítico en ~${cuando}</span>`);
  }
  if (umbrales) {
    partes.push(`<span class="umbrales">aviso ${umbrales.od_aviso}` +
                ` · crítico ${umbrales.od_critico} mg/L</span>`);
  }
  tend.innerHTML = partes.join("");
}

async function cargar() {
  const desde = new Date(Date.now() - rangoHoras * 3600e3).toISOString();
  try {
    const r = await fetch(`/api/readings?limit=20000&since=${encodeURIComponent(desde)}` +
      (piscina ? `&dispositivo=${encodeURIComponent(piscina)}` : ""));
    const j = await r.json();
    datos = (j.lecturas || []).filter(l =>
      l.oxigeno_disuelto !== null || l.temperatura !== null || l.saturacion !== null
    ).reverse();
    datos.forEach(l => { l.t = new Date(l.recibido_en); });
    ultimaCarga = new Date();
    render();
  } catch (e) {
    document.getElementById("meta").innerHTML =
      '<span class="alerta">⚠ No se pudo consultar la API — ¿el servidor está corriendo?</span>';
  }
}

function render() {
  const ult = datos[datos.length - 1];
  const ids = { oxigeno_disuelto: ["v-od", "a-od"], temperatura: ["v-temp", "a-temp"], saturacion: ["v-sat", "a-sat"] };
  for (const s of SERIES) {
    const v = ult ? ult[s.campo] : null;
    const texto = v === null || v === undefined ? "—" : v.toFixed(s.dec);
    document.getElementById(ids[s.campo][0]).innerHTML = `${texto}<span>${s.unidad}</span>`;
    document.getElementById(ids[s.campo][1]).textContent = texto === "—" ? "" : `${texto} ${s.unidad}`;
  }

  const meta = document.getElementById("meta");
  if (!ult) {
    meta.innerHTML = "Sin lecturas en este rango todavía.";
  } else {
    const edad = (Date.now() - ult.t.getTime()) / 1000;
    const alerta = edad > 90
      ? ` <span class="alerta">⚠ sin datos nuevos desde hace ${edad < 5400 ? Math.round(edad / 60) + " min" : Math.round(edad / 3600) + " h"}</span>`
      : "";
    meta.innerHTML =
      `Última lectura: <strong>${ult.t.toLocaleString("es")}</strong>` +
      ` · ${datos.length} lecturas en ${rangoHoras} h` +
      (ult.dispositivo ? ` · ${ult.dispositivo}` : "") + alerta;
  }

  for (const s of SERIES) dibujar(s);

  const tabla = document.getElementById("tabla");
  tabla.innerHTML = datos.slice(-20).reverse().map(l => {
    const c = (v, d) => v === null || v === undefined ? "—" : v.toFixed(d);
    return `<tr><td>${l.t.toLocaleTimeString("es")}</td><td>${c(l.oxigeno_disuelto, 2)}</td>` +
           `<td>${c(l.temperatura, 2)}</td><td>${c(l.saturacion, 1)}</td><td>${l.dispositivo || "—"}</td></tr>`;
  }).join("");
}

function escala(puntos, campo, h, padT, padB, incluir) {
  const vals = puntos.map(p => p[campo]).filter(v => v !== null && v !== undefined);
  let vmin = Math.min(...vals), vmax = Math.max(...vals);
  const minReal = vmin;               // sin el margen estético, para decidir umbrales
  if (vmin === vmax) { vmin -= 1; vmax += 1; }
  const margen = (vmax - vmin) * 0.12;
  vmin -= margen; vmax += margen;
  // Bajar el eje hasta el umbral siempre aplastaría la línea contra el techo y
  // escondería la tendencia, que es la señal temprana. Se hace solo cuando el
  // agua ya se acerca al aviso: ahí el margen importa más que el detalle.
  // Contra el vmin ORIGINAL, no contra el ya bajado: si no, incluir el aviso
  // vuelve elegible al crítico y la escala cae en cascada hasta el fondo
  // aunque el agua esté perfecta.
  for (const v of (incluir || [])) {
    if (v !== null && v !== undefined && v < minReal && minReal - v <= 2) vmin = Math.min(vmin, v - 0.15);
  }
  return { vmin, vmax, y: v => padT + (1 - (v - vmin) / (vmax - vmin)) * (h - padT - padB) };
}

function dibujar(serie) {
  const cont = document.querySelector(`[data-grafica="${serie.campo}"]`);
  const puntos = datos.filter(p => p[serie.campo] !== null && p[serie.campo] !== undefined);
  if (puntos.length < 2) {
    cont.innerHTML = '<p class="vacio">Aún no hay suficientes lecturas para la gráfica.</p>';
    return;
  }
  const w = Math.max(cont.clientWidth || 560, 280), h = 130;
  const padL = 46, padR = 12, padT = 8, padB = 18;
  const t0 = Date.now() - rangoHoras * 3600e3, t1 = Date.now();
  const x = t => padL + (t - t0) / (t1 - t0) * (w - padL - padR);
  const esOD = serie.campo === "oxigeno_disuelto";
  const forzar = esOD && umbrales ? [umbrales.od_aviso, umbrales.od_critico] : null;
  const { vmin, vmax, y } = escala(puntos, serie.campo, h, padT, padB, forzar);

  let svg = `<svg viewBox="0 0 ${w} ${h}" role="img" aria-label="${serie.nombre}, últimas ${rangoHoras} horas">`;

  // Franja nocturna: el oxígeno se desploma de madrugada, cuando la respiración
  // lleva horas sin fotosíntesis que la compense. Sin marcar la noche, el ciclo
  // diario es invisible y el mínimo del amanecer parece un dato suelto.
  if (rangoHoras >= 6) {
    const paso = 15 * 60e3;
    let ini = null;
    for (let t = t0; t <= t1 + paso; t += paso) {
      const hora = new Date(t).getHours();
      const noche = hora >= 18 || hora < 6;
      if (noche && ini === null) ini = t;
      if ((!noche || t > t1) && ini !== null) {
        const x0 = Math.max(padL, x(ini)), x1 = Math.min(w - padR, x(Math.min(t, t1)));
        if (x1 > x0) svg += `<rect class="banda-noche" x="${x0.toFixed(1)}" y="${padT}" ` +
                            `width="${(x1 - x0).toFixed(1)}" height="${h - padT - padB}"/>`;
        ini = null;
      }
    }
  }

  // Tramos con la sonda fuera del agua: sin marcarlos, una caída a 0.3 mg/L
  // por manipulación se lee igual que una asfixia real.
  {
    let ini = null;
    for (let i = 0; i <= datos.length; i++) {
      const m = i < datos.length && datos[i].manipulacion;
      if (m && ini === null) ini = datos[i].t.getTime();
      if (!m && ini !== null) {
        const fin = datos[i - 1].t.getTime();
        const x0 = Math.max(padL, x(ini)), x1 = Math.min(w - padR, x(fin));
        if (x1 > x0 + 0.5) {
          svg += `<rect class="banda-manip" x="${x0.toFixed(1)}" y="${padT}" ` +
                 `width="${(x1 - x0).toFixed(1)}" height="${h - padT - padB}"/>`;
          if (esOD && x1 - x0 > 46)
            svg += `<text class="etq-manip" x="${((x0 + x1) / 2).toFixed(1)}" ` +
                   `y="${padT + 9}" text-anchor="middle">manipulación</text>`;
        }
        ini = null;
      }
    }
  }

  // Bandas de umbral: el número solo no dice si 6.1 mg/L está bien o mal.
  if (esOD && umbrales) {
    const piso = h - padB;
    const yc = Math.min(piso, Math.max(padT, y(umbrales.od_critico)));
    const ya = Math.min(piso, Math.max(padT, y(umbrales.od_aviso)));
    if (ya < piso) svg += `<rect class="banda-aviso" x="${padL}" y="${ya.toFixed(1)}" ` +
                          `width="${w - padL - padR}" height="${(piso - ya).toFixed(1)}"/>`;
    if (yc < piso) svg += `<rect class="banda-crit" x="${padL}" y="${yc.toFixed(1)}" ` +
                          `width="${w - padL - padR}" height="${(piso - yc).toFixed(1)}"/>`;
    if (y(umbrales.od_critico) > padT && y(umbrales.od_critico) < piso) {
      svg += `<line class="linea-umbral" x1="${padL}" x2="${w - padR}" y1="${yc.toFixed(1)}" y2="${yc.toFixed(1)}"/>`;
      svg += `<text class="etq-umbral" x="${w - padR - 2}" y="${(yc - 3).toFixed(1)}" text-anchor="end">crítico ${umbrales.od_critico}</text>`;
    }
  }

  const nivel = f => vmin + (vmax - vmin) * f;
  for (const f of [0, 0.5, 1]) {
    const yy = y(nivel(f)).toFixed(1);
    svg += `<line class="gridline" x1="${padL}" x2="${w - padR}" y1="${yy}" y2="${yy}"/>`;
    svg += `<text class="ejey" x="${padL - 6}" y="${+yy + 3}" text-anchor="end">${nivel(f).toFixed(serie.dec === 1 ? 0 : 1)}</text>`;
  }
  for (const f of [0.08, 0.5, 0.92]) {
    const t = t0 + (t1 - t0) * f;
    svg += `<text class="ejex" x="${x(t).toFixed(1)}" y="${h - 4}" text-anchor="middle">${fmtHora(new Date(t))}</text>`;
  }
  // La línea se corta cuando hay un hueco en los datos (sonda muda, corte de luz):
  // un corte de dos horas no debe verse igual que agua estable.
  const deltas = puntos.slice(1).map((p, i) => p.t - puntos[i].t).sort((a, b) => a - b);
  const mediana = deltas[Math.floor(deltas.length / 2)] || 15000;
  const corte = Math.max(3 * mediana, 60000);
  let d = "", tPrev = null;
  for (const p of puntos) {
    const t = p.t.getTime();
    d += `${tPrev === null || t - tPrev > corte ? "M" : "L"}${x(t).toFixed(1)},${y(p[serie.campo]).toFixed(1)}`;
    tPrev = t;
  }
  svg += `<path d="${d}" fill="none" stroke="${serie.color}" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>`;
  const fin = puntos[puntos.length - 1];
  svg += `<circle cx="${x(fin.t.getTime()).toFixed(1)}" cy="${y(fin[serie.campo]).toFixed(1)}" r="3.5" fill="${serie.color}"/>`;
  svg += `<line class="cruz" y1="${padT}" y2="${h - padB}" x1="-9" x2="-9" data-cruz hidden/>`;
  svg += `<circle r="4" fill="none" stroke="${serie.color}" stroke-width="2" data-foco hidden cx="-9" cy="-9"/>`;
  svg += "</svg>";
  cont.innerHTML = svg;
  cont._puntos = puntos;
  cont._x = x; cont._y = y;

  const svgEl = cont.firstChild;
  svgEl.addEventListener("mousemove", ev => {
    const caja = svgEl.getBoundingClientRect();
    const t = t0 + (ev.clientX - caja.left) / caja.width * (t1 - t0);
    mostrarCruz(t, ev.clientX, ev.clientY);
  });
  svgEl.addEventListener("mouseleave", ocultarCruz);
}

function masCercano(puntos, t) {
  let mejor = puntos[0], dist = Infinity;
  for (const p of puntos) {
    const d = Math.abs(p.t.getTime() - t);
    if (d < dist) { dist = d; mejor = p; }
  }
  return mejor;
}

function mostrarCruz(t, cx, cy) {
  const tooltip = document.getElementById("tooltip");
  let filas = "", hora = "";
  for (const s of SERIES) {
    const cont = document.querySelector(`[data-grafica="${s.campo}"]`);
    if (!cont._puntos) continue;
    const p = masCercano(cont._puntos, t);
    hora = fmtHora(p.t);
    const xx = cont._x(p.t.getTime()).toFixed(1);
    const cruz = cont.querySelector("[data-cruz]"), foco = cont.querySelector("[data-foco]");
    cruz.setAttribute("x1", xx); cruz.setAttribute("x2", xx); cruz.hidden = false;
    foco.setAttribute("cx", xx); foco.setAttribute("cy", cont._y(p[s.campo]).toFixed(1)); foco.hidden = false;
    filas += `<div class="fila"><span class="punto" style="background:${s.color}"></span>` +
             `${s.nombre}<b>${p[s.campo].toFixed(s.dec)} ${s.unidad}</b></div>`;
  }
  tooltip.innerHTML = `<div class="fila"><b>${hora}</b></div>` + filas;
  tooltip.hidden = false;
  const dx = cx + 14 + 180 > innerWidth ? -14 - tooltip.offsetWidth : 14;
  tooltip.style.left = (cx + dx) + "px";
  tooltip.style.top = Math.min(cy + 12, innerHeight - tooltip.offsetHeight - 8) + "px";
}

function ocultarCruz() {
  document.getElementById("tooltip").hidden = true;
  document.querySelectorAll("[data-cruz],[data-foco]").forEach(e => { e.hidden = true; });
}

// --- Resumen diario ----------------------------------------------------------
async function cargarResumen() {
  const filtro = piscina ? `&dispositivo=${encodeURIComponent(piscina)}` : "";
  let j, umbral = 4.0;
  try {
    j = await (await fetch(`/api/stats?dias=7${filtro}`)).json();
    if (piscina) umbral = (await (await fetch(`/api/umbrales/${encodeURIComponent(piscina)}`)).json()).od_aviso;
  } catch (e) { return; }
  const c = (v, d) => v === null || v === undefined ? "—" : v.toFixed(d);
  document.getElementById("resumen").innerHTML = j.dias.map(d => {
    const alarma = d.od_min !== null && d.od_min < umbral;
    return `<tr><td>${d.fecha}</td><td>${d.dispositivo || "—"}</td><td>${d.n}</td>` +
      `<td${alarma ? ' style="color:#e34948;font-weight:600"' : ""}>${c(d.od_min, 2)}</td>` +
      `<td>${c(d.od_max, 2)}</td><td>${c(d.od_prom, 2)}</td>` +
      `<td>${c(d.temp_min, 1)}</td><td>${c(d.temp_max, 1)}</td></tr>`;
  }).join("") || '<tr><td colspan="8">Sin datos todavía.</td></tr>';
  const desde = new Date(Date.now() - 30 * 86400e3).toISOString();
  document.getElementById("btn-csv").href = `/api/export.csv?since=${encodeURIComponent(desde)}${filtro}`;
}

// --- Mapa de módulos ---------------------------------------------------------
let mapa = null, marcadores = null, dispositivos = [], mapaAjustado = false, ubicando = false;

function iniciarMapa() {
  const nota = document.getElementById("mapa-nota");
  if (typeof L === "undefined") {
    document.getElementById("mapa").innerHTML =
      '<p class="vacio">No se pudo cargar el mapa (¿sin internet?).</p>';
    return;
  }
  mapa = L.map("mapa").setView([-1.8, -78.5], 6);   // Ecuador por defecto

  // Satélite (Esri World Imagery, sin API key) + nombres de lugares encima.
  // En una camaronera las piscinas se ven desde el aire; es la base por defecto.
  const satelite = L.layerGroup([
    L.tileLayer("https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
      { maxZoom: 19, attribution: "Imágenes © Esri, Maxar, Earthstar Geographics" }),
    L.tileLayer("https://server.arcgisonline.com/ArcGIS/rest/services/Reference/World_Boundaries_and_Places/MapServer/tile/{z}/{y}/{x}",
      { maxZoom: 19, className: "capa-etiquetas", pane: "overlayPane" }),
  ]);
  // Calles (OpenStreetMap). Solo esta capa se invierte en modo oscuro.
  const calles = L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png",
    { maxZoom: 19, attribution: "© OpenStreetMap", className: "capa-osm" });

  let baseGuardada = "satelite";
  try { baseGuardada = localStorage.getItem("mapa_base") || "satelite"; } catch (e) {}
  (baseGuardada === "calles" ? calles : satelite).addTo(mapa);
  L.control.layers({ "Satélite": satelite, "Mapa": calles }, null,
    { position: "topright", collapsed: false }).addTo(mapa);
  mapa.on("baselayerchange", ev => {
    try { localStorage.setItem("mapa_base", ev.name === "Mapa" ? "calles" : "satelite"); } catch (e) {}
  });

  marcadores = L.layerGroup().addTo(mapa);
  nota.textContent = "Pulsa «En el mapa» y luego haz clic donde está la sonda.";

  mapa.on("click", async ev => {
    if (!ubicando) return;
    await ubicarModulo(ev.latlng.lat, ev.latlng.lng);
    terminarUbicar();
  });

  document.getElementById("btn-ubicar").addEventListener("click", () => {
    ubicando ? terminarUbicar() : empezarUbicar();
  });

  // Alta de una sonda nueva: nombre + descripción, y opcionalmente su punto.
  document.getElementById("btn-registrar").addEventListener("click", async () => {
    const nombre = (prompt(
      "Nombre de la sonda nueva.\\n\\nIMPORTANTE: debe ser EXACTAMENTE el mismo " +
      "SONDA_NOMBRE (deviceName) con el que reportará su agente, p. ej. piscina-2:") || "").trim();
    if (!nombre) return;
    if (dispositivos.some(d => d.nombre === nombre)) {
      alert(`«${nombre}» ya existe.`); return;
    }
    const descripcion = prompt("Descripción (p. ej. «Piscina 2, sector norte»):", "") || "";
    const token = localStorage.getItem("token_panel") || prompt("Token de la app (AUTH_TOKEN):") || "";
    const r = await fetch(`/api/dispositivos/${encodeURIComponent(nombre)}?token=${encodeURIComponent(token)}`, {
      method: "PUT", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ descripcion }),
    });
    if (r.status === 401) { localStorage.removeItem("token_panel"); alert("Token inválido."); return; }
    if (!r.ok) { alert("No se pudo registrar."); return; }
    try { localStorage.setItem("token_panel", token); } catch (e) {}
    await cargarDispositivos();
    if (confirm(`«${nombre}» registrada. ¿La ubicamos ahora en el mapa?\\n(Haz clic donde está.)`)) {
      nombrePendiente = nombre;
      empezarUbicar();
    }
  });

  // GPS del navegador: párate junto al módulo y pulsa el botón.
  document.getElementById("btn-gps").addEventListener("click", () => {
    if (!navigator.geolocation) {
      alert("Este navegador no soporta geolocalización."); return;
    }
    const nota = document.getElementById("mapa-nota");
    nota.textContent = "Obteniendo tu ubicación…";
    navigator.geolocation.getCurrentPosition(async pos => {
      const { latitude, longitude, accuracy } = pos.coords;
      if (!confirm(`¿Ubicar el módulo en tu posición actual?\\n(precisión ±${Math.round(accuracy)} m)`)) {
        terminarUbicar(); return;
      }
      const ok = await ubicarModulo(latitude, longitude);
      if (ok) { mapa.setView([latitude, longitude], Math.max(mapa.getZoom(), 16)); }
      terminarUbicar();
    }, err => {
      const razones = {
        1: "Permiso denegado — habilita la ubicación para este sitio.",
        2: "Posición no disponible.",
        3: "Tiempo de espera agotado.",
      };
      alert("No se pudo obtener la ubicación: " + (razones[err.code] || err.message) +
            (location.protocol === "http:" && location.hostname !== "localhost"
              ? "\\n(El GPS del navegador solo funciona con HTTPS o en localhost.)" : ""));
      terminarUbicar();
    }, { enableHighAccuracy: true, timeout: 15000, maximumAge: 0 });
  });
}

let nombrePendiente = "";   // sonda recién registrada, a la espera de su punto en el mapa

async function ubicarModulo(lat, lng) {
  const nombres = dispositivos.map(d => d.nombre);
  const nombre = nombrePendiente ||
    (nombres.length === 1 ? nombres[0]
      : prompt(`¿Qué módulo ubicas aquí?\\n(${nombres.join(", ") || "escribe el nombre"})`, nombres[0] || ""));
  nombrePendiente = "";
  if (!nombre) return false;
  const descripcion = prompt("Descripción del punto (opcional, p. ej. «Piscina 1»):",
    (dispositivos.find(d => d.nombre === nombre) || {}).descripcion || "") || "";
  const token = localStorage.getItem("token_panel") || prompt("Token de la app (AUTH_TOKEN):") || "";
  const r = await fetch(`/api/dispositivos/${encodeURIComponent(nombre)}?token=${encodeURIComponent(token)}`, {
    method: "PUT", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ lat, lng, descripcion }),
  });
  if (r.status === 401) { localStorage.removeItem("token_panel"); alert("Token inválido."); return false; }
  if (!r.ok) { alert("No se pudo guardar la ubicación."); return false; }
  try { localStorage.setItem("token_panel", token); } catch (e) {}
  await cargarDispositivos();
  return true;
}

function empezarUbicar() {
  ubicando = true;
  document.getElementById("btn-ubicar").classList.add("activo");
  document.getElementById("mapa").classList.add("ubicando");
  document.getElementById("mapa-nota").textContent = "Haz clic en el mapa donde está el módulo…";
}
function terminarUbicar() {
  ubicando = false;
  document.getElementById("btn-ubicar").classList.remove("activo");
  document.getElementById("mapa").classList.remove("ubicando");
  document.getElementById("mapa-nota").textContent =
    "Pulsa «En el mapa» y luego haz clic donde está la sonda.";
}

async function cargarDispositivos() {
  if (!mapa) return;
  try {
    const j = await (await fetch("/api/dispositivos")).json();
    dispositivos = j.dispositivos || [];
  } catch (e) { return; }
  marcadores.clearLayers();
  const puestos = dispositivos.filter(d => d.lat !== null && d.lng !== null);
  for (const d of puestos) {
    // El color es la ZONA DE OXÍGENO, no la conectividad. Con doce piscinas,
    // un mapa todo verde porque todas reportan —con una muriéndose— es
    // exactamente el fallo que un mapa debe evitar.
    const e = finca[d.nombre];
    const zona = e ? e.zona : "desconocida";
    let color = cssVar(COLOR_ZONA[zona] || "--tinta2");
    let estado = { normal: "● Oxígeno normal", aviso: "● Oxígeno bajo",
                   critico: "● Oxígeno crítico", sin_datos: "○ Sin datos recientes" }[zona]
                 || "○ Sin lecturas todavía";
    if (!d.ultima) { color = cssVar("--tinta2"); estado = "○ Sin lecturas todavía"; }
    if (d.ultima) {
      const edad = (Date.now() - new Date(d.ultima.recibido_en).getTime()) / 1000;
      if (edad > 90) estado += ` · hace ${edad < 5400 ? Math.round(edad / 60) + " min" : Math.round(edad / 3600) + " h"}`;
    }
    const u = d.ultima;
    const datos = u ? `<b>${u.oxigeno_disuelto?.toFixed(2) ?? "—"}</b> mg/L · ` +
                      `<b>${u.temperatura?.toFixed(2) ?? "—"}</b> °C · ` +
                      `<b>${u.saturacion?.toFixed(1) ?? "—"}</b> %<br>` : "";
    L.circleMarker([d.lat, d.lng], {
      radius: zona === "critico" ? 11 : 9, color: "#ffffff", weight: 2,
      dashArray: zona === "sin_datos" ? "3 3" : null,
      fillColor: color, fillOpacity: 0.95,
    }).bindPopup(
      `<div class="popup-dato"><strong>${(e && e.nombre) || d.descripcion || d.nombre}</strong>` +
      `<br>${datos}${estado}<br>` +
      `<button class="popup-quitar" data-quitar="${encodeURIComponent(d.nombre)}">🗑 Quitar del mapa</button></div>`
    ).addTo(marcadores);
  }
  pintarPiscinas();
  const sinUbicar = dispositivos.filter(d => d.lat === null);
  if (sinUbicar.length && !ubicando) {
    document.getElementById("mapa-nota").textContent =
      `Sin ubicar: ${sinUbicar.map(d => d.nombre).join(", ")} — pulsa «En el mapa» y haz clic donde está.`;
  }
  if (!mapaAjustado && puestos.length) {
    mapaAjustado = true;
    mapa.fitBounds(L.latLngBounds(puestos.map(d => [d.lat, d.lng])).pad(0.4), { maxZoom: 15 });
  }
}

// --- Alarmas -----------------------------------------------------------------
const NOMBRE_TIPO = { od_bajo: "🟠 Oxígeno bajo", od_critico: "🔴 Oxígeno CRÍTICO", sin_datos: "🔕 Sin datos" };

async function cargarAlarmas() {
  let j;
  try { j = await (await fetch("/api/alarmas")).json(); } catch (e) { return; }
  const banner = document.getElementById("alarmas");
  if (j.activas.length) {
    banner.hidden = false;
    banner.innerHTML = "<strong>⚠ Alarma activa</strong><br>" + j.activas.map(a =>
      `${NOMBRE_TIPO[a.tipo] || a.tipo} en <strong>${a.dispositivo}</strong>` +
      (a.valor !== null ? ` — ${a.valor.toFixed(2)} mg/L` : "") +
      ` (desde ${new Date(a.iniciada_en).toLocaleTimeString("es")})`
    ).join("<br>");
  } else {
    banner.hidden = true;
  }
  document.getElementById("tabla-alarmas").innerHTML = j.historial.slice(0, 20).map(a =>
    `<tr><td>${a.dispositivo}</td><td>${NOMBRE_TIPO[a.tipo] || a.tipo}</td>` +
    `<td>${a.valor !== null ? a.valor.toFixed(2) : "—"}</td>` +
    `<td>${new Date(a.iniciada_en).toLocaleString("es")}</td>` +
    `<td>${a.resuelta_en ? new Date(a.resuelta_en).toLocaleString("es") : "activa"}</td></tr>`
  ).join("") || '<tr><td colspan="5">Sin alarmas registradas.</td></tr>';
}

document.getElementById("btn-umbrales").addEventListener("click", async () => {
  const nombre = piscina || (dispositivos.find(d => d.ultima) || dispositivos[0] || {}).nombre;
  if (!nombre) { alert("Todavía no hay ninguna sonda reportando."); return; }
  let u = { od_aviso: 4, od_critico: 3 };
  try { u = await (await fetch(`/api/umbrales/${encodeURIComponent(nombre)}`)).json(); } catch (e) {}
  const aviso = parseFloat(prompt(`Umbral de AVISO para «${nombre}» (mg/L):`, u.od_aviso));
  if (isNaN(aviso)) return;
  const critico = parseFloat(prompt(`Umbral CRÍTICO para «${nombre}» (mg/L):`, u.od_critico));
  if (isNaN(critico)) return;
  const token = localStorage.getItem("token_panel") || prompt("Token de la app (AUTH_TOKEN):") || "";
  const r = await fetch(`/api/umbrales/${encodeURIComponent(nombre)}?token=${encodeURIComponent(token)}`, {
    method: "PUT", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ od_aviso: aviso, od_critico: critico }),
  });
  if (r.status === 401) { localStorage.removeItem("token_panel"); alert("Token inválido."); return; }
  if (!r.ok) { alert((await r.json()).detail || "No se pudo guardar."); return; }
  try { localStorage.setItem("token_panel", token); } catch (e) {}
  alert(`Umbrales de «${nombre}»: aviso < ${aviso} mg/L, crítico < ${critico} mg/L`);
});

function pintarPiscinas() {
  const cont = document.getElementById("piscinas");
  // Primero las que reportan; las registradas sin datos también cuentan.
  const orden = [...dispositivos].sort((a, b) => (b.ultima ? 1 : 0) - (a.ultima ? 1 : 0));
  const nombres = orden.map(d => d.nombre);
  // Con ≥2 sondas la rejilla "Todas las piscinas" ya hace de selector;
  // los chips solo duplicarían. Se conservan para sondas registradas sin datos.
  const rejillaVisible = !document.getElementById("finca").hidden;
  if (nombres.length < 2 || rejillaVisible) {
    cont.hidden = true;
    if (piscina && !nombres.includes(piscina)) { piscina = ""; }
    return;
  }
  if (!nombres.includes(piscina)) {
    // Por defecto, la piscina con la lectura más reciente.
    const conUltima = dispositivos.filter(d => d.ultima)
      .sort((a, b) => new Date(b.ultima.recibido_en) - new Date(a.ultima.recibido_en));
    piscina = (conUltima[0] || {}).nombre || nombres[0];
    try { localStorage.setItem("piscina_panel", piscina); } catch (e) {}
  }
  cont.hidden = false;
  cont.innerHTML = "<span>Piscina:</span>" + nombres.map(n =>
    `<button data-piscina="${encodeURIComponent(n)}" class="${n === piscina ? "activo" : ""}">` +
    `${(dispositivos.find(d => d.nombre === n) || {}).descripcion || n}</button>`
  ).join("");
}

document.getElementById("piscinas").addEventListener("click", ev => {
  const b = ev.target.closest("button[data-piscina]");
  if (!b) return;
  piscina = decodeURIComponent(b.dataset.piscina);
  try { localStorage.setItem("piscina_panel", piscina); } catch (e) {}
  pintarPiscinas();
  cargarEstado().then(cargar);
  cargarResumen();
});

document.addEventListener("click", async ev => {
  const b = ev.target.closest("[data-quitar]");
  if (!b) return;
  const nombre = decodeURIComponent(b.dataset.quitar);
  if (!confirm(`¿Quitar «${nombre}» del mapa?\\n(Sus lecturas no se borran.)`)) return;
  const token = localStorage.getItem("token_panel") || prompt("Token de la app (AUTH_TOKEN):") || "";
  const r = await fetch(`/api/dispositivos/${encodeURIComponent(nombre)}?token=${encodeURIComponent(token)}`,
    { method: "DELETE" });
  if (r.status === 401) { localStorage.removeItem("token_panel"); alert("Token inválido."); return; }
  if (r.ok) { try { localStorage.setItem("token_panel", token); } catch (e) {} mapa.closePopup(); cargarDispositivos(); }
});

document.getElementById("rangos").addEventListener("click", ev => {
  const b = ev.target.closest("button[data-rango]");
  if (!b) return;
  rangoHoras = +b.dataset.rango;
  document.querySelectorAll("#rangos button").forEach(x => x.classList.toggle("activo", x === b));
  cargar();
});
addEventListener("resize", () => { if (datos.length) render(); });

iniciarMapa();
cargarFinca().then(cargarDispositivos);
cargarEstado().then(cargar);
cargarAlarmas();
cargarResumen();
setInterval(() => {
  cargarFinca().then(cargarDispositivos);   // el mapa necesita las zonas ya cargadas
  cargarEstado().then(cargar);
  cargarAlarmas();
}, 10000);
setInterval(cargarResumen, 60000);
</script>
</body></html>"""


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

    resumen = {"total": len(piscinas)}
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
    return PAGINA
