"""
Tests del colector Modbus RTU sobre TCP (modbus.py).

La trama de sondeo conocida de la sonda (esclavo 1, función 03, 6 registros)
sirve de vector de verdad para el CRC:  01 03 00 00 00 06 C5 C8
"""

import asyncio
import struct

import pytest

from modbus import TRAMA_SONDEO, atender_modulo, construir_trama, crc16, parsear_respuesta


# --- CRC y construcción de tramas -------------------------------------------

def test_crc16_vector_conocido():
    # El CRC de "01 03 00 00 00 06" debe ser C5 C8 (byte bajo primero).
    assert crc16(bytes.fromhex("010300000006")) == bytes.fromhex("C5C8")


def test_construir_trama_de_sondeo():
    assert construir_trama(esclavo=1, inicio=0, cantidad=6) == bytes.fromhex("010300000006C5C8")


def test_trama_sondeo_es_la_de_la_sonda():
    assert TRAMA_SONDEO == bytes.fromhex("010300000006C5C8")


# --- Parseo de respuestas ----------------------------------------------------

def respuesta_valida(od=7.46, temp=28.56, sat=97.08):
    """Construye la respuesta de 17 bytes que enviaría la sonda (floats DCBA)."""
    datos = struct.pack("<fff", od, temp, sat)
    cuerpo = bytes([0x01, 0x03, 0x0C]) + datos
    return cuerpo + crc16(cuerpo)


def test_parsear_respuesta_bytes_reales_dcba():
    # Los 12 bytes de datos de una respuesta real capturada del hardware:
    # 7d 07 6a 40 | f4 9a d1 41 | 76 77 36 42 → 3.66 mg/L, 26.20 °C, 45.62 %
    cuerpo = bytes([0x01, 0x03, 0x0C]) + bytes.fromhex("7d076a40f49ad14176773642")
    campos = parsear_respuesta(cuerpo + crc16(cuerpo))
    assert campos is not None
    assert campos["oxigeno_disuelto"] == pytest.approx(3.66, abs=0.01)
    assert campos["temperatura"] == pytest.approx(26.20, abs=0.01)
    assert campos["saturacion"] == pytest.approx(45.62, abs=0.01)


def test_parsear_respuesta_valida():
    campos = parsear_respuesta(respuesta_valida())
    assert campos is not None
    assert campos["oxigeno_disuelto"] == pytest.approx(7.46, abs=1e-4)
    assert campos["temperatura"] == pytest.approx(28.56, abs=1e-4)
    assert campos["saturacion"] == pytest.approx(97.08, abs=1e-4)


def test_parsear_respuesta_crc_malo():
    trama = bytearray(respuesta_valida())
    trama[-1] ^= 0xFF
    assert parsear_respuesta(bytes(trama)) is None


def test_parsear_respuesta_corta():
    assert parsear_respuesta(b"\x01\x03") is None


def test_parsear_respuesta_con_basura_antes():
    # El módulo puede intercalar paquetes propios; la respuesta debe
    # encontrarse aunque venga precedida de otros bytes.
    campos = parsear_respuesta(b"HEARTBEAT" + respuesta_valida())
    assert campos is not None
    assert campos["temperatura"] == pytest.approx(28.56, abs=1e-4)


# --- Ciclo de sondeo sobre TCP ----------------------------------------------

@pytest.mark.asyncio
async def test_sondeo_extremo_a_extremo():
    """Un módulo simulado se conecta, responde al sondeo y la lectura llega al callback."""
    lecturas = []

    async def guardar(campos, dispositivo):
        lecturas.append((campos, dispositivo))

    server = await asyncio.start_server(
        lambda r, w: atender_modulo(r, w, guardar=guardar, intervalo=0.05, espera=1.0),
        "127.0.0.1", 0,
    )
    puerto = server.sockets[0].getsockname()[1]

    async def modulo_simulado():
        reader, writer = await asyncio.open_connection("127.0.0.1", puerto)
        writer.write(b"REG:0001SN123456")  # paquete de registro típico de USR
        await writer.drain()
        # Responde a dos sondeos y cierra.
        for _ in range(2):
            trama = await asyncio.wait_for(reader.readexactly(8), timeout=2)
            assert trama == TRAMA_SONDEO
            writer.write(respuesta_valida())
            await writer.drain()
        writer.close()

    await asyncio.wait_for(modulo_simulado(), timeout=5)
    await asyncio.sleep(0.1)
    server.close()
    await server.wait_closed()

    assert len(lecturas) >= 1
    campos, dispositivo = lecturas[0]
    assert campos["oxigeno_disuelto"] == pytest.approx(7.46, abs=1e-4)
    assert campos["temperatura"] == pytest.approx(28.56, abs=1e-4)
    assert campos["saturacion"] == pytest.approx(97.08, abs=1e-4)
    # El paquete de registro identifica al dispositivo.
    assert dispositivo == "REG:0001SN123456"


# --- Integración con main.py -------------------------------------------------

@pytest.mark.asyncio
async def test_guardar_lectura_modbus_en_db(tmp_path, monkeypatch):
    """La lectura del colector queda en la tabla `lecturas` de main.py."""
    import importlib
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    import main
    importlib.reload(main)
    main.init_db()

    await main.guardar_lectura_modbus(
        {"oxigeno_disuelto": 7.46, "temperatura": 28.56, "saturacion": 97.08},
        "SN123456",
    )

    with main.db() as con:
        fila = con.execute("SELECT * FROM lecturas ORDER BY id DESC LIMIT 1").fetchone()
    assert fila is not None
    assert fila["temperatura"] == pytest.approx(28.56, abs=1e-4)
    assert fila["dispositivo"] == "SN123456"
    assert fila["recibido_en"]


@pytest.mark.asyncio
async def test_rechaza_modulo_sin_registro_valido():
    """Con `registro_esperado`, una conexión que no se identifica bien se cierra sin sondear."""
    lecturas = []

    async def guardar(campos, dispositivo):
        lecturas.append(campos)

    server = await asyncio.start_server(
        lambda r, w: atender_modulo(
            r, w, guardar=guardar, intervalo=0.05, espera=0.5,
            registro_esperado="SN123456",
        ),
        "127.0.0.1", 0,
    )
    puerto = server.sockets[0].getsockname()[1]

    reader, writer = await asyncio.open_connection("127.0.0.1", puerto)
    writer.write(b"IMPOSTOR")
    await writer.drain()
    # El servidor debe cerrar sin enviar ningún sondeo.
    datos = await asyncio.wait_for(reader.read(64), timeout=3)
    assert datos == b""
    writer.close()
    server.close()
    await server.wait_closed()
    assert lecturas == []
