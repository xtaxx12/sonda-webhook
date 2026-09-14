"""
Colector Modbus RTU sobre TCP para la sonda de oxígeno disuelto.

El módulo USR, configurado como cliente TCP transparente (o con su segundo
socket apuntando aquí), abre la conexión y hace de puente TCP <-> RS-485.
Este servidor le envía la trama de sondeo cada N segundos, parsea la
respuesta de la sonda y entrega los valores a un callback.

Mapa de registros (esclavo 1, función 03):
    regs 0-1  Oxígeno disuelto  float32  mg/L
    regs 2-3  Temperatura       float32  °C
    regs 4-5  Saturación        float32  %
"""

import asyncio
import struct
from typing import Optional

ESPERA_REGISTRO = 0.5   # segundos para que el módulo mande su paquete de registro
_MAX_BUFFER = 4096


def crc16(datos: bytes) -> bytes:
    """CRC-16/Modbus, devuelto byte bajo primero (como va en la trama)."""
    crc = 0xFFFF
    for b in datos:
        crc ^= b
        for _ in range(8):
            if crc & 1:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
    return bytes([crc & 0xFF, crc >> 8])


def construir_trama(esclavo: int = 1, inicio: int = 0, cantidad: int = 6) -> bytes:
    cuerpo = struct.pack(">BBHH", esclavo, 0x03, inicio, cantidad)
    return cuerpo + crc16(cuerpo)


TRAMA_SONDEO = construir_trama()

_CABECERA_RESPUESTA = bytes([0x01, 0x03, 0x0C])
_LARGO_RESPUESTA = 3 + 12 + 2  # cabecera + 6 registros + CRC


def parsear_respuesta(datos: bytes) -> Optional[dict]:
    """
    Busca en `datos` una respuesta válida de la sonda y extrae los tres floats.
    Tolera basura antes o después (heartbeats o paquetes propios del módulo).
    Devuelve None si no hay ninguna respuesta con CRC correcto.
    """
    desde = 0
    while True:
        i = datos.find(_CABECERA_RESPUESTA, desde)
        if i < 0:
            return None
        trama = datos[i:i + _LARGO_RESPUESTA]
        if len(trama) == _LARGO_RESPUESTA and crc16(trama[:-2]) == trama[-2:]:
            # Floats en DCBA (little endian) — verificado contra el hardware:
            # con ABCD o CDAB salen números astronómicos.
            od, temp, sat = struct.unpack("<fff", trama[3:15])
            return {"oxigeno_disuelto": od, "temperatura": temp, "saturacion": sat}
        desde = i + 1


async def atender_modulo(reader, writer, guardar, intervalo: float = 60.0, espera: float = 5.0,
                         registro_esperado: str = ""):
    """
    Atiende la conexión de un módulo: captura su paquete de registro,
    lo sondea cada `intervalo` segundos y pasa cada lectura a `guardar`.

    guardar(campos: dict, dispositivo: str | None) — corrutina.

    Si `registro_esperado` no está vacío, el paquete de registro del módulo
    debe contenerlo (p. ej. su SN); si no llega o no coincide, se cierra la
    conexión sin sondear. Es la defensa contra conexiones ajenas, ya que el
    socket va en texto plano.
    """
    origen = writer.get_extra_info("peername")
    dispositivo = None
    try:
        # El módulo suele anunciarse nada más conectar (paquete de registro/SN).
        try:
            registro = await asyncio.wait_for(reader.read(256), timeout=ESPERA_REGISTRO)
            if registro:
                dispositivo = registro.decode("utf-8", errors="replace").strip() or None
        except asyncio.TimeoutError:
            pass

        if registro_esperado and registro_esperado not in (dispositivo or ""):
            print(f"modbus: conexión de {origen} rechazada (registro {dispositivo!r})")
            return

        while True:
            writer.write(TRAMA_SONDEO)
            await writer.drain()

            buffer = b""
            limite = asyncio.get_event_loop().time() + espera
            campos = None
            while campos is None:
                restante = limite - asyncio.get_event_loop().time()
                if restante <= 0:
                    break  # silencio: la sonda no contestó (el err:1 de USR)
                try:
                    trozo = await asyncio.wait_for(reader.read(256), timeout=restante)
                except asyncio.TimeoutError:
                    break
                if not trozo:
                    return  # el módulo cerró la conexión
                buffer = (buffer + trozo)[-_MAX_BUFFER:]
                campos = parsear_respuesta(buffer)

            if campos is not None:
                await guardar(campos, dispositivo)

            await asyncio.sleep(intervalo)
    except (ConnectionResetError, BrokenPipeError, OSError):
        pass
    finally:
        try:
            writer.close()
        except Exception:
            pass
        print(f"modbus: conexión de {origen} ({dispositivo or 'sin registro'}) cerrada")
