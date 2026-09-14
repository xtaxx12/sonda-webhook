#!/usr/bin/env python3
"""
Levanta el panel con seis piscinas simuladas para ver la vista de conjunto.

Usa una base temporal propia: NUNCA toca readings.db ni dispara alarmas
reales, porque no hay agente escribiendo ni Telegram configurado.

    python3 demo_finca.py          # http://localhost:8002
"""

import math
import os
import random
import sqlite3
import tempfile
from datetime import datetime, timezone, timedelta

RUTA = os.path.join(tempfile.mkdtemp(prefix="sonda-demo-"), "demo.db")
os.environ["DB_PATH"] = RUTA
os.environ.pop("MODBUS_TCP_HOST", None)   # sin agente: los datos son sembrados
os.environ.pop("TELEGRAM_TOKEN", None)    # sin notificaciones

import main  # noqa: E402  (después de fijar el entorno)

# nombre, descripción, OD final, tendencia por hora, lat, lng, minutos mudo
PISCINAS = [
    ("sonda-01", "Piscina 1 — Norte",   7.2,  0.10, -2.612, -79.902, 0),
    ("sonda-02", "Piscina 2 — Norte",   6.4, -0.15, -2.615, -79.897, 0),
    ("sonda-03", "Piscina 3 — Centro",  4.6, -1.30, -2.619, -79.901, 0),
    ("sonda-04", "Piscina 4 — Centro",  3.7, -0.60, -2.622, -79.895, 0),
    ("sonda-05", "Piscina 5 — Sur",     2.6, -0.40, -2.626, -79.899, 0),
    ("sonda-06", "Piscina 6 — Sur",     6.8,  0.00, -2.629, -79.893, 150),
]


def saturacion(od: float, t: float) -> float:
    tk = t + 273.15
    cs = math.exp(-139.34411 + 1.575701e5 / tk - 6.642308e7 / tk**2
                  + 1.243800e10 / tk**3 - 8.621949e11 / tk**4)
    return od / cs * 100


def sembrar() -> None:
    main.init_db()
    ahora = datetime.now(timezone.utc)
    with main.db() as con:
        for nombre, desc, od_fin, pend, lat, lng, mudo in PISCINAS:
            con.execute("INSERT OR REPLACE INTO dispositivos (nombre, lat, lng, descripcion)"
                        " VALUES (?,?,?,?)", (nombre, lat, lng, desc))
            # 6 h de lecturas cada 5 min, terminando en od_fin con esa pendiente
            for i in range(72):
                minutos = (71 - i) * 5 + mudo
                horas_atras = minutos / 60
                od = od_fin - pend * horas_atras + random.uniform(-0.04, 0.04)
                od = max(0.2, od)
                temp = 27.6 + 0.5 * math.sin(i / 9) + random.uniform(-0.05, 0.05)
                t = (ahora - timedelta(minutes=minutos)).isoformat()
                con.execute(
                    "INSERT INTO lecturas (recibido_en, medido_en, dispositivo,"
                    " oxigeno_disuelto, temperatura, saturacion, payload)"
                    " VALUES (?,?,?,?,?,?,?)",
                    (t, t, nombre, round(od, 2), round(temp, 2),
                     round(saturacion(od, temp), 1), "{}"))


if __name__ == "__main__":
    import uvicorn
    sembrar()
    print(f"Base temporal: {RUTA}")
    print("Panel de demostración en http://localhost:8002  (Ctrl-C para salir)\n")
    uvicorn.run(main.app, host="127.0.0.1", port=8002, log_level="warning")
