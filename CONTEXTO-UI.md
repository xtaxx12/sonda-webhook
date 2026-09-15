# Contexto del proyecto — brief para diseñar la UI

Documento para quien vaya a diseñar o rediseñar la interfaz del monitor de
oxígeno disuelto. Contiene todo lo necesario para trabajar sin leer el código:
propósito, usuarios, datos, API y decisiones ya tomadas.

---

## 1. Qué es esto, en una frase

Un panel web que muestra en tiempo real el **oxígeno disuelto (OD), la
temperatura y la saturación** del agua de piscinas de camarón, con **alarmas**
cuando el oxígeno baja y **mapa** de dónde está cada sonda.

## 2. Por qué existe

En una camaronera, el oxígeno del agua cae de madrugada (la respiración de
todo lo vivo en la piscina consume O₂ y no hay fotosíntesis que lo reponga).
Si baja de ~4 mg/L el camarón se estresa; bajo ~3 mg/L empieza a morir. La
respuesta es prender aireadores a tiempo. **La pregunta que responde el panel
es "¿tengo que levantarme a prender los aireadores?"** — no "¿cuánto mide la
sonda?".

Antes los datos vivían en la nube del fabricante del hardware (USR Cloud),
con 1 lectura por minuto y sin forma gratuita de sacarlos. Este sistema los
lee directo de la sonda cada 15 segundos y los guarda en una base propia.

## 3. Quién lo usa y en qué situación

| Usuario | Situación | Dispositivo | Qué necesita ver |
|---|---|---|---|
| **Dueño / encargado** | 3 de la mañana, recién despierto por una alarma en Telegram | Teléfono, pantalla brillante en cuarto oscuro | En 2 segundos: ¿qué piscina, qué tan grave, hacia dónde va? |
| **Operario en la piscina** | De día, junto al agua, con el teléfono en la mano | Teléfono, sol directo | Valor actual de *esta* piscina; ubicar la sonda en el mapa con GPS |
| **Dueño revisando** | Tarde, en la oficina o casa | Laptop/desktop | Tendencias del día, comparar días, exportar a Excel, historial de alarmas |
| **Técnico instalando** | Montando una sonda nueva | Laptop en la camaronera | Registrar la sonda, ver que llegan datos, ajustar umbrales |

**Móvil primero.** El caso que más importa (alarma nocturna) es siempre en
teléfono. El desktop es el caso secundario.

## 4. El hardware (para entender los datos)

```
Sonda de OD (RS485 Modbus)  →  Módulo USR (RS485→WiFi)  →  agente.py en una PC/Raspberry de la LAN
                                                                     ↓ HTTPS
                                                          Servidor (main.py, FastAPI + SQLite) → panel web
```

- Cada sonda reporta **tres valores cada 15 s**: OD (mg/L), temperatura (°C),
  saturación (%). Resolución real: centésimas.
- Cada sonda tiene un **nombre** (`deviceName`, p. ej. `piscina-1`) que la
  identifica en todo el sistema. Una sonda = una piscina.
- Cuando la sonda no responde, el agente registra **una fila de fallo** (valores
  nulos + motivo). Un hueco en los datos es información: puede ser sonda
  desconectada, batería baja, o corte de internet.
- Las sondas se alimentan de batería + panel solar en campo: los fallos por
  voltaje bajo tienden a concentrarse en horas sin sol.

## 5. Modelo de datos

**Lectura** (`lecturas`): `id`, `recibido_en` (ISO UTC), `medido_en`,
`dispositivo`, `oxigeno_disuelto`, `temperatura`, `saturacion`, `payload`
(JSON crudo). Valores nulos = fila de fallo.

**Dispositivo** (`dispositivos`): `nombre`, `lat`, `lng`, `descripcion`. Se
crea al registrarlo desde la UI o automáticamente al llegar su primera lectura.

**Umbrales** (`umbrales`, por dispositivo): `od_aviso` (defecto 4.0),
`od_critico` (defecto 3.0).

**Alarma** (`alarmas`): `dispositivo`, `tipo` (`od_bajo` | `od_critico` |
`sin_datos`), `valor`, `iniciada_en`, `ultima_notif`, `resuelta_en` (null =
activa). Reglas: se abre al cruzar el umbral, se repite cada 30 min mientras
siga, se cierra sola al recuperarse. `sin_datos` se abre tras 10 min sin
lectura válida.

Zona horaria del sitio: **Ecuador, UTC−5** (sin horario de verano). Los
timestamps de la API vienen en UTC; la UI convierte a hora local.

## 6. Contrato de la API (todo JSON, sin autenticación para leer)

Base: misma URL del panel. Ejemplos con datos reales.

### Estado operativo — lo primero que debe leer la UI
`GET /api/estado?dispositivo=piscina-1`
```json
{
  "hay_datos": true,
  "dispositivo": "piscina-1",
  "zona": "normal",                    // "normal" | "aviso" | "critico"
  "oxigeno_disuelto": 7.38, "temperatura": 28.1, "saturacion": 95.34,
  "umbrales": {"od_aviso": 4.0, "od_critico": 3.0},
  "pendiente_od_hora": 0.909,          // mg/L por hora (regresión última hora), null si <5 muestras
  "horas_a_critico": null,             // solo si está bajando; estimación de margen
  "edad_segundos": 11,                 // antigüedad de la última lectura válida
  "medido_en": "2026-09-14T19:45:01+00:00",
  "muestras_tendencia": 239
}
```
Sin lecturas: `{"hay_datos": false}`. **La zona la decide el servidor** (misma
lógica que las alarmas); la UI no debe recalcularla.

### Última lectura
`GET /api/latest?dispositivo=piscina-1` → una lectura (404 si no hay).

### Histórico
`GET /api/readings?limit=20000&since=ISO&dispositivo=piscina-1`
```json
{"total": 240, "lecturas": [ {"id": 5001, "recibido_en": "...", "medido_en": "...",
  "dispositivo": "piscina-1", "oxigeno_disuelto": 6.57, "temperatura": 26.14, "saturacion": 81.83}, ... ]}
```
Orden: más reciente primero. Incluye filas de fallo (valores `null`).
24 h a 15 s ≈ 5 760 filas.

### Dispositivos (sondas / piscinas)
`GET /api/dispositivos`
```json
{"dispositivos": [ {"nombre": "piscina-1", "lat": -3.419, "lng": -79.992,
  "descripcion": "Piscina 1", "ultima": {"recibido_en": "...", "oxigeno_disuelto": 6.57,
  "temperatura": 26.14, "saturacion": 81.83}} ]}
```
`lat/lng` null = sin ubicar. `ultima` null = nunca reportó.

- `PUT /api/dispositivos/{nombre}?token=…` body `{"lat", "lng", "descripcion"}` — todos opcionales; lo que no viene se conserva. Sirve para registrar (solo descripción), ubicar, o renombrar la descripción.
- `DELETE /api/dispositivos/{nombre}?token=…` — quita del mapa (no borra lecturas).

### Alarmas
`GET /api/alarmas?limit=50` → `{"activas": [...], "historial": [...]}` (cada
alarma con los campos del modelo).

### Umbrales
`GET /api/umbrales/{nombre}` → `{"od_aviso": 4.0, "od_critico": 3.0}`
`PUT /api/umbrales/{nombre}?token=…` body `{"od_aviso", "od_critico"}` (crítico ≤ aviso).

### Estadísticas diarias
`GET /api/stats?dias=7&dispositivo=piscina-1`
```json
{"dias": [ {"fecha": "2026-09-14", "dispositivo": "piscina-1", "n": 662,
  "od_min": 3.63, "od_max": 7.55, "od_prom": 6.83,
  "temp_min": 26.3, "temp_max": 28.7, "temp_prom": 27.4} ]}
```
Día en hora local (UTC−5).

### Exportar
`GET /api/export.csv?since=ISO&hasta=ISO&dispositivo=…` → CSV descargable.

### Escrituras y token
Toda escritura (PUT/DELETE) exige `?token=…` o header `X-Auth-Token`. La UI
actual lo pide una vez y lo guarda en `localStorage` (`token_panel`). No hay
usuarios ni roles; leer es público, escribir requiere el token.

### Docs interactivas
`GET /docs` (Swagger automático de FastAPI).

## 7. Lo que la UI tiene hoy (y funciona)

Una sola página (`GET /`), HTML + CSS + JS vanilla embebidos en `main.py`,
sin build ni framework. Refresca datos cada 10 s por `fetch`. Modo claro/oscuro
según el sistema. De arriba abajo:

1. **Banner rojo** si hay alarmas activas.
2. **Selector de piscina** (chips) — solo aparece con ≥2 sondas.
3. **Franja de estado** coloreada por zona: "● Oxígeno normal · hace 11 s · piscina-1". En crítico late. Si la última lectura tiene >3 min dice "Sin datos recientes".
4. **Tarjeta grande de OD** (2.9 rem) coloreada por zona, con tendencia debajo:
   "↑ 0.93 mg/L por hora · aviso 4 · crítico 3". Si baja: "Llega al crítico en ~1.8 h".
5. **Temperatura y saturación** en dos tarjetas más pequeñas.
6. **Rango**: 1 h / 6 h / 24 h + botón "⚙ Umbrales".
7. **Tres gráficas de línea** (una por variable; nunca doble eje). La de OD tiene
   **bandas** ámbar (<aviso) y roja (<crítico) con línea punteada. En 6/24 h se
   sombrea la **noche** (18:00–06:00). La línea **se corta** en huecos de datos.
   Tooltip con crosshair sincronizado entre las tres.
8. **Mapa** (Leaflet + OpenStreetMap) con un marcador por sonda: verde "En línea",
   ámbar "Sin datos desde hace X", gris "Sin lecturas". Popup con últimos valores
   y botón "Quitar del mapa". Botones: "➕ Registrar sonda", "📡 Mi ubicación"
   (GPS del teléfono), "📍 En el mapa".
9. **Resumen diario** (7 días): tabla mín/máx/prom, mínimos en rojo si cruzaron
   el umbral. Botón "⬇ Descargar CSV".
10. **Tablas plegables**: últimas lecturas, historial de alarmas.

Paleta de series validada para daltonismo y contraste en ambos modos:
OD azul `#2a78d6`/`#3987e5`, temperatura naranja `#eb6834`/`#d95926`,
saturación aqua `#1baf7a`/`#199e70` (claro/oscuro). Estado: verde ok, ámbar
aviso, rojo crítico — siempre con icono + texto, nunca color solo.

## 8. Principios de diseño ya decididos (conservar)

- **Estado antes que telemetría.** Lo primero visible es la zona (normal/aviso/
  crítico) y la tendencia, no un número suelto.
- **Un dato viejo no es un estado.** Si la sonda lleva minutos sin reportar, la
  UI lo dice en vez de mostrar "normal" con un valor antiguo.
- **Los huecos se ven.** Un corte de dos horas no se dibuja como línea continua.
- **La noche se ve.** El ciclo día/noche es el contexto del oxígeno.
- **Un eje por gráfica.** Variables con unidades distintas nunca comparten eje.
- **No inventar.** Sin tendencia con <5 muestras; sin "horas al crítico" si sube.
- **La zona la calcula el servidor**, para que panel y alarmas nunca discrepen.
- **Móvil sin scroll horizontal** a 400 px; tablas anchas scrollean dentro de su caja.
- **Modo oscuro real** (para la alarma de las 3 a.m.).

## 9. Lo que se puede mejorar (dónde aportar)

Ideas abiertas, por orden de valor para el usuario:

1. **Vista "todas las piscinas"** para cuando haya varias: una tarjeta compacta
   por piscina (zona, OD, tendencia, edad) ordenadas por gravedad; tocar una →
   detalle. Hoy el selector obliga a ver una a la vez.
2. **Jerarquía en móvil para la alarma nocturna**: que en 400 px lo primero sea
   estado + OD + tendencia y todo lo demás quede debajo. Botón de "silenciar
   30 min" / "ya prendí aireadores" (requiere endpoint nuevo).
3. **Gráfica de OD como protagonista**; temperatura y saturación pueden ser
   secundarias/colapsables.
4. **Indicadores de salud del sistema**: batería/voltaje si algún día se reporta,
   % de lecturas fallidas del día, última vez que reportó cada sonda.
5. **Ubicar/registrar sondas** con un flujo más guiado que `prompt()`.
6. **Comparar días** en el resumen (mín de madrugada de cada día, en gráfica).
7. Legibilidad bajo sol directo (contraste alto en modo claro).

## 10. Restricciones técnicas

- Servidor **Python/FastAPI**, una sola página servida desde `main.py`. Puede
  moverse a archivos estáticos, pero **sin build step obligatorio** (debe correr
  en una Raspberry con `python3` y nada más).
- JS vanilla o librerías por CDN con integridad (SRI). Hoy solo Leaflet 1.9.4.
- Sin cuentas de usuario. Escrituras protegidas por token (ver §6).
- Debe funcionar en **localhost** y en **HTTPS público** (Fly.io). El GPS del
  navegador solo funciona en HTTPS/localhost.
- Puede haber **1 sonda o 20**. Diseñar para ambas.
- Los datos pueden tener **huecos** y **filas nulas**; la UI debe tolerarlos.
- Nombres de sonda son texto libre del usuario (`piscina-1`, `Piscina Norte`).

## 11. Datos para probar

- Servidor local: `http://localhost:8001` (datos reales de una sonda en casa).
- Para simular una segunda piscina o una alarma, un POST al webhook basta:
  ```bash
  curl -X POST "http://localhost:8001/usr/webhook?token=prueba" -H 'Content-Type: application/json' \
    -d '{"deviceName":"piscina-2","Dissolved_Oxygen":3.4,"Temperature":27.5,"DO_Saturation":42}'
  ```
- Fila de fallo: `{"deviceName":"piscina-2","error":"sonda sin respuesta"}`.
- Para limpiar lo simulado: borrar por `dispositivo` en la tabla `lecturas`
  (SQLite en `~/sonda-datos/readings.db`).

## 12. Vocabulario

Usar los términos del usuario, en español: **piscina** (no "estanque"),
**sonda** (no "sensor"), **oxígeno** u **OD**, **aireadores**, **aviso** /
**crítico**, **sin datos**. Nombres de sonda tal como los escribió el usuario.
