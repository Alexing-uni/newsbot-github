"""Análisis técnico clásico como componentes sombra de la bolsa (peso 0: learn mide si predicen).

Todas las puntuaciones están en [-1, 1] (positivo = alcista) y usan solo velas diarias cerradas
[ms, open, high, low, close, volume]; con pocas velas devuelven 0.
"""


def _clamp(x: float) -> float:
    return max(-1.0, min(1.0, x))


def breakout_score(candles: list[list[float]], n: int = 20) -> float:
    """Canal de Donchian de las `n` velas ANTERIORES a la última: cierre por encima del máximo = +1
    (ruptura alcista), por debajo del mínimo = -1; dentro, la posición lineal en el canal."""
    if len(candles) < n + 1:
        return 0.0
    window = candles[-n - 1:-1]
    hi, lo, close = max(c[2] for c in window), min(c[3] for c in window), candles[-1][4]
    if close >= hi:
        return 1.0
    if close <= lo:
        return -1.0
    return _clamp(2 * (close - lo) / (hi - lo) - 1)


def _ema_series(values: list[float], n: int) -> list[float]:
    alpha, out = 2 / (n + 1), [values[0]]
    for v in values[1:]:
        out.append(alpha * v + (1 - alpha) * out[-1])
    return out


def macd_score(closes: list[float], fast: int = 12, slow: int = 26, signal: int = 9) -> float:
    """Histograma del MACD (línea MACD − su señal) dividido por el movimiento diario medio de las
    últimas `slow` sesiones (un ATR de cierres): un histograma de un movimiento diario ya es ±1."""
    if len(closes) < slow + signal:
        return 0.0
    line = [f - s for f, s in zip(_ema_series(closes, fast), _ema_series(closes, slow), strict=True)]
    hist = line[-1] - _ema_series(line[slow - 1:], signal)[-1]
    moves = [abs(b - a) for a, b in zip(closes[-slow - 1:-1], closes[-slow:], strict=True)]
    scale = sum(moves) / len(moves)
    return _clamp(hist / scale) if scale > 0 else 0.0


def high52_score(candles: list[list[float]], n: int = 252) -> float:
    """Cercanía al máximo de 52 semanas (George y Hwang, 2004): en el máximo = +1, un 10 % por
    debajo = 0, un 20 % o más = -1. Con menos de un año de velas usa las que haya (mín. 20)."""
    if len(candles) < 20:
        return 0.0
    window = candles[-n:]
    top = max(c[2] for c in window)
    if top <= 0:
        return 0.0
    below = 1 - candles[-1][4] / top
    return _clamp(1 - below / 0.10)


def dip_score(candles: list[list[float]], n: int = 5, min_moves: float = 2.5,
              price: float | None = None) -> float:
    """Compra de caídas: el precio (`price`, el de ahora; si no, el último cierre) cae al menos
    `min_moves` movimientos diarios medios desde el máximo de las últimas `n` sesiones (contando la
    de hoy). Con la tendencia de fondo alcista (media de 50 > media de 200, o de
    las que haya) es un posible rebote, hasta +1 a 5 movimientos; con la de fondo bajista es un
    cuchillo cayendo, hasta −0.5. Sin caída suficiente, 0."""
    if len(candles) < 60:
        return 0.0
    closes = [c[4] for c in candles]
    moves = [abs(b - a) / a for a, b in zip(closes[-21:-1], closes[-20:], strict=True) if a > 0]
    daily = sum(moves) / len(moves) if moves else 0.0
    top = max(c[2] for c in candles[-n - 1:])
    if daily <= 0 or top <= 0:
        return 0.0
    depth = (1 - (price or closes[-1]) / top) / daily
    if depth < min_moves:
        return 0.0
    strength = min(1.0, (depth - min_moves) / 3 + 0.2)
    long = closes[-200:]
    uptrend = sum(closes[-50:]) / 50 > sum(long) / len(long)
    return strength if uptrend else -0.5 * strength


def features(candles: list[list[float]], price: float | None = None) -> dict:
    """Los componentes técnicos de un valor, con sus velas diarias cerradas (y el precio de ahora)."""
    return {"breakout": breakout_score(candles), "macd": macd_score([c[4] for c in candles]),
            "high52": high52_score(candles), "dip": dip_score(candles, price=price)}
