# Receptor de la sonda de oxígeno disuelto

Recibe las lecturas que USR Cloud envía por webhook, las guarda en SQLite
y las expone por HTTP. Pensado para desplegarse en Fly.io.

USR Cloud sigue funcionando igual — el dashboard, el historial y las alarmas
de la plataforma no se tocan. Esto es una copia paralela de los datos.

---

## 1. Desplegar en Fly

```bash
# Edita fly.toml y cambia app = "sonda-od" por el nombre que quieras
fly launch --no-deploy          # si la app aún no existe
fly volumes create datos --size 1 --region gru
fly secrets set AUTH_TOKEN=$(openssl rand -hex 24)
fly deploy
```

Guarda el token que generaste — lo necesitas en el paso 2:

```bash
fly secrets list                # confirma que AUTH_TOKEN existe
```

Si perdiste el valor, genera otro con `fly secrets set AUTH_TOKEN=...`.

Comprueba que arrancó:

```bash
curl https://TU-APP.fly.dev/health
# {"ok":true,"db":"/data/readings.db"}
```

## 2. Crear la regla en USR Cloud

1. Entra a **Rule engine → Rule List → Addrule**
2. Nodo de entrada: el dispositivo `Dissolved_Oxygen_2`, disparando cuando
   se actualicen sus variables
3. Nodo de salida: **HTTP / Webhook** con esta configuración:

   | Campo | Valor |
   |---|---|
   | Método | `POST` |
   | URL | `https://TU-APP.fly.dev/usr/webhook?token=EL_TOKEN` |
   | Content-Type | `application/json` |

   Si el nodo permite cabeceras en vez de query string, es preferible mandar
   el token como `X-Auth-Token` y dejar la URL limpia.

4. Guarda y **activa** la regla.

## 3. Comprobar que llegan los datos

```bash
curl https://TU-APP.fly.dev/api/latest
```

O abre `https://TU-APP.fly.dev/` en el navegador.

**Si llegan peticiones pero sin valores**, el formato del JSON de USR no
coincide con lo que el parser espera. Mira el payload real:

```bash
curl https://TU-APP.fly.dev/api/raw
```

Y ajusta el diccionario `CAMPOS` al principio de `main.py` con los nombres
que veas. El parser ya cubre tres formatos comunes (plano, anidado y lista
de `{name, value}`), así que lo más probable es que funcione sin tocar nada.

---

## Endpoints

| Ruta | Qué hace |
|---|---|
| `GET /` | Página con la última lectura |
| `POST /usr/webhook` | Lo que llama USR Cloud |
| `GET /api/latest` | Última lectura con valores (JSON) |
| `GET /api/readings?limit=100&since=ISO` | Histórico (JSON) |
| `GET /api/raw?limit=5` | Payloads crudos, para depurar |
| `GET /health` | Healthcheck |
| `GET /docs` | Documentación automática de la API |

## Variables de entorno

| Variable | Por defecto | Para qué |
|---|---|---|
| `AUTH_TOKEN` | vacío | Si la defines, exige el token en el webhook. **Ponla siempre.** |
| `DB_PATH` | `/data/readings.db` | Dónde guardar la base |
| `MODBUS_TCP_PORT` | vacío (apagado) | Puerto TCP para que el módulo USR se conecte directo (sin nube) |
| `MODBUS_INTERVALO` | `60` | Segundos entre sondeos Modbus |
| `MODBUS_REGISTRO` | vacío | Texto que debe contener el paquete de registro del módulo (su SN). **Ponla en producción.** |

## Agente local (recomendado, sin USR Cloud)

El módulo USR ya está configurado como servidor Modbus TCP en la LAN
(`Data Transfer Mode: Modbus TCP<=>Modbus RTU`, Server, puerto 8899).
`agente.py` corre en cualquier máquina de esa red — solo librería estándar,
sin `pip install` — consulta la sonda, valida la lectura y la empuja al
webhook de esta app por HTTPS saliente. Nada queda expuesto a internet.

```bash
SONDA_HOST=192.168.3.157 SONDA_PORT=8899 \
WEBHOOK_URL=https://TU-APP.fly.dev/usr/webhook WEBHOOK_TOKEN=EL_TOKEN \
INTERVALO=15 python3 agente.py
```

Qué hace en cada ciclo:

- Consulta por Modbus TCP (esclavo 1, fc 03, 6 registros) y decodifica los
  floats en **DCBA** (little endian — con otro orden salen números astronómicos).
- Valida la coherencia física con Benson-Krause: la saturación reportada debe
  cuadrar con OD/temperatura; si no, avisa (byte order, trama corrupta o
  sonda descalibrada).
- Si la sonda calla (el `err:1` de USR), **no guarda nada** — nunca un cero falso.
- Si el webhook no responde, la lectura queda en un buffer SQLite local
  (`BUFFER_DB`, por defecto `agente_buffer.db`) y se reenvía cuando vuelva
  el internet.

Tests: `python -m pytest test_agente.py test_modbus.py`

## Modo Modbus directo (módulo como cliente, sin agente)

Además del webhook, la app puede sondear la sonda directamente si el módulo
USR se configura como **cliente TCP transparente** (o con su segundo socket)
apuntando a este servidor. No pasa por USR Cloud y no cuesta nada:

```bash
MODBUS_TCP_PORT=5020 MODBUS_INTERVALO=60 MODBUS_REGISTRO=SN_DEL_MODULO \
  uvicorn main:app --port 8000
```

La app envía `01 03 00 00 00 06 C5 C8` cada `MODBUS_INTERVALO` segundos,
parsea la respuesta (3 × float32: OD, temperatura, saturación) y guarda en
la misma tabla — el panel y la API muestran estas lecturas igual que las
del webhook. Silencio de la sonda = no se guarda nada (el `err:1` de USR).

Para configurar el módulo: conéctate a su WiFi, entra a `10.10.100.254`
(admin/admin) y en la sección de sockets apunta el destino a la IP pública
o dominio de este servidor y el puerto `MODBUS_TCP_PORT`. El socket va en
texto plano — define `MODBUS_REGISTRO` con el SN del módulo para rechazar
conexiones ajenas.

En Fly hace falta exponer el puerto crudo añadiendo a `fly.toml`:

```toml
[[services]]
  protocol      = "tcp"
  internal_port = 5020
  [[services.ports]]
    port = 5020
```

Y definir los secretos: `fly secrets set MODBUS_TCP_PORT=5020 MODBUS_REGISTRO=...`

Los tests del protocolo y del colector están en `test_modbus.py`
(`python -m pytest test_modbus.py`).

## Probarlo en local

`AUTH_TOKEN` es obligatorio: sin él, el webhook responde 503 y no acepta
lecturas (evita inyecciones y alarmas falsas). Guarda la base fuera de
directorios temporales:

```bash
pip install -r requirements.txt
mkdir -p ~/sonda-datos
DB_PATH=~/sonda-datos/readings.db AUTH_TOKEN=prueba uvicorn main:app --reload --port 8000

curl -X POST "http://localhost:8000/usr/webhook?token=prueba" \
  -H 'Content-Type: application/json' \
  -d '{"deviceName":"Dissolved_Oxygen_2","Dissolved_Oxygen":4.25,"Temperature":27.79,"DO_Saturation":54.58}'
```

## Notas

- `auto_stop_machines = false` en `fly.toml` es deliberado. Si la máquina se
  duerme, los webhooks que lleguen mientras arranca se pierden.
- El volumen `datos` hace que las lecturas sobrevivan a cada `fly deploy`.
  Sin él, la base se borra en cada despliegue.
- El campo `payload` guarda el JSON crudo (hasta 20 KB) de cada petición,
  así que aunque el parseo falle, el dato original no se pierde.

## Mapa de registros de la sonda

Por si algún día quieres leerla directamente por Modbus RTU:

| Variable | Registros | Formato |
|---|---|---|
| Oxígeno disuelto | 0–1 | float32, mg/L |
| Temperatura | 2–3 | float32, °C |
| Saturación | 4–5 | float32, % |

Trama de lectura completa (esclavo 1, función 03, 6 registros):
`01 03 00 00 00 06 C5 C8`
