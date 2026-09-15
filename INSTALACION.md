# Instalar el sistema en otra PC

Guía para montar el monitor de la sonda de oxígeno disuelto en una máquina
nueva: Windows, macOS o Linux (incluida Raspberry Pi).

## Qué es el sistema

Dos programas de Python que trabajan juntos:

| Programa | Qué hace | Dónde corre |
|---|---|---|
| **Servidor** (`main.py`) | Guarda las lecturas, sirve el panel web con gráficas/mapa/alarmas | Cualquier PC (o Fly.io en la nube) |
| **Agente** (`agente.py`) | Consulta la sonda por Modbus TCP cada N segundos y empuja al servidor | Una máquina en la **misma red WiFi que el módulo USR** |

Pueden correr en la misma PC (instalación completa, lo normal) o separados
(servidor en la nube, agente en una Raspberry junto a la sonda).

## Requisitos

- **Python 3.10 o superior** — en Windows descárgalo de [python.org](https://www.python.org/downloads/)
  y marca la casilla *"Add Python to PATH"* al instalar; en macOS/Linux suele venir.
- Los archivos del proyecto: `main.py`, `modbus.py`, `alertas.py`, `acceso.py`,
  `agente.py`, `requirements.txt` y la carpeta `static/` con el panel (y
  `test_*.py` si quieres verificar). Lo más fácil: `git clone` del repositorio
  o copiar la carpeta completa, p. ej. `sonda-webhook`.
- El **módulo USR** configurado en modo `Modbus TCP<=>Modbus RTU`, Server,
  puerto 8899 (ya está así) y conectado a la WiFi — recuerda que **solo ve
  redes de 2.4 GHz**.
- Saber la **IP del módulo** en esa red (aparece en el router; conviene
  fijársela como IP reservada para que no cambie).

## Paso 1 — Preparar el entorno

Abre una terminal en la carpeta del proyecto:

**macOS / Linux / Raspberry:**
```bash
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
mkdir -p ~/sonda-datos
```

**Windows (PowerShell):**
```powershell
python -m venv venv
.\venv\Scripts\pip install -r requirements.txt
mkdir $HOME\sonda-datos
```

> El agente (`agente.py`) no necesita instalar nada: usa solo la librería
> estándar de Python. El venv es para el servidor (FastAPI + uvicorn).

## Paso 2 — Arrancar el servidor

Elige un token secreto (cualquier texto; en producción usa uno largo y
aleatorio). **Sin `AUTH_TOKEN` el servidor rechaza todas las lecturas.**
Si el panel se va a ver desde fuera de la red local (túnel, Fly), pon también
una `PANEL_CLAVE`: el navegador la pedirá al entrar.

**macOS / Linux:**
```bash
DB_PATH=~/sonda-datos/readings.db AUTH_TOKEN=mi-token-secreto PANEL_CLAVE=mi-clave \
  ./venv/bin/uvicorn main:app --host 0.0.0.0 --port 8001
```

**Windows (PowerShell):**
```powershell
$env:DB_PATH="$HOME\sonda-datos\readings.db"
$env:AUTH_TOKEN="mi-token-secreto"
$env:PANEL_CLAVE="mi-clave"
.\venv\Scripts\uvicorn main:app --host 0.0.0.0 --port 8001
```

Comprueba en el navegador: `http://localhost:8001` → debe aparecer el panel
(vacío al principio). `http://localhost:8001/health` debe responder `{"ok":true,...}`.

> `--host 0.0.0.0` hace que el panel también se vea desde otros equipos de la
> red local en `http://IP-DE-ESTA-PC:8001`.

## Paso 3 — Arrancar el agente

En otra terminal (misma carpeta):

**macOS / Linux:**
```bash
SONDA_HOST=192.168.3.157 SONDA_PORT=8899 SONDA_NOMBRE=piscina-1 \
WEBHOOK_URL=http://localhost:8001/usr/webhook WEBHOOK_TOKEN=mi-token-secreto \
INTERVALO=15 BUFFER_DB=~/sonda-datos/agente_buffer.db \
  python3 agente.py
```

**Windows (PowerShell):**
```powershell
$env:SONDA_HOST="192.168.3.157"; $env:SONDA_PORT="8899"
$env:SONDA_NOMBRE="piscina-1"
$env:WEBHOOK_URL="http://localhost:8001/usr/webhook"
$env:WEBHOOK_TOKEN="mi-token-secreto"
$env:INTERVALO="15"; $env:BUFFER_DB="$HOME\sonda-datos\agente_buffer.db"
python agente.py
```

Cambia `SONDA_HOST` por la IP real del módulo. Deberías ver cada 15 s una
línea como:

```
agente: OD=6.09 mg/L T=27.82 °C Sat=78.30 %
```

y las lecturas aparecer en el panel. Si el servidor está en la nube, cambia
`WEBHOOK_URL` por la URL pública (p. ej. `https://tu-app.fly.dev/usr/webhook`).

## Paso 4 — Verificar (opcional pero recomendado)

```bash
./venv/bin/pip install pytest pytest-asyncio httpx
./venv/bin/python -m pytest -q
```

Todos los tests deben pasar (más de 150). Cubren el protocolo Modbus (con
tramas reales de la sonda), el agente, las alarmas, el acceso y la API.

## Variables de configuración

**Servidor** (`main.py`):

| Variable | Por defecto | Para qué |
|---|---|---|
| `AUTH_TOKEN` | — (obligatoria) | Protege el webhook y las acciones del panel |
| `PANEL_CLAVE` | vacío | Clave para entrar al panel (recomendada si se ve desde internet) |
| `DB_PATH` | `./readings.db` | Dónde guardar la base (ponla fuera de carpetas temporales) |
| `TELEGRAM_TOKEN` | vacío | Token del bot de Telegram para las alarmas |
| `TELEGRAM_CHAT_ID` | vacío | Chat que recibe las alarmas |
| `MUDA_MIN` | `10` | Minutos sin datos antes de alarmar "sonda muda" |
| `STATS_UTC_OFFSET` | `-5` | Zona horaria del resumen diario (Ecuador) |
| `MODBUS_TCP_PORT` | apagado | Modo alternativo sin agente (ver README) |

**Agente** (`agente.py`):

| Variable | Por defecto | Para qué |
|---|---|---|
| `SONDA_HOST` | `192.168.3.157` | IP del módulo USR |
| `SONDA_PORT` | `8899` | Puerto Modbus TCP del módulo |
| `SONDA_NOMBRE` | `sonda-od-agente` | Nombre de la piscina en el panel (uno distinto por sonda) |
| `WEBHOOK_URL` | — (obligatoria) | A dónde empujar las lecturas |
| `WEBHOOK_TOKEN` | vacío | El mismo `AUTH_TOKEN` del servidor |
| `INTERVALO` | `15` | Segundos entre consultas a la sonda |
| `BUFFER_DB` | `./agente_buffer.db` | Cola local si se cae el internet |

## Arranque automático

### Linux / Raspberry Pi (systemd)

Crea `/etc/systemd/system/sonda-servidor.service`:

```ini
[Unit]
Description=Servidor sonda OD
After=network-online.target

[Service]
User=pi
WorkingDirectory=/home/pi/sonda-webhook
Environment=DB_PATH=/home/pi/sonda-datos/readings.db
Environment=AUTH_TOKEN=mi-token-secreto
Environment=PANEL_CLAVE=mi-clave
ExecStart=/home/pi/sonda-webhook/venv/bin/uvicorn main:app --host 0.0.0.0 --port 8001
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Y `/etc/systemd/system/sonda-agente.service`:

```ini
[Unit]
Description=Agente sonda OD
After=network-online.target

[Service]
User=pi
WorkingDirectory=/home/pi/sonda-webhook
Environment=SONDA_HOST=192.168.3.157
Environment=SONDA_NOMBRE=piscina-1
Environment=WEBHOOK_URL=http://localhost:8001/usr/webhook
Environment=WEBHOOK_TOKEN=mi-token-secreto
Environment=INTERVALO=15
Environment=BUFFER_DB=/home/pi/sonda-datos/agente_buffer.db
ExecStart=/usr/bin/python3 /home/pi/sonda-webhook/agente.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Activa ambos:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now sonda-servidor sonda-agente
sudo systemctl status sonda-agente      # ver que corre
journalctl -u sonda-agente -f           # ver las lecturas en vivo
```

(Si el servidor vive en Fly, instala solo `sonda-agente.service` con la
`WEBHOOK_URL` pública.)

### Windows

Lo más simple: crea dos archivos `.bat` con los comandos del paso 2 y 3 y
ponlos en el Programador de tareas ("al iniciar sesión"). 

### macOS

`launchd` con dos plists en `~/Library/LaunchAgents`, o simplemente deja las
dos terminales abiertas.

## Problemas frecuentes

| Síntoma | Causa probable |
|---|---|
| `agente: la sonda no respondió` | Cable A/B suelto o mal pelado (aprieta sobre el cobre), o la sonda sin alimentación de 12 V |
| El agente no conecta con el módulo | IP equivocada (`SONDA_HOST`), o el módulo se cayó de la WiFi — recuerda: solo 2.4 GHz |
| Webhook responde `401` | El `WEBHOOK_TOKEN` del agente no coincide con el `AUTH_TOKEN` del servidor |
| Webhook responde `503` | El servidor arrancó sin `AUTH_TOKEN` |
| El panel pide una clave que no conoces | Es `PANEL_CLAVE` del servidor; cámbiala y reinicia (las sesiones viejas caducan solas) |
| No puedo registrar sondas ni cambiar umbrales | Sin `PANEL_CLAVE`, guarda el `AUTH_TOKEN` en Configuración → Clave de la app |
| Panel vacío pero el agente lee bien | `WEBHOOK_URL` apunta a otro servidor/puerto; mira el buffer: si crece, no está entregando |
| Valores absurdos (millones) | Byte order: la sonda entrega float32 **DCBA**; usa este código tal cual, no otro decodificador |
| El módulo no aparece en la red | Reconfigura su WiFi: conéctate a su red propia, entra a `10.10.100.254` (admin/admin) |

## Mapa de registros de la sonda (referencia)

Esclavo 1, función 03, 6 registros desde `0x0000` — trama: `01 03 00 00 00 06 C5 C8`

| Variable | Registros | Formato |
|---|---|---|
| Oxígeno disuelto | 0–1 | float32 **DCBA**, mg/L |
| Temperatura | 2–3 | float32 **DCBA**, °C |
| Saturación | 4–5 | float32 **DCBA**, % |
