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
from datetime import datetime, timezone
from typing import Any, Optional

import modbus

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse

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
        con.execute("""
            CREATE TABLE IF NOT EXISTS dispositivos (
                nombre      TEXT PRIMARY KEY,
                lat         REAL,
                lng         REAL,
                descripcion TEXT
            )
        """)


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


# --- Colector Modbus TCP -----------------------------------------------------

MODBUS_TCP_PORT = int(os.environ.get("MODBUS_TCP_PORT", "0") or 0)
MODBUS_INTERVALO = float(os.environ.get("MODBUS_INTERVALO", "60") or 60)
MODBUS_REGISTRO = os.environ.get("MODBUS_REGISTRO", "").strip()


async def guardar_lectura_modbus(campos: dict, dispositivo: Optional[str]) -> None:
    """Guarda una lectura obtenida por sondeo Modbus en la misma tabla."""
    ahora = datetime.now(timezone.utc).isoformat()
    payload = json.dumps({"origen": "modbus_tcp", "dispositivo": dispositivo, **campos})
    with _lock, db() as con:
        con.execute(
            """INSERT INTO lecturas
               (recibido_en, medido_en, dispositivo, oxigeno_disuelto, temperatura, saturacion, payload)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                ahora,
                ahora,  # aquí la medición es nuestra, no diferida por la nube
                dispositivo,
                campos.get("oxigeno_disuelto"),
                campos.get("temperatura"),
                campos.get("saturacion"),
                payload,
            ),
        )


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
    if MODBUS_TCP_PORT:
        await _arrancar_modbus()


@app.get("/health")
def health():
    return {"ok": True, "db": DB_PATH}


@app.post("/usr/webhook")
async def webhook(request: Request, token: str = Query(default="")):
    if AUTH_TOKEN:
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
    ahora = datetime.now(timezone.utc).isoformat()

    with _lock, db() as con:
        con.execute(
            """INSERT INTO lecturas
               (recibido_en, medido_en, dispositivo, oxigeno_disuelto, temperatura, saturacion, payload)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                ahora,
                campos["medido_en"],
                campos["dispositivo"],
                campos["oxigeno_disuelto"],
                campos["temperatura"],
                campos["saturacion"],
                texto[:20000],
            ),
        )

    return {"ok": True, "recibido_en": ahora, "extraido": campos}


SQL_ULTIMA_VALIDA = """
    SELECT * FROM lecturas
    WHERE oxigeno_disuelto IS NOT NULL
       OR temperatura IS NOT NULL
       OR saturacion IS NOT NULL
    ORDER BY id DESC LIMIT 1
"""


@app.get("/api/latest")
def latest():
    with db() as con:
        fila = con.execute(SQL_ULTIMA_VALIDA).fetchone()
    if not fila:
        return JSONResponse({"error": "todavía no llega ninguna lectura"}, status_code=404)
    d = dict(fila)
    d.pop("payload", None)
    return d


@app.get("/api/readings")
def readings(limit: int = Query(default=100, ge=1, le=20000), since: str = Query(default="")):
    sql = "SELECT id, recibido_en, medido_en, dispositivo, oxigeno_disuelto, temperatura, saturacion FROM lecturas"
    params: list = []
    if since:
        sql += " WHERE recibido_en >= ?"
        params.append(since)
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
    --s-od:#2a78d6; --s-temp:#eb6834; --s-sat:#1baf7a;
    font-family: system-ui, -apple-system, sans-serif; margin:0;
    padding-block: 2rem; padding-inline: 1.25rem; max-width: 46rem;
    margin-inline:auto; background:var(--fondo); color:var(--tinta);
  }
  @media (prefers-color-scheme: dark) { body {
    --fondo:#191917; --tinta:#f0efec; --tinta2:#a3a29b; --borde:#35342f;
    --tarjeta:#232320; --aviso:#f59e0b;
    --s-od:#3987e5; --s-temp:#d95926; --s-sat:#199e70;
  } }
  h1 { font-size:1.15rem; font-weight:600; letter-spacing:-.01em; margin:0 0 1.25rem; }
  .grid { display:grid; gap:.75rem; grid-template-columns:repeat(auto-fit,minmax(9rem,1fr)); }
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
    #mapa .leaflet-layer, #mapa .leaflet-control-zoom, #mapa .leaflet-control-attribution
      { filter: invert(1) hue-rotate(180deg) brightness(.9) contrast(.9); }
    .leaflet-popup-content-wrapper, .leaflet-popup-tip { background:var(--tarjeta); color:var(--tinta); }
  }
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
  <h1>Sonda de oxígeno disuelto</h1>

  <div class="grid" id="tarjetas">
    <div class="tarjeta"><h2><span class="punto" style="background:var(--s-od)"></span>Oxígeno disuelto</h2>
      <p class="valor" id="v-od">—<span>mg/L</span></p></div>
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
      <span style="display:flex;gap:.4rem">
        <button id="btn-gps" title="Usa el GPS de este dispositivo: párate junto al módulo y púlsalo">📡 Mi ubicación</button>
        <button id="btn-ubicar" title="Haz clic aquí y luego en el mapa para fijar dónde está el módulo">📍 En el mapa</button>
      </span>
    </div>
    <div id="mapa"></div>
    <p class="mapa-nota" id="mapa-nota"></p>
  </div>

  <details>
    <summary>Ver tabla de lecturas recientes</summary>
    <div class="tabla-scroll"><table>
      <thead><tr><th>Hora</th><th>OD (mg/L)</th><th>Temp (°C)</th><th>Sat (%)</th><th>Origen</th></tr></thead>
      <tbody id="tabla"></tbody>
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

const fmtHora = t => t.toLocaleTimeString("es", { hour: "2-digit", minute: "2-digit" });

async function cargar() {
  const desde = new Date(Date.now() - rangoHoras * 3600e3).toISOString();
  try {
    const r = await fetch(`/api/readings?limit=20000&since=${encodeURIComponent(desde)}`);
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

function escala(puntos, campo, h, padT, padB) {
  const vals = puntos.map(p => p[campo]).filter(v => v !== null && v !== undefined);
  let vmin = Math.min(...vals), vmax = Math.max(...vals);
  if (vmin === vmax) { vmin -= 1; vmax += 1; }
  const margen = (vmax - vmin) * 0.12;
  vmin -= margen; vmax += margen;
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
  const { vmin, vmax, y } = escala(puntos, serie.campo, h, padT, padB);

  let svg = `<svg viewBox="0 0 ${w} ${h}" role="img" aria-label="${serie.nombre}, últimas ${rangoHoras} horas">`;
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
  const d = puntos.map((p, i) => `${i ? "L" : "M"}${x(p.t.getTime()).toFixed(1)},${y(p[serie.campo]).toFixed(1)}`).join("");
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
  L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png",
    { maxZoom: 19, attribution: "© OpenStreetMap" }).addTo(mapa);
  marcadores = L.layerGroup().addTo(mapa);
  nota.textContent = "Pulsa «Ubicar módulo» y luego haz clic en el mapa para fijar su posición.";

  mapa.on("click", async ev => {
    if (!ubicando) return;
    await ubicarModulo(ev.latlng.lat, ev.latlng.lng);
    terminarUbicar();
  });

  document.getElementById("btn-ubicar").addEventListener("click", () => {
    ubicando ? terminarUbicar() : empezarUbicar();
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

async function ubicarModulo(lat, lng) {
  const nombres = dispositivos.map(d => d.nombre);
  const nombre = nombres.length === 1 ? nombres[0]
    : prompt(`¿Qué módulo ubicas aquí?\\n(${nombres.join(", ") || "escribe el nombre"})`, nombres[0] || "");
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
    "Pulsa «Ubicar módulo» y luego haz clic en el mapa para fijar su posición.";
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
    let color = "#8a8984", estado = "○ Sin lecturas todavía";
    if (d.ultima) {
      const edad = (Date.now() - new Date(d.ultima.recibido_en).getTime()) / 1000;
      if (edad <= 90) { color = "#008300"; estado = "● En línea"; }
      else { color = "#c98500"; estado = `⚠ Sin datos desde hace ${edad < 5400 ? Math.round(edad / 60) + " min" : Math.round(edad / 3600) + " h"}`; }
    }
    const u = d.ultima;
    const datos = u ? `<b>${u.oxigeno_disuelto?.toFixed(2) ?? "—"}</b> mg/L · ` +
                      `<b>${u.temperatura?.toFixed(2) ?? "—"}</b> °C · ` +
                      `<b>${u.saturacion?.toFixed(1) ?? "—"}</b> %<br>` : "";
    L.circleMarker([d.lat, d.lng], {
      radius: 9, color: "#ffffff", weight: 2, fillColor: color, fillOpacity: 0.95,
    }).bindPopup(
      `<div class="popup-dato"><strong>${d.nombre}</strong>` +
      (d.descripcion ? ` — ${d.descripcion}` : "") + `<br>${datos}${estado}<br>` +
      `<button class="popup-quitar" data-quitar="${encodeURIComponent(d.nombre)}">🗑 Quitar del mapa</button></div>`
    ).addTo(marcadores);
  }
  const sinUbicar = dispositivos.filter(d => d.lat === null);
  if (sinUbicar.length && !ubicando) {
    document.getElementById("mapa-nota").textContent =
      `Sin ubicar: ${sinUbicar.map(d => d.nombre).join(", ")} — pulsa «Ubicar módulo» y haz clic en el mapa.`;
  }
  if (!mapaAjustado && puestos.length) {
    mapaAjustado = true;
    mapa.fitBounds(L.latLngBounds(puestos.map(d => [d.lat, d.lng])).pad(0.4), { maxZoom: 15 });
  }
}

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
cargar();
cargarDispositivos();
setInterval(() => { cargar(); cargarDispositivos(); }, 10000);
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
    try:
        lat, lng = float(cuerpo["lat"]), float(cuerpo["lng"])
    except (KeyError, TypeError, ValueError):
        raise HTTPException(status_code=422, detail="lat y lng numéricos requeridos")
    descripcion = str(cuerpo.get("descripcion", "") or "")

    with _lock, db() as con:
        con.execute(
            """INSERT INTO dispositivos (nombre, lat, lng, descripcion) VALUES (?, ?, ?, ?)
               ON CONFLICT(nombre) DO UPDATE SET lat=excluded.lat, lng=excluded.lng,
                                                 descripcion=excluded.descripcion""",
            (nombre, lat, lng, descripcion),
        )
    return {"ok": True, "nombre": nombre, "lat": lat, "lng": lng}


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
