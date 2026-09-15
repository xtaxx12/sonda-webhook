"""
Recalcula la marca de manipulación sobre el histórico ya guardado.

    python3 backfill_manipulacion.py ~/sonda-datos/readings.db

Marca las lecturas tomadas con la sonda fuera del agua, para que dejen de
contaminar los mínimos y máximos diarios. Es idempotente: se puede correr
las veces que haga falta.
"""

import os
import sys
from datetime import datetime, timedelta


def marcar(con, gradiente: float, delta_minimo: float, ventana_s: int) -> int:
    """Marca sobre una conexión abierta. Devuelve cuántas lecturas marcó."""
    filas = con.execute(
        "SELECT id, dispositivo, recibido_en, temperatura FROM lecturas"
        " WHERE temperatura IS NOT NULL ORDER BY dispositivo, recibido_en"
    ).fetchall()

    marcadas, sospechoso_hasta, prev = [], {}, {}
    for f in filas:
        d = f["dispositivo"]
        try:
            t = datetime.fromisoformat(f["recibido_en"])
        except (TypeError, ValueError):
            continue
        anterior = prev.get(d)
        if anterior:
            minutos = (t - anterior[0]).total_seconds() / 60
            salto = abs(f["temperatura"] - anterior[1])
            if minutos > 0 and salto > delta_minimo and salto / minutos > gradiente:
                sospechoso_hasta[d] = t + timedelta(seconds=ventana_s)
        if d in sospechoso_hasta and t <= sospechoso_hasta[d]:
            marcadas.append(f["id"])
        prev[d] = (t, f["temperatura"])

    con.executemany("UPDATE lecturas SET manipulacion = 1 WHERE id = ?",
                    [(i,) for i in marcadas])
    con.commit()
    return len(marcadas)


def backfill(ruta: str):
    os.environ["DB_PATH"] = ruta
    import importlib
    import main
    importlib.reload(main)
    main.init_db()
    with main.db() as con:
        total = con.execute("SELECT COUNT(*) c FROM lecturas").fetchone()["c"]
        n = marcar(con, main.GRADIENTE_MANIPULACION,
                   main.DELTA_MINIMO_MANIPULACION, main.VENTANA_MANIPULACION)
    return total, n


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit("uso: python3 backfill_manipulacion.py <ruta-a-readings.db>")
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    total, n = backfill(sys.argv[1])
    print(f"{n} de {total} lecturas marcadas como manipulación")
