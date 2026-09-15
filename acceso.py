"""
Sesiones del panel: una cookie firmada con HMAC, sin estado en el servidor.

Formato: "<expira_epoch>.<hmac_sha256_hex>". El secreto se deriva de la
clave del panel y del token del webhook, así sobrevive a reinicios sin
pedir otra variable de entorno. Cambiar la clave invalida todas las sesiones.
"""

import hashlib
import hmac
import time

DIAS_SESION = 30


def secreto_de(clave: str, token: str) -> bytes:
    return hashlib.sha256(f"{clave}|{token}|sonda-od".encode()).digest()


def _firma(expira: int, secreto: bytes) -> str:
    return hmac.new(secreto, str(expira).encode(), hashlib.sha256).hexdigest()


def emitir(secreto: bytes, dias: int = DIAS_SESION) -> str:
    expira = int(time.time()) + dias * 86400
    return f"{expira}.{_firma(expira, secreto)}"


def validar(valor: str, secreto: bytes) -> bool:
    if not valor or "." not in valor:
        return False
    expira_txt, firma = valor.split(".", 1)
    try:
        expira = int(expira_txt)
    except ValueError:
        return False
    if expira < time.time():
        return False
    return hmac.compare_digest(firma, _firma(expira, secreto))
