"use strict";
/* Panel de la camaronera. Una sola página con secciones (#/inicio, #/mapa…).
   Todo el estado operativo (zonas, tendencias, frescura) lo decide el servidor
   en /api/estados y /api/estado; aquí solo se pinta. */

// --- Utilidades --------------------------------------------------------------
const $ = id => document.getElementById(id);
const ZONA_HORARIA = "America/Guayaquil";
const FMT_HORA = new Intl.DateTimeFormat("es-EC", { timeZone: ZONA_HORARIA, hour: "2-digit", minute: "2-digit", hour12: false });
const FMT_HORA_S = new Intl.DateTimeFormat("es-EC", { timeZone: ZONA_HORARIA, hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });
const FMT_FECHA_HORA = new Intl.DateTimeFormat("es-EC", { timeZone: ZONA_HORARIA, day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false });
const FMT_RELOJ = new Intl.DateTimeFormat("es-EC", { timeZone: ZONA_HORARIA, weekday: "long", day: "numeric", month: "long", hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });
const fmtHora = t => FMT_HORA.format(t);
const fmtFechaHora = t => FMT_FECHA_HORA.format(t);
const num = (v, d) => v === null || v === undefined || Number.isNaN(v) ? "—" : (+v).toFixed(d);
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const cssVar = n => getComputedStyle(document.body).getPropertyValue(n).trim() || "#888";

function haceCuanto(seg) {
  if (seg === null || seg === undefined) return "";
  if (seg < 90) return `hace ${Math.max(0, Math.round(seg))} s`;
  if (seg < 5400) return `hace ${Math.round(seg / 60)} min`;
  if (seg < 172800) return `hace ${Math.round(seg / 3600)} h`;
  return `hace ${Math.round(seg / 86400)} d`;
}
function duracion(ms) {
  const m = Math.round(ms / 60000);
  if (m < 60) return `${m} min`;
  if (m < 2880) return `${(m / 60).toFixed(1)} h`;
  return `${Math.round(m / 1440)} d`;
}

const COLOR_ZONA = {
  normal: "--z-ok", aviso: "--z-aviso", critico: "--z-crit",
  sin_datos: "--tinta2", manipulacion: "--aviso", desconocida: "--tinta2",
};
const ETIQUETA_ZONA = {
  normal: "Normal", aviso: "Aviso", critico: "Crítico", sin_datos: "Sin datos",
  manipulacion: "Manipulación", desconocida: "Sin lectura",
};
const ETIQUETA_ZONA_LARGA = {
  normal: "Oxígeno normal", aviso: "Oxígeno bajo", critico: "Oxígeno crítico",
  sin_datos: "Sin datos recientes", manipulacion: "Sonda en manipulación", desconocida: "Sin lectura de oxígeno",
};
const NOMBRE_TIPO = { od_bajo: "OD bajo", od_critico: "OD crítico", sin_datos: "Sin datos" };
const COLOR_TIPO = { od_bajo: "--z-aviso", od_critico: "--z-crit", sin_datos: "--aviso" };

function pill(zona, texto) {
  return `<span class="pill" data-zona="${zona}">${texto || ETIQUETA_ZONA[zona] || zona}</span>`;
}
function tendenciaTxt(p, zona) {
  if (zona === "sin_datos") return '<span class="c-sin">sin reportar</span>';
  if (p === null || p === undefined) return "—";
  if (Math.abs(p) < 0.05) return "→ estable";
  return `<span class="${p < 0 ? "c-aviso" : "c-ok"}">${p < 0 ? "↓" : "↑"} ${Math.abs(p).toFixed(2)} mg/L/h</span>`;
}
function margenTxt(h) {
  if (h === null || h === undefined) return "—";
  return `<span class="c-aviso">~${h < 1 ? Math.round(h * 60) + " min" : h.toFixed(1) + " h"} al crítico</span>`;
}

// --- Estado global -----------------------------------------------------------
const SERIES = [
  { campo: "oxigeno_disuelto", color: "var(--s-od)",   dec: 2, unidad: "mg/L", nombre: "Oxígeno" },
  { campo: "temperatura",      color: "var(--s-temp)", dec: 2, unidad: "°C",   nombre: "Temperatura" },
  { campo: "saturacion",       color: "var(--s-sat)",  dec: 1, unidad: "%",    nombre: "Saturación" },
];
let rangoHoras = 1;
let datos = [];             // lecturas del rango, ascendentes
let piscina = "";           // dispositivo seleccionado
let umbrales = null;
let finca = {};             // dispositivo -> estado (de /api/estados)
let listaFinca = [];
let dispositivos = [];      // de /api/dispositivos
let alarmas = { activas: [], historial: [] };
let ultimaFinca = null;
try { piscina = localStorage.getItem("piscina_panel") || ""; } catch (e) {}

function nombreDe(d) {
  const e = finca[d]; if (e && e.nombre) return e.nombre;
  const r = dispositivos.find(x => x.nombre === d); return (r && r.descripcion) || d;
}

// --- Sesión y token de la app (para escribir) --------------------------------
let sesion = { protegido: false, activa: false };
async function cargarSesion() {
  try { sesion = await (await fetch("/api/sesion")).json(); } catch (e) { return; }
  $("btn-salir").hidden = !sesion.activa;
  $("caja-token").hidden = sesion.activa;   // con sesión, las escrituras ya van autorizadas
}
$("btn-salir").addEventListener("click", async () => { await fetch("/logout", { method: "POST" }); location.reload(); });
function sesionCaducada(r) {
  // Con clave, un 401 en lectura significa que la sesión expiró: a la pantalla de acceso.
  if (r.status === 401 && sesion.protegido) { location.reload(); return true; }
  return false;
}
function tokenApp() { try { return localStorage.getItem("token_panel") || ""; } catch (e) { return ""; } }
async function fetchAuth(url, opciones = {}) {
  const token = tokenApp();
  if (!token && !sesion.activa) { irA("configuracion"); mensaje("token-mensaje", "Primero guarda la clave de la app.", "error"); throw new Error("sin token"); }
  const cab = { "Content-Type": "application/json", ...(opciones.headers || {}) };
  if (token) cab["X-Auth-Token"] = token;
  const r = await fetch(url, { ...opciones, headers: cab });
  if (r.status === 401) {
    if (sesionCaducada(r)) throw new Error("sesión caducada");
    irA("configuracion"); mensaje("token-mensaje", "Clave inválida: revísala.", "error"); throw new Error("token inválido");
  }
  return r;
}
function mensaje(id, texto, clase) {
  const el = $(id); if (!el) return;
  el.textContent = texto; el.className = "mensaje " + (clase || "");
  if (texto) setTimeout(() => { if (el.textContent === texto) { el.textContent = ""; } }, 6000);
}

// --- Navegación --------------------------------------------------------------
const TITULOS = {
  inicio: ["Inicio", "Monitoreo en tiempo real del oxígeno disuelto en tus piscinas"],
  piscinas: ["Piscinas", "Todas las piscinas ordenadas por urgencia"],
  mapa: ["Mapa", "Dónde está cada módulo y en qué estado"],
  alarmas: ["Alarmas", "Activas e historial"],
  historial: ["Historial", "Lecturas crudas y exportación"],
  reportes: ["Reportes", "Resumen diario por piscina"],
  dispositivos: ["Dispositivos", "Sondas registradas y alta de nuevas"],
  configuracion: ["Configuración", "Umbrales, clave y apariencia"],
};
let paginaActual = "";
let inicial = Promise.resolve();   // primera carga de la finca: las páginas esperan a tener nombres y umbrales
function paginaDeHash() {
  const p = (location.hash || "#/inicio").replace(/^#\/?/, "").split("?")[0];
  return TITULOS[p] ? p : "inicio";
}
function irA(p) { location.hash = "#/" + p; }
function mostrarPagina() {
  const p = paginaDeHash();
  paginaActual = p;
  document.querySelectorAll("section[data-pagina]").forEach(s => { s.hidden = s.dataset.pagina !== p; });
  document.querySelectorAll("#menu a").forEach(a => a.classList.toggle("activo", a.dataset.pagina === p));
  $("titulo-pagina").textContent = TITULOS[p][0];
  $("sub-pagina").textContent = TITULOS[p][1];
  window.scrollTo({ top: 0 });
  if (p === "inicio") { if (datos.length) render(); if (mapaMini) setTimeout(() => mapaMini.invalidateSize(), 50); }
  if (p === "mapa" && mapa) setTimeout(() => { mapa.invalidateSize(); ajustarMapa(mapa); }, 50);
  inicial.then(() => {
    if (p === "historial") { cargarHistorial(); cargarTablaHistorial(); }
    if (p === "reportes") cargarResumen();
    if (p === "configuracion") { $("cfg-token").value = tokenApp(); rellenarUmbrales(); cargarTelegram(); }
  });
}
addEventListener("hashchange", mostrarPagina);

// --- Tema --------------------------------------------------------------------
function temaOscuroActivo() {
  const t = document.documentElement.dataset.theme;
  if (t) return t === "dark";
  return matchMedia("(prefers-color-scheme: dark)").matches;
}
function aplicarTema(oscuro) {
  document.documentElement.dataset.theme = oscuro ? "dark" : "light";
  try { localStorage.setItem("tema", oscuro ? "dark" : "light"); } catch (e) {}
  sincronizarTema();
  if (datos.length) render();
  pintarMarcadores();
}
function sincronizarTema() {
  const v = temaOscuroActivo();
  ["tema-oscuro", "tema-oscuro-2"].forEach(id => { $(id).checked = v; });
}
["tema-oscuro", "tema-oscuro-2"].forEach(id => $(id).addEventListener("change", ev => aplicarTema(ev.target.checked)));
sincronizarTema();

// --- Reloj y salud -----------------------------------------------------------
function actualizarReloj() {
  const t = FMT_RELOJ.format(new Date());
  const [fecha, hora] = t.split(/,\s(?=\d\d:\d\d:\d\d$)/);
  $("reloj").innerHTML = `<b>${esc(hora || t)}</b>${esc(fecha)} · (UTC−5) Ecuador`;
}
actualizarReloj(); setInterval(actualizarReloj, 1000);

function actualizarSalud(lista) {
  const el = $("salud"), nota = $("salud-nota");
  if (!lista.length) { el.textContent = "○ Sin sondas todavía"; el.dataset.nivel = ""; nota.textContent = "Agente de la finca"; return; }
  const mudas = lista.filter(p => p.zona === "sin_datos");
  if (mudas.length) {
    el.textContent = `⚠ ${mudas.length === 1 ? mudas[0].nombre + " sin datos" : mudas.length + " sondas sin datos"}`;
    el.dataset.nivel = "mal";
    nota.textContent = "Revisa el módulo o la red";
  } else {
    el.textContent = "● Agente conectado";
    el.dataset.nivel = "ok";
    nota.textContent = lista.length === 1 ? "1 sonda reportando" : `${lista.length} sondas reportando`;
  }
}

// --- Franja de estado --------------------------------------------------------
function actualizarFranja(resumen, lista) {
  const f = $("franja");
  const r = resumen || {};
  let nivel = "ok", icono = "✔", titulo = "Todo en normalidad", sub = "No hay alarmas críticas en este momento.";
  if (!lista.length) { nivel = ""; icono = "○"; titulo = "Sin sondas reportando"; sub = "Cuando el agente envíe la primera lectura aparecerá aquí."; }
  else if (r.critico) { nivel = "crit"; icono = "‼"; titulo = `${r.critico} ${r.critico === 1 ? "piscina en CRÍTICO" : "piscinas en CRÍTICO"}`; sub = "Prende los aireadores y revisa la piscina ahora."; }
  else if (r.aviso) { nivel = "aviso"; icono = "⚠"; titulo = `${r.aviso} ${r.aviso === 1 ? "piscina en aviso" : "piscinas en aviso"}`; sub = "Oxígeno bajo; vigila la tendencia."; }
  else if (r.sin_datos) { nivel = "aviso"; icono = "⚠"; titulo = `${r.sin_datos} ${r.sin_datos === 1 ? "sonda sin datos" : "sondas sin datos"}`; sub = "Las demás están en normalidad."; }
  else if (r.manipulacion) { sub = `${r.manipulacion === 1 ? "Una sonda" : r.manipulacion + " sondas"} en manipulación; el resto normal.`; }
  f.dataset.nivel = nivel; $("franja-icono").textContent = icono;
  $("franja-titulo").textContent = titulo; $("franja-sub").textContent = sub;
  $("franja-lecturas").textContent = r.lecturas_hora ?? "—";
  const frescas = lista.map(p => p.edad_segundos).filter(v => v !== null && v !== undefined);
  $("franja-ultima").textContent = frescas.length ? haceCuanto(Math.min(...frescas)) : "—";
}

// --- Tarjetas y tablas de piscinas ------------------------------------------
function chispa(valores, color) {
  if (!valores || valores.length < 2) return "";
  const w = 100, h = 28, min = Math.min(...valores), max = Math.max(...valores);
  const rango = (max - min) || 1;
  const d = valores.map((v, i) =>
    `${i ? "L" : "M"}${(i / (valores.length - 1) * w).toFixed(1)},${(h - 2 - (v - min) / rango * (h - 5)).toFixed(1)}`).join("");
  return `<svg viewBox="0 0 ${w} ${h}" preserveAspectRatio="none" aria-hidden="true">` +
         `<path d="${d}" fill="none" stroke="${color}" stroke-width="1.6" vector-effect="non-scaling-stroke" stroke-linejoin="round"/></svg>`;
}
function htmlRejilla(lista) {
  if (!lista.length) return '<p class="vacio">Sin sondas reportando todavía.</p>';
  return lista.map(p => {
    const color = cssVar(COLOR_ZONA[p.zona] || "--tinta2");
    return `<button class="piscina${p.dispositivo === piscina ? " activa" : ""}" data-zona="${p.zona}" data-ir="${encodeURIComponent(p.dispositivo)}">` +
      `<div class="cab"><span class="nom" title="${esc(p.dispositivo)}">${esc(p.nombre)}</span>${pill(p.zona)}</div>` +
      `<div class="cuerpo"><span class="od">${num(p.oxigeno_disuelto, 2)}<small>mg/L</small></span>${chispa(p.chispa, color)}</div>` +
      `<div class="tend">${tendenciaTxt(p.pendiente_od_hora, p.zona)}</div>` +
      `<div class="pie"><span>T° ${num(p.temperatura, 1)} °C · Sat ${num(p.saturacion, 1)} %</span>` +
      `<span>${p.horas_a_critico !== null && p.horas_a_critico !== undefined ? margenTxt(p.horas_a_critico) : haceCuanto(p.edad_segundos)}</span></div></button>`;
  }).join("");
}
function htmlResumenFinca(r) {
  if (!r || !r.total) return "";
  const t = [`<b>${r.total}</b> ${r.total === 1 ? "piscina" : "piscinas"}`];
  if (r.critico) t.push(`<span class="c-critico">${r.critico} en crítico</span>`);
  if (r.aviso) t.push(`<span class="c-aviso">${r.aviso} en aviso</span>`);
  if (r.sin_datos) t.push(`<span class="c-sin">${r.sin_datos} sin datos</span>`);
  if (r.manipulacion) t.push(`<span class="c-sin">${r.manipulacion} en manipulación</span>`);
  if (r.normal) t.push(`<span>${r.normal} normales</span>`);
  return t.join(" · ");
}
function htmlTablaPiscinas(lista, conMargen) {
  if (!lista.length) return `<tr><td colspan="8" class="vacio">Sin sondas reportando todavía.</td></tr>`;
  return lista.map(p =>
    `<tr class="fila-ir" data-ir="${encodeURIComponent(p.dispositivo)}"><td><span class="punto" style="background:${cssVar(COLOR_ZONA[p.zona])}"></span> ${esc(p.nombre)}</td>` +
    `<td class="${p.zona === "critico" ? "c-critico" : p.zona === "aviso" ? "c-aviso" : ""}">${num(p.oxigeno_disuelto, 2)}</td>` +
    `<td>${num(p.temperatura, 1)}</td><td>${num(p.saturacion, 1)}</td><td>${tendenciaTxt(p.pendiente_od_hora, p.zona)}</td>` +
    (conMargen ? `<td>${margenTxt(p.horas_a_critico)}</td>` : "") +
    `<td>${pill(p.zona)}</td><td>${haceCuanto(p.edad_segundos)}</td></tr>`).join("");
}
function rellenarSelector(sel, incluirTodas) {
  const nombres = [...new Set([...listaFinca.map(p => p.dispositivo), ...dispositivos.map(d => d.nombre)])];
  const actual = sel.value;
  sel.innerHTML = (incluirTodas ? '<option value="">Todas las piscinas</option>' : "") +
    nombres.map(n => `<option value="${esc(n)}">${esc(nombreDe(n))}</option>`).join("");
  if (nombres.includes(actual) || (incluirTodas && actual === "")) sel.value = actual;
  else if (!incluirTodas && nombres.includes(piscina)) sel.value = piscina;
}

async function cargarFinca() {
  let j;
  try { const r = await fetch("/api/estados"); if (sesionCaducada(r)) return; j = await r.json(); } catch (e) { return; }
  listaFinca = j.piscinas || [];
  finca = Object.fromEntries(listaFinca.map(p => [p.dispositivo, p]));
  ultimaFinca = new Date();
  actualizarSalud(listaFinca);
  actualizarFranja(j.resumen, listaFinca);

  // Si no hay piscina elegida (o ya no existe), la más urgente.
  if (!finca[piscina] && listaFinca.length) { piscina = listaFinca[0].dispositivo; guardarPiscina(); }

  $("resumen-finca").innerHTML = htmlResumenFinca(j.resumen);
  $("resumen-finca-2").innerHTML = htmlResumenFinca(j.resumen);
  $("rejilla").innerHTML = htmlRejilla(listaFinca);
  $("rejilla-2").innerHTML = htmlRejilla(listaFinca);
  $("tabla-piscinas").innerHTML = htmlTablaPiscinas(listaFinca, false);
  $("tabla-piscinas-2").innerHTML = htmlTablaPiscinas(listaFinca, true);

  const sel = $("piscinas");
  sel.hidden = listaFinca.length < 2;
  rellenarSelector(sel, false);
  rellenarSelector($("hist-piscina"), true);
  rellenarSelector($("rep-piscina"), true);
  rellenarSelector($("umb-piscina"), false);
  if (paginaActual === "configuracion" && $("umb-aviso").value === "") rellenarUmbrales();
}
function guardarPiscina() { try { localStorage.setItem("piscina_panel", piscina); } catch (e) {} }
function elegirPiscina(nombre, ir) {
  piscina = nombre; guardarPiscina();
  $("rejilla").innerHTML = htmlRejilla(listaFinca);
  $("rejilla-2").innerHTML = htmlRejilla(listaFinca);
  $("piscinas").value = piscina;
  cargarEstado().then(cargar);
  if (ir) irA("inicio");
}
document.addEventListener("click", ev => {
  const b = ev.target.closest("[data-ir]");
  if (!b) return;
  elegirPiscina(decodeURIComponent(b.dataset.ir), paginaActual !== "inicio");
});
$("piscinas").addEventListener("change", ev => elegirPiscina(ev.target.value, false));

// --- Detalle: estado y tendencia --------------------------------------------
async function cargarEstado() {
  let e;
  try { e = await (await fetch("/api/estado" + (piscina ? `?dispositivo=${encodeURIComponent(piscina)}` : ""))).json(); }
  catch (err) { return; }
  const franja = $("estado"), tarjeta = $("tarjeta-od"), tend = $("tendencia");
  $("det-nombre").textContent = piscina ? nombreDe(piscina) : "Sin sonda";
  if (!e.hay_datos) {
    franja.dataset.zona = "desconocida"; $("estado-txt").textContent = "Sin datos";
    $("det-meta").textContent = "Todavía no llega ninguna lectura."; tend.innerHTML = ""; $("umbrales-mini").innerHTML = "";
    return;
  }
  umbrales = e.umbrales;
  franja.dataset.zona = e.zona; $("estado-txt").textContent = ETIQUETA_ZONA_LARGA[e.zona] || e.zona;
  tarjeta.dataset.zona = e.zona;
  $("det-meta").textContent = `Sonda: ${e.dispositivo} · Última lectura: ${e.medido_en ? fmtFechaHora(new Date(e.medido_en)) : "—"} (${haceCuanto(e.edad_segundos)})`;
  $("v-od").innerHTML = `${num(e.oxigeno_disuelto, 2)}<span>mg/L</span>`;
  $("v-temp").innerHTML = `${num(e.temperatura, 2)}<span>°C</span>`;
  $("v-sat").innerHTML = `${num(e.saturacion, 1)}<span>%</span>`;

  const partes = [];
  const p = e.pendiente_od_hora;
  if (p === null || p === undefined) partes.push("<span>Tendencia: pocas lecturas todavía</span>");
  else if (Math.abs(p) < 0.05) partes.push("<span>→ estable (última hora)</span>");
  else partes.push(`<span>${p < 0 ? "↓" : "↑"} ${Math.abs(p).toFixed(2)} mg/L por hora</span>`);
  if (e.horas_a_critico !== null && e.horas_a_critico !== undefined) {
    const h = e.horas_a_critico;
    partes.push(`<span class="margen">Llega al crítico en ~${h < 1 ? Math.round(h * 60) + " min" : h.toFixed(1) + " h"}</span>`);
  }
  tend.innerHTML = partes.join("");
  $("umbrales-mini").innerHTML = umbrales
    ? `<span><span class="punto" style="background:var(--z-aviso)"></span> Aviso: &lt; ${umbrales.od_aviso} mg/L</span>` +
      `<span><span class="punto" style="background:var(--z-crit)"></span> Crítico: &lt; ${umbrales.od_critico} mg/L</span>`
    : "";
}

// --- Lecturas y gráficas -----------------------------------------------------
async function cargar() {
  const desde = new Date(Date.now() - rangoHoras * 3600e3).toISOString();
  try {
    const r = await fetch(`/api/readings?limit=20000&since=${encodeURIComponent(desde)}` +
      (piscina ? `&dispositivo=${encodeURIComponent(piscina)}` : ""));
    const j = await r.json();
    datos = (j.lecturas || []).filter(l => l.oxigeno_disuelto !== null || l.temperatura !== null || l.saturacion !== null).reverse();
    datos.forEach(l => { l.t = new Date(l.recibido_en); });
    render();
  } catch (e) {
    $("meta").innerHTML = '<span class="alerta">⚠ No se pudo consultar la API. ¿El servidor está corriendo?</span>';
  }
}

function render() {
  const ult = datos[datos.length - 1];
  for (const s of SERIES) {
    const id = { temperatura: "a-temp", saturacion: "a-sat" }[s.campo];
    if (id) $(id).textContent = ult && ult[s.campo] !== null ? `${num(ult[s.campo], s.dec)} ${s.unidad}` : "";
  }
  const meta = $("meta");
  if (!ult) meta.textContent = "Sin lecturas en este rango todavía.";
  else {
    const edad = (Date.now() - ult.t.getTime()) / 1000;
    meta.innerHTML = `${datos.length} lecturas en ${rangoHoras} h` +
      (edad > 180 ? ` · <span class="alerta">⚠ sin datos nuevos ${haceCuanto(edad)}</span>` : "");
  }
  for (const s of SERIES) dibujar(s);
}

function escala(puntos, campo, h, padT, padB, incluir, noNegativo) {
  const vals = puntos.map(p => p[campo]).filter(v => v !== null && v !== undefined);
  let vmin = Math.min(...vals), vmax = Math.max(...vals);
  const minReal = vmin;
  if (vmin === vmax) { vmin -= 1; vmax += 1; }
  const margen = (vmax - vmin) * 0.12;
  vmin -= margen; vmax += margen;
  // Solo se baja el eje hasta el umbral cuando el agua ya se acerca al aviso.
  for (const v of (incluir || [])) {
    if (v !== null && v !== undefined && v < minReal && minReal - v <= 2) vmin = Math.min(vmin, v - 0.15);
  }
  // Oxígeno y saturación no son negativos: un eje que baja de cero confunde.
  if (noNegativo && minReal >= 0) vmin = Math.max(vmin, 0);
  return { vmin, vmax, y: v => padT + (1 - (v - vmin) / (vmax - vmin)) * (h - padT - padB) };
}

function dibujar(serie) {
  const cont = document.querySelector(`[data-grafica="${serie.campo}"]`);
  if (!cont) return;
  const puntos = datos.filter(p => p[serie.campo] !== null && p[serie.campo] !== undefined)
    .map(p => ({ t: p.t, v: p[serie.campo], manip: !!p.manipulacion }));
  const t1 = Date.now();
  dibujarGrafica(cont, serie, puntos, t1 - rangoHoras * 3600e3, t1, { alto: serie.campo === "oxigeno_disuelto" ? 230 : 150 });
}

// Gráfica genérica: puntos {t: Date, v, min?, max?, manip?}. Con min/max pinta
// la banda mín–máx del cubo (rangos largos); sin ellos, solo la línea.
function dibujarGrafica(cont, serie, puntos, t0, t1, op = {}) {
  if (puntos.length < 2) { cont.innerHTML = '<p class="vacio">Aún no hay suficientes lecturas para la gráfica.</p>'; cont._puntos = null; return; }
  const esOD = serie.campo === "oxigeno_disuelto";
  const w = Math.max(cont.clientWidth || 560, 280), h = op.alto || 150;
  const padL = 46, padR = 12, padT = 8, padB = 18;
  const horas = (t1 - t0) / 3600e3;
  const x = t => padL + (t - t0) / (t1 - t0) * (w - padL - padR);
  const forzar = esOD && umbrales ? [umbrales.od_aviso, umbrales.od_critico] : null;
  const conBanda = puntos.some(p => p.min !== null && p.min !== undefined);
  const paraEscala = conBanda ? puntos.flatMap(p => [{ v: p.v }, { v: p.min }, { v: p.max }]) : puntos;
  const { vmin, vmax, y } = escala(paraEscala, "v", h, padT, padB, forzar, serie.campo !== "temperatura");

  let svg = `<svg viewBox="0 0 ${w} ${h}" role="img" aria-label="${serie.nombre}">`;

  // Franja nocturna (18:00 a 06:00 hora de Ecuador): ahí se desploma el oxígeno.
  if (horas >= 6 && horas <= 24 * 8) {
    const paso = 15 * 60e3;
    let ini = null;
    for (let t = t0; t <= t1 + paso; t += paso) {
      const hora = +FMT_HORA.format(new Date(t)).split(":")[0];
      const noche = hora >= 18 || hora < 6;
      if (noche && ini === null) ini = t;
      if ((!noche || t > t1) && ini !== null) {
        const x0 = Math.max(padL, x(ini)), x1 = Math.min(w - padR, x(Math.min(t, t1)));
        if (x1 > x0) svg += `<rect class="banda-noche" x="${x0.toFixed(1)}" y="${padT}" width="${(x1 - x0).toFixed(1)}" height="${h - padT - padB}"/>`;
        ini = null;
      }
    }
  }
  // Tramos con la sonda fuera del agua (manipulación): sombreados.
  {
    const ancho = op.paso ? op.paso * 1000 : 0;
    let ini = null, fin = null;
    for (let i = 0; i <= puntos.length; i++) {
      const p = puntos[i], m = p && p.manip;
      if (m && ini === null) ini = p.t.getTime();
      if (m) fin = p.t.getTime() + ancho;
      if (!m && ini !== null) {
        const x0 = Math.max(padL, x(ini)), x1 = Math.min(w - padR, x(fin));
        if (x1 > x0 + 0.5) {
          svg += `<rect class="banda-manip" x="${x0.toFixed(1)}" y="${padT}" width="${(x1 - x0).toFixed(1)}" height="${h - padT - padB}"/>`;
          if (esOD && x1 - x0 > 46) svg += `<text class="etq-manip" x="${((x0 + x1) / 2).toFixed(1)}" y="${padT + 9}" text-anchor="middle">manipulación</text>`;
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
    if (ya < piso) svg += `<rect class="banda-aviso" x="${padL}" y="${ya.toFixed(1)}" width="${w - padL - padR}" height="${(piso - ya).toFixed(1)}"/>`;
    if (yc < piso) svg += `<rect class="banda-crit" x="${padL}" y="${yc.toFixed(1)}" width="${w - padL - padR}" height="${(piso - yc).toFixed(1)}"/>`;
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
  const nEtq = horas > 48 ? 5 : 3;
  for (let i = 0; i < nEtq; i++) {
    const f = 0.08 + (0.84 * i) / (nEtq - 1);
    const t = new Date(t0 + (t1 - t0) * f);
    svg += `<text class="ejex" x="${x(t).toFixed(1)}" y="${h - 4}" text-anchor="middle">${horas > 48 ? fmtFechaHora(t) : fmtHora(t)}</text>`;
  }
  // La línea se corta en los huecos: un corte de dos horas no es agua estable.
  const deltas = puntos.slice(1).map((p, i) => p.t - puntos[i].t).sort((a, b) => a - b);
  const mediana = deltas[Math.floor(deltas.length / 2)] || 15000;
  const corte = Math.max(3 * mediana, 60000);
  if (conBanda) {
    // Banda mín–máx por tramo continuo.
    let tramo = [];
    const cerrar = () => {
      if (tramo.length < 2) { tramo = []; return; }
      const arriba = tramo.map(p => `${x(p.t.getTime()).toFixed(1)},${y(p.max).toFixed(1)}`);
      const abajo = tramo.slice().reverse().map(p => `${x(p.t.getTime()).toFixed(1)},${y(p.min).toFixed(1)}`);
      svg += `<polygon class="banda-minmax" fill="${serie.color}" points="${arriba.concat(abajo).join(" ")}"/>`;
      tramo = [];
    };
    let tPrev = null;
    for (const p of puntos) {
      const t = p.t.getTime();
      if (tPrev !== null && t - tPrev > corte) cerrar();
      if (p.min !== null && p.min !== undefined) tramo.push(p); else cerrar();
      tPrev = t;
    }
    cerrar();
  }
  let d = "", tPrev = null;
  for (const p of puntos) {
    const t = p.t.getTime();
    d += `${tPrev === null || t - tPrev > corte ? "M" : "L"}${x(t).toFixed(1)},${y(p.v).toFixed(1)}`;
    tPrev = t;
  }
  svg += `<path d="${d}" fill="none" stroke="${serie.color}" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>`;
  const fin = puntos[puntos.length - 1];
  svg += `<circle cx="${x(fin.t.getTime()).toFixed(1)}" cy="${y(fin.v).toFixed(1)}" r="3.5" fill="${serie.color}"/>`;
  svg += `<line class="cruz" y1="${padT}" y2="${h - padB}" x1="-9" x2="-9" data-cruz hidden/>`;
  svg += `<circle r="4" fill="none" stroke="${serie.color}" stroke-width="2" data-foco hidden cx="-9" cy="-9"/>`;
  svg += "</svg>";
  cont.innerHTML = svg;
  cont._puntos = puntos; cont._x = x; cont._y = y; cont._serie = serie; cont._largo = horas > 48;
  const svgEl = cont.firstChild;
  const grupo = cont.dataset.grafica !== undefined ? "[data-grafica]" : "[data-grafica-h]";
  svgEl.addEventListener("mousemove", ev => {
    const caja = svgEl.getBoundingClientRect();
    mostrarCruz(t0 + (ev.clientX - caja.left) / caja.width * (t1 - t0), ev.clientX, ev.clientY, grupo);
  });
  svgEl.addEventListener("mouseleave", ocultarCruz);
}
function masCercano(puntos, t) {
  let mejor = puntos[0], dist = Infinity;
  for (const p of puntos) { const d = Math.abs(p.t.getTime() - t); if (d < dist) { dist = d; mejor = p; } }
  return mejor;
}
function mostrarCruz(t, cx, cy, grupo = "[data-grafica]") {
  const tooltip = $("tooltip");
  let filas = "", hora = "";
  for (const cont of document.querySelectorAll(grupo)) {
    if (!cont._puntos) continue;
    const s = cont._serie;
    const p = masCercano(cont._puntos, t);
    hora = cont._largo ? fmtFechaHora(p.t) : FMT_HORA_S.format(p.t);
    const xx = cont._x(p.t.getTime()).toFixed(1);
    const cruz = cont.querySelector("[data-cruz]"), foco = cont.querySelector("[data-foco]");
    cruz.setAttribute("x1", xx); cruz.setAttribute("x2", xx); cruz.hidden = false;
    foco.setAttribute("cx", xx); foco.setAttribute("cy", cont._y(p.v).toFixed(1)); foco.hidden = false;
    const rango = p.min !== null && p.min !== undefined ? ` <small>(${p.min.toFixed(s.dec)}–${p.max.toFixed(s.dec)})</small>` : "";
    filas += `<div class="fila"><span class="punto" style="background:${s.color}"></span>${s.nombre}<b>${p.v.toFixed(s.dec)} ${s.unidad}${rango}</b></div>`;
  }
  tooltip.innerHTML = `<div class="fila"><b>${hora}</b></div>` + filas;
  tooltip.hidden = false;
  const dx = cx + 14 + 180 > innerWidth ? -14 - tooltip.offsetWidth : 14;
  tooltip.style.left = (cx + dx) + "px";
  tooltip.style.top = Math.min(cy + 12, innerHeight - tooltip.offsetHeight - 8) + "px";
}
function ocultarCruz() {
  $("tooltip").hidden = true;
  document.querySelectorAll("[data-cruz],[data-foco]").forEach(e => { e.hidden = true; });
}
$("rangos").addEventListener("click", ev => {
  const b = ev.target.closest("button[data-rango]");
  if (!b) return;
  rangoHoras = +b.dataset.rango;
  document.querySelectorAll("#rangos button").forEach(x => x.classList.toggle("activo", x === b));
  cargar();
});
addEventListener("resize", () => { if (paginaActual === "inicio" && datos.length) render(); if (paginaActual === "historial") cargarHistorial(); });

// --- Historial: serie agregada + tabla + CSV ---------------------------------
let histHoras = 24, histDesde = null, histHasta = null;
const CAMPO_SERIE = { oxigeno_disuelto: "od", temperatura: "temp", saturacion: "sat" };
function localAIso(valor) {   // datetime-local (hora Ecuador, sin DST) -> ISO UTC
  return valor ? new Date(valor + ":00-05:00").toISOString() : "";
}
function isoALocal(d) {       // Date -> valor de datetime-local en hora Ecuador
  const partes = Object.fromEntries(new Intl.DateTimeFormat("en-CA", { timeZone: ZONA_HORARIA, year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false })
    .formatToParts(d).map(p => [p.type, p.value]));
  return `${partes.year}-${partes.month}-${partes.day}T${partes.hour === "24" ? "00" : partes.hour}:${partes.minute}`;
}
function rangoHistorial() {
  if (histHoras) { const t1 = Date.now(); return [t1 - histHoras * 3600e3, t1]; }
  return [histDesde, histHasta];
}
async function cargarHistorial() {
  const [t0, t1] = rangoHistorial();
  if (!t0 || !t1 || t1 <= t0) { $("hist-meta").textContent = "Elige un rango válido."; return; }
  const sel = $("hist-piscina").value;
  const filtro = sel ? `&dispositivo=${encodeURIComponent(sel)}` : "";
  let j;
  try {
    j = await (await fetch(`/api/serie?desde=${new Date(t0).toISOString()}&hasta=${new Date(t1).toISOString()}${filtro}`)).json();
  } catch (e) { $("hist-meta").textContent = "No se pudo consultar la API."; return; }
  if (j.detail) { $("hist-meta").textContent = j.detail; return; }
  if (sel && finca[sel]) umbrales = finca[sel].umbrales;
  const n = j.puntos.reduce((a, p) => a + p.n, 0);
  $("hist-meta").textContent = `${n} lecturas · ${j.puntos.length} cubos de ${j.paso >= 3600 ? (j.paso / 3600).toFixed(1) + " h" : j.paso / 60 + " min"} · ${fmtFechaHora(new Date(t0))} → ${fmtFechaHora(new Date(t1))}`;
  for (const s of SERIES) {
    const k = CAMPO_SERIE[s.campo];
    const puntos = j.puntos.filter(p => p[k] !== null || p.manip > 0).map(p => ({
      t: new Date(p.t), v: p[k], min: p[k + "_min"], max: p[k + "_max"], manip: p.manip > 0 && p[k] === null,
    })).filter(p => p.v !== null || p.manip);
    // Un cubo solo con manipulación no tiene valor: se sombrea, pero no se une a la línea.
    const conValor = puntos.filter(p => p.v !== null);
    const cont = document.querySelector(`[data-grafica-h="${s.campo}"]`);
    dibujarGrafica(cont, s, conValor.length >= 2 ? marcarManip(conValor, puntos) : [], t0, t1, { alto: s.campo === "oxigeno_disuelto" ? 230 : 150, paso: j.paso });
  }
  $("btn-csv").href = `/api/export.csv?since=${encodeURIComponent(new Date(t0).toISOString())}&hasta=${encodeURIComponent(new Date(t1).toISOString())}${filtro}`;
}
function marcarManip(conValor, todos) {
  // Inserta los cubos de manipulación (sin valor) como marcas para el sombreado,
  // copiando el último valor conocido para que la escala no cambie.
  const salida = [];
  let ultimo = conValor[0].v;
  for (const p of todos) {
    if (p.v !== null) { ultimo = p.v; salida.push(p); }
    else salida.push({ ...p, v: ultimo, min: null, max: null, manip: true });
  }
  return salida;
}
$("hist-rangos").addEventListener("click", ev => {
  const b = ev.target.closest("button[data-hist]"); if (!b) return;
  histHoras = +b.dataset.hist;
  document.querySelectorAll("#hist-rangos button").forEach(x => x.classList.toggle("activo", x === b));
  $("hist-personalizado").hidden = histHoras !== 0;
  if (!histHoras) {
    if (!$("hist-desde").value) { $("hist-desde").value = isoALocal(new Date(Date.now() - 2 * 86400e3)); $("hist-hasta").value = isoALocal(new Date()); }
    $("hist-aplicar").click();
  } else cargarHistorial();
});
$("hist-aplicar").addEventListener("click", () => {
  histDesde = Date.parse(localAIso($("hist-desde").value)); histHasta = Date.parse(localAIso($("hist-hasta").value));
  cargarHistorial();
});
async function cargarTablaHistorial() {
  const sel = $("hist-piscina").value;
  const filtro = sel ? `&dispositivo=${encodeURIComponent(sel)}` : "";
  let j;
  try { j = await (await fetch(`/api/readings?limit=40${filtro}`)).json(); } catch (e) { return; }
  $("tabla").innerHTML = (j.lecturas || []).map(l => {
    const fallo = l.oxigeno_disuelto === null && l.temperatura === null && l.saturacion === null;
    return `<tr><td>${fmtFechaHora(new Date(l.recibido_en))}</td><td>${esc(nombreDe(l.dispositivo))}</td>` +
      `<td>${num(l.oxigeno_disuelto, 2)}</td><td>${num(l.temperatura, 2)}</td><td>${num(l.saturacion, 1)}</td>` +
      `<td>${fallo ? '<span class="pill gris">sin respuesta</span>' : l.manipulacion ? '<span class="pill" data-zona="manipulacion">manipulación</span>' : ""}</td></tr>`;
  }).join("") || '<tr><td colspan="6" class="vacio">Sin lecturas todavía.</td></tr>';
}
$("hist-piscina").addEventListener("change", () => { cargarHistorial(); cargarTablaHistorial(); });

// --- Reportes: resumen diario ------------------------------------------------
let reporteDias = 7;
async function cargarResumen() {
  const sel = $("rep-piscina").value;
  const filtro = sel ? `&dispositivo=${encodeURIComponent(sel)}` : "";
  let j;
  try { j = await (await fetch(`/api/stats?dias=${reporteDias}${filtro}`)).json(); } catch (e) { return; }
  $("resumen").innerHTML = j.dias.map(d => {
    const u = finca[d.dispositivo] && finca[d.dispositivo].umbrales;
    const bajo = d.od_min !== null && u && d.od_min < u.od_aviso;
    return `<tr><td>${d.fecha}</td><td>${esc(nombreDe(d.dispositivo))}</td><td>${d.n}</td>` +
      `<td class="${bajo ? "c-critico" : ""}">${num(d.od_min, 2)}</td><td>${num(d.od_max, 2)}</td><td>${num(d.od_prom, 2)}</td>` +
      `<td>${num(d.temp_min, 1)}</td><td>${num(d.temp_max, 1)}</td></tr>`;
  }).join("") || '<tr><td colspan="8" class="vacio">Sin datos todavía.</td></tr>';
  const desde = new Date(Date.now() - reporteDias * 86400e3).toISOString();
  $("btn-csv-2").href = `/api/export.csv?since=${encodeURIComponent(desde)}${filtro}`;
}
$("rep-piscina").addEventListener("change", cargarResumen);
$("rep-dias").addEventListener("click", ev => {
  const b = ev.target.closest("button[data-dias]"); if (!b) return;
  reporteDias = +b.dataset.dias;
  document.querySelectorAll("#rep-dias button").forEach(x => x.classList.toggle("activo", x === b));
  cargarResumen();
});

// --- Alarmas -----------------------------------------------------------------
function htmlActivas(activas) {
  return "<strong>⚠ Alarma activa</strong><br>" + activas.map(a =>
    `<span class="punto" style="background:${cssVar(COLOR_TIPO[a.tipo] || "--tinta2")}"></span> ${NOMBRE_TIPO[a.tipo] || a.tipo} en <strong>${esc(nombreDe(a.dispositivo))}</strong>` +
    (a.valor !== null ? ` · ${a.valor.toFixed(2)} mg/L` : "") + ` (desde ${fmtFechaHora(new Date(a.iniciada_en))})`).join("<br>");
}
async function cargarAlarmas() {
  let j;
  try { j = await (await fetch("/api/alarmas?limit=200")).json(); } catch (e) { return; }
  alarmas = j;
  const n = j.activas.length;
  for (const id of ["conteo-alarmas", "campana-n"]) { $(id).hidden = !n; $(id).textContent = n; }
  for (const id of ["alarmas", "alarmas-2"]) { $(id).hidden = !n; if (n) $(id).innerHTML = htmlActivas(j.activas); }
  const fila = (a, completa) => {
    const ini = new Date(a.iniciada_en), fin = a.resuelta_en ? new Date(a.resuelta_en) : null;
    const tipo = `<span class="punto" style="background:${cssVar(COLOR_TIPO[a.tipo] || "--tinta2")}"></span> ${NOMBRE_TIPO[a.tipo] || a.tipo}`;
    const estado = fin ? '<span class="pill resuelta">Resuelta</span>' : '<span class="pill activa">Activa</span>';
    return completa
      ? `<tr><td>${fmtFechaHora(ini)}</td><td>${esc(nombreDe(a.dispositivo))}</td><td>${tipo}</td><td>${a.valor !== null ? a.valor.toFixed(2) : "—"}</td>` +
        `<td>${fin ? fmtFechaHora(fin) : "—"}</td><td>${duracion((fin || new Date()) - ini)}</td><td>${estado}</td></tr>`
      : `<tr><td>${fmtFechaHora(ini)}</td><td>${esc(nombreDe(a.dispositivo))}</td><td>${tipo}</td><td>${a.valor !== null ? a.valor.toFixed(2) : "—"}</td><td>${estado}</td></tr>`;
  };
  $("tabla-alarmas-inicio").innerHTML = j.historial.slice(0, 6).map(a => fila(a, false)).join("") || '<tr><td colspan="5" class="vacio">Sin alarmas registradas.</td></tr>';
  $("tabla-alarmas").innerHTML = j.historial.map(a => fila(a, true)).join("") || '<tr><td colspan="7" class="vacio">Sin alarmas registradas.</td></tr>';
}

// --- Mapas (mini en Inicio, completo en Mapa) --------------------------------
let mapa = null, mapaMini = null, marcadores = null, marcadoresMini = null;
let ajustados = new Set(), ubicando = false;

function crearMapa(id) {
  if (typeof L === "undefined") { $(id).innerHTML = '<p class="vacio">No se pudo cargar el mapa (¿sin internet?).</p>'; return null; }
  const m = L.map(id).setView([-1.8, -78.5], 6);   // Ecuador
  const satelite = L.layerGroup([
    L.tileLayer("https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
      { maxZoom: 19, attribution: "Imágenes © Esri, Maxar, Earthstar Geographics" }),
    L.tileLayer("https://server.arcgisonline.com/ArcGIS/rest/services/Reference/World_Boundaries_and_Places/MapServer/tile/{z}/{y}/{x}",
      { maxZoom: 19, className: "capa-etiquetas", pane: "overlayPane" }),
  ]);
  const calles = L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png",
    { maxZoom: 19, attribution: "© OpenStreetMap", className: "capa-osm" });
  let base = "satelite";
  try { base = localStorage.getItem("mapa_base") || "satelite"; } catch (e) {}
  (base === "calles" ? calles : satelite).addTo(m);
  L.control.layers({ "Satélite": satelite, "Mapa": calles }, null, { position: "topright", collapsed: id === "mapa-mini" }).addTo(m);
  m.on("baselayerchange", ev => { try { localStorage.setItem("mapa_base", ev.name === "Mapa" ? "calles" : "satelite"); } catch (e) {} });
  return m;
}
function iniciarMapas() {
  mapaMini = crearMapa("mapa-mini");
  mapa = crearMapa("mapa");
  if (mapaMini) marcadoresMini = L.layerGroup().addTo(mapaMini);
  if (mapa) {
    marcadores = L.layerGroup().addTo(mapa);
    mapa.on("click", async ev => {
      if (!ubicando) return;
      await ubicarModulo($("ubicar-nombre").value, ev.latlng.lat, ev.latlng.lng, $("ubicar-descripcion").value);
      terminarUbicar();
    });
  }
  $("btn-ubicar").addEventListener("click", () => ubicando ? terminarUbicar() : empezarUbicar());
  $("btn-cancelar-ubicar").addEventListener("click", terminarUbicar);
  $("ubicar-nombre").addEventListener("change", () => {
    const d = dispositivos.find(x => x.nombre === $("ubicar-nombre").value);
    $("ubicar-descripcion").value = (d && d.descripcion) || "";
  });
  $("btn-gps").addEventListener("click", () => {
    if (!navigator.geolocation) { $("mapa-nota-2").textContent = "Este navegador no soporta geolocalización."; return; }
    if (!ubicando) empezarUbicar();
    $("mapa-nota").textContent = "Obteniendo tu ubicación…";
    navigator.geolocation.getCurrentPosition(async pos => {
      const { latitude, longitude, accuracy } = pos.coords;
      const nombre = $("ubicar-nombre").value;
      if (!nombre) { $("mapa-nota").textContent = "Elige el módulo primero."; return; }
      if (!confirm(`¿Ubicar «${nombreDe(nombre)}» en tu posición actual? (precisión ±${Math.round(accuracy)} m)`)) return;
      const ok = await ubicarModulo(nombre, latitude, longitude, $("ubicar-descripcion").value);
      if (ok) mapa.setView([latitude, longitude], Math.max(mapa.getZoom(), 16));
      terminarUbicar();
    }, err => {
      const razones = { 1: "Permiso denegado: habilita la ubicación para este sitio.", 2: "Posición no disponible.", 3: "Tiempo de espera agotado." };
      $("mapa-nota").textContent = "No se pudo obtener la ubicación: " + (razones[err.code] || err.message) +
        (location.protocol === "http:" && location.hostname !== "localhost" ? " (El GPS del navegador solo funciona con HTTPS o en localhost.)" : "");
    }, { enableHighAccuracy: true, timeout: 15000, maximumAge: 0 });
  });
}
function empezarUbicar(nombre) {
  ubicando = true;
  $("btn-ubicar").classList.add("activo");
  $("mapa").classList.add("ubicando");
  $("form-ubicar").hidden = false;
  const sel = $("ubicar-nombre");
  sel.innerHTML = dispositivos.map(d => `<option value="${esc(d.nombre)}">${esc(nombreDe(d.nombre))}${d.lat === null ? " (sin ubicar)" : ""}</option>`).join("");
  if (nombre) sel.value = nombre;
  else { const sinUbicar = dispositivos.find(d => d.lat === null); if (sinUbicar) sel.value = sinUbicar.nombre; }
  sel.dispatchEvent(new Event("change"));
  $("mapa-nota").textContent = dispositivos.length ? "Haz clic en el mapa donde está el módulo, o usa «Mi ubicación»." : "Primero registra una sonda en Dispositivos.";
}
function terminarUbicar() {
  ubicando = false;
  $("btn-ubicar").classList.remove("activo");
  $("mapa").classList.remove("ubicando");
  $("form-ubicar").hidden = true;
}
async function ubicarModulo(nombre, lat, lng, descripcion) {
  if (!nombre) return false;
  let r;
  try {
    r = await fetchAuth(`/api/dispositivos/${encodeURIComponent(nombre)}`, { method: "PUT", body: JSON.stringify({ lat, lng, descripcion: descripcion || "" }) });
  } catch (e) { return false; }
  if (!r.ok) { $("mapa-nota-2").textContent = "No se pudo guardar la ubicación."; return false; }
  $("mapa-nota-2").textContent = `«${nombreDe(nombre)}» ubicada.`;
  await cargarDispositivos();
  return true;
}
function ajustarMapa(m) {
  const puestos = dispositivos.filter(d => d.lat !== null && d.lng !== null);
  if (!m || ajustados.has(m) || !puestos.length) return;
  ajustados.add(m);
  m.fitBounds(L.latLngBounds(puestos.map(d => [d.lat, d.lng])).pad(0.4), { maxZoom: 15 });
}
function pintarMarcadores() {
  for (const [m, capa] of [[mapa, marcadores], [mapaMini, marcadoresMini]]) {
    if (!m || !capa) continue;
    capa.clearLayers();
    for (const d of dispositivos.filter(d => d.lat !== null && d.lng !== null)) {
      // El color es la ZONA DE OXÍGENO, no la conectividad.
      const e = finca[d.nombre];
      const zona = e ? e.zona : "desconocida";
      const color = cssVar(COLOR_ZONA[zona] || "--tinta2");
      const u = d.ultima;
      const datosTxt = u ? `<b>${num(u.oxigeno_disuelto, 2)}</b> mg/L · <b>${num(u.temperatura, 1)}</b> °C · <b>${num(u.saturacion, 1)}</b> %<br>` : "";
      const estado = e ? `${ETIQUETA_ZONA_LARGA[zona]} · ${haceCuanto(e.edad_segundos)}` : "Sin lecturas todavía";
      L.circleMarker([d.lat, d.lng], {
        radius: zona === "critico" ? 11 : 9, color: "#ffffff", weight: 2,
        dashArray: zona === "sin_datos" ? "3 3" : null, fillColor: color, fillOpacity: 0.95,
      }).bindTooltip(esc(nombreDe(d.nombre)), { permanent: m === mapa, direction: "top", offset: [0, -8] })
        .bindPopup(`<div class="popup-dato"><strong>${esc(nombreDe(d.nombre))}</strong><br>${datosTxt}${estado}<br>` +
          `<button class="popup-quitar" data-quitar="${encodeURIComponent(d.nombre)}">🗑 Quitar del mapa</button></div>`)
        .addTo(capa);
    }
  }
  ajustarMapa(mapaMini);
  if (paginaActual === "mapa") ajustarMapa(mapa);
}
async function cargarDispositivos() {
  try { dispositivos = (await (await fetch("/api/dispositivos")).json()).dispositivos || []; } catch (e) { return; }
  pintarMarcadores();
  const sinUbicar = dispositivos.filter(d => d.lat === null);
  if (!ubicando) $("mapa-nota-2").textContent = sinUbicar.length
    ? `Sin ubicar: ${sinUbicar.map(d => nombreDe(d.nombre)).join(", ")}. Pulsa «Ubicar en el mapa».` : "";
  pintarTablaDispositivos();
  rellenarSelector($("umb-piscina"), false);
}
document.addEventListener("click", async ev => {
  const b = ev.target.closest("[data-quitar]");
  if (!b) return;
  const nombre = decodeURIComponent(b.dataset.quitar);
  if (!confirm(`¿Quitar «${nombreDe(nombre)}» del mapa? Sus lecturas no se borran.`)) return;
  let r;
  try { r = await fetchAuth(`/api/dispositivos/${encodeURIComponent(nombre)}`, { method: "DELETE" }); } catch (e) { return; }
  if (r.ok) { if (mapa) mapa.closePopup(); if (mapaMini) mapaMini.closePopup(); cargarDispositivos(); }
});

// --- Dispositivos: tabla y alta ---------------------------------------------
function pintarTablaDispositivos() {
  $("tabla-dispositivos").innerHTML = dispositivos.map(d => {
    const u = d.ultima;
    const edad = u ? (Date.now() - new Date(u.recibido_en).getTime()) / 1000 : null;
    return `<tr><td><code>${esc(d.nombre)}</code></td><td>${esc(d.descripcion) || "—"}</td>` +
      `<td>${d.lat !== null ? `${(+d.lat).toFixed(5)}, ${(+d.lng).toFixed(5)}` : '<span class="c-sin">sin ubicar</span>'}</td>` +
      `<td>${u ? `${num(u.oxigeno_disuelto, 2)} mg/L · ${haceCuanto(edad)}` : "nunca"}</td>` +
      `<td class="acciones"><button class="boton" data-editar="${encodeURIComponent(d.nombre)}">✎ Editar</button> ` +
      `<button class="boton" data-ubicar="${encodeURIComponent(d.nombre)}">📍 Ubicar</button> ` +
      (d.lat !== null ? `<button class="boton peligro" data-quitar="${encodeURIComponent(d.nombre)}">🗑 Quitar del mapa</button>` : "") + `</td></tr>`;
  }).join("") || '<tr><td colspan="5" class="vacio">Sin sondas registradas. Regístrala abajo o espera la primera lectura del agente.</td></tr>';
}
document.addEventListener("click", ev => {
  const u = ev.target.closest("[data-ubicar]");
  if (u) { irA("mapa"); setTimeout(() => empezarUbicar(decodeURIComponent(u.dataset.ubicar)), 80); return; }
  const e = ev.target.closest("[data-editar]");
  if (e) {
    const d = dispositivos.find(x => x.nombre === decodeURIComponent(e.dataset.editar));
    $("reg-nombre").value = d.nombre; $("reg-descripcion").value = d.descripcion || "";
    $("reg-descripcion").focus();
  }
});
$("form-registrar").addEventListener("submit", async ev => {
  ev.preventDefault();
  const nombre = $("reg-nombre").value.trim(), descripcion = $("reg-descripcion").value.trim();
  if (!nombre) return;
  let r;
  try { r = await fetchAuth(`/api/dispositivos/${encodeURIComponent(nombre)}`, { method: "PUT", body: JSON.stringify({ descripcion }) }); }
  catch (e) { return; }
  if (!r.ok) { mensaje("reg-mensaje", "No se pudo registrar.", "error"); return; }
  mensaje("reg-mensaje", `«${nombre}» guardada.`, "ok");
  $("reg-nombre").value = ""; $("reg-descripcion").value = "";
  await cargarDispositivos(); cargarFinca();
});

// --- Configuración: umbrales, clave ------------------------------------------
async function rellenarUmbrales() {
  rellenarSelector($("umb-piscina"), false);
  const nombre = $("umb-piscina").value;
  if (!nombre) return;
  try {
    const u = await (await fetch(`/api/umbrales/${encodeURIComponent(nombre)}`)).json();
    $("umb-aviso").value = u.od_aviso; $("umb-critico").value = u.od_critico;
  } catch (e) {}
}
$("umb-piscina").addEventListener("change", rellenarUmbrales);
$("form-umbrales").addEventListener("submit", async ev => {
  ev.preventDefault();
  const nombre = $("umb-piscina").value;
  const aviso = parseFloat($("umb-aviso").value), critico = parseFloat($("umb-critico").value);
  if (!nombre) { mensaje("umb-mensaje", "Todavía no hay ninguna sonda.", "error"); return; }
  if (critico > aviso) { mensaje("umb-mensaje", "El crítico debe ser menor o igual que el aviso.", "error"); return; }
  let r;
  try { r = await fetchAuth(`/api/umbrales/${encodeURIComponent(nombre)}`, { method: "PUT", body: JSON.stringify({ od_aviso: aviso, od_critico: critico }) }); }
  catch (e) { return; }
  if (!r.ok) { mensaje("umb-mensaje", (await r.json()).detail || "No se pudo guardar.", "error"); return; }
  mensaje("umb-mensaje", `Umbrales de «${nombreDe(nombre)}»: aviso < ${aviso}, crítico < ${critico} mg/L.`, "ok");
  cargarFinca(); cargarEstado().then(cargar);
});
async function cargarTelegram() {
  let j;
  try { j = await (await fetch("/api/telegram")).json(); } catch (e) { return; }
  const el = $("telegram-estado");
  el.textContent = j.configurado ? "configurado" : "sin configurar";
  el.className = "pill " + (j.configurado ? "resuelta" : "gris");
  $("btn-telegram").disabled = !j.configurado;
}
$("btn-telegram").addEventListener("click", async () => {
  mensaje("telegram-mensaje", "Enviando…", "");
  let r;
  try { r = await fetchAuth("/api/telegram/prueba", { method: "POST" }); } catch (e) { return; }
  if (!r.ok) { mensaje("telegram-mensaje", (await r.json()).detail || "No se pudo enviar.", "error"); return; }
  const j = await r.json();
  mensaje("telegram-mensaje", j.enviado ? "Mensaje enviado: revisa Telegram." : "Telegram rechazó el envío: revisa token y chat id.", j.enviado ? "ok" : "error");
});
$("form-token").addEventListener("submit", ev => {
  ev.preventDefault();
  try { localStorage.setItem("token_panel", $("cfg-token").value.trim()); } catch (e) {}
  mensaje("token-mensaje", "Clave guardada en este navegador.", "ok");
});

// --- Arranque ----------------------------------------------------------------
iniciarMapas();
cargarSesion();
inicial = cargarFinca().then(() => { cargarDispositivos(); cargarEstado().then(cargar); cargarAlarmas(); });
mostrarPagina();
setInterval(() => {
  cargarFinca().then(cargarDispositivos);   // el mapa necesita las zonas ya cargadas
  cargarEstado().then(cargar);
  cargarAlarmas();
}, 10000);
setInterval(() => { if (paginaActual === "reportes") cargarResumen(); if (paginaActual === "historial") { cargarHistorial(); cargarTablaHistorial(); } }, 60000);
