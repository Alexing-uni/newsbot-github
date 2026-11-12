"""Fear & Greed Index de alternative.me (sin key). Atribución: https://alternative.me/crypto/"""
from newsbot.http import get_json

URL = "https://api.alternative.me/fng/?limit=1"


def fetch(get=get_json) -> int:
    return int(get(URL)["data"][0]["value"])  # 0 = miedo extremo, 100 = codicia extrema


def contrarian(value: int) -> float:
    """[-1, 1]. Solo cuenta en los extremos (<=25 o >=75) y al revés: miedo extremo suma, codicia
    extrema resta. En la zona media es 0: ahí el índice no aporta nada que no diga ya la tendencia.
    """
    x = (value - 50) / 50
    return -x if abs(x) >= 0.5 else 0.0
