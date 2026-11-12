import math
import statistics
import time

import ccxt

DAY_MS = 86_400_000


def make_exchange(name: str, markets: dict | None = None, **params) -> ccxt.Exchange:
    """Fuente de datos (y exchange para operar en cripto). "yahoo": datos de bolsa con la misma
    interfaz que ccxt (features/yahoo.py); `markets` = sección [market] del config de bolsa."""
    if name == "yahoo":
        from newsbot.features.yahoo import YahooExchange
        return YahooExchange(markets or {})
    return getattr(ccxt, name)({"enableRateLimit": True, **params})


def with_retry(fn, *args, tries: int = 4, base_delay: float = 2.0, sleep=time.sleep, **kwargs):
    for attempt in range(tries):
        try:
            return fn(*args, **kwargs)
        except ccxt.NetworkError:  # incluye ExchangeNotAvailable y RequestTimeout
            if attempt == tries - 1:
                raise
            sleep(base_delay * 2**attempt)


TIMEFRAME_MS = {"1d": DAY_MS, "4h": 14_400_000, "1h": 3_600_000, "15m": 900_000, "5m": 300_000}


def closed_candles(ex, symbol: str, now_ms: int, limit: int = 250,
                   timeframe: str = "1d") -> list[list[float]]:
    candles = with_retry(ex.fetch_ohlcv, symbol, timeframe, limit=limit)
    # Solo velas cerradas: la vela en curso todavía no existe en el pasado (evita look-ahead).
    return [c for c in candles if c[0] + TIMEFRAME_MS[timeframe] <= now_ms]


def ema(values: list[float], n: int) -> float:
    alpha, out = 2 / (n + 1), values[0]
    for v in values[1:]:
        out = alpha * v + (1 - alpha) * out
    return out


def intraday_score(closes: list[float], fast: int, slow: int) -> float:
    """Impulso de las últimas horas en [-1, 1]: EMA rápida frente a lenta en velas cortas.
    Una brecha del 1,5 % ya es señal completa. Describe el movimiento reciente; no lo predice."""
    if len(closes) < slow:
        return 0.0
    gap = ema(closes, fast) / ema(closes, slow) - 1
    return max(-1.0, min(1.0, gap / 0.015))


def last_price(ex, symbol: str) -> float:
    return float(with_retry(ex.fetch_ticker, symbol)["last"])


def min_order_eur(ex, symbol: str, fallback: float) -> float:
    market = with_retry(ex.load_markets)[symbol]
    value = (market.get("limits", {}).get("cost") or {}).get("min")
    return float(value) if value else fallback


def sma(values: list[float], n: int) -> float:
    return sum(values[-n:]) / n


def trend_score(closes: list[float], fast: int, slow: int) -> float:
    """SMA rápida frente a lenta, en [-1, 1]. Una brecha del 5 % ya es señal completa."""
    if len(closes) < slow:
        return 0.0
    gap = sma(closes, fast) / sma(closes, slow) - 1
    return max(-1.0, min(1.0, gap / 0.05))


def atr_pct(candles: list[list[float]], n: int = 14) -> float:
    """ATR(n) como fracción del último cierre."""
    if len(candles) < n + 1:
        return 0.0
    trs = [max(c[2] - c[3], abs(c[2] - prev[4]), abs(c[3] - prev[4]))
           for prev, c in zip(candles[-n - 1:-1], candles[-n:], strict=True)]
    return sum(trs) / n / candles[-1][4]


def realized_vol(closes: list[float], n: int = 30) -> float:
    """Volatilidad anualizada (365 días: cripto cotiza todos los días) de los últimos n retornos."""
    if len(closes) < n + 1:
        return 0.0
    rets = [math.log(b / a) for a, b in zip(closes[-n - 1:-1], closes[-n:], strict=True)]
    return statistics.stdev(rets) * math.sqrt(365)


def regime(closes: list[float], n: int = 200) -> str:
    """bear: cierre < SMA200; lateral: hasta un 2 % por encima; bull: más arriba."""
    if len(closes) < n:
        return "unknown"
    ratio = closes[-1] / sma(closes, n) - 1
    return "bear" if ratio < 0 else "lateral" if ratio < 0.02 else "bull"


SHADOW = ("rsi", "bollinger", "momentum", "volume")  # se miden siempre; operan solo con peso > 0


def rsi_score(closes: list[float], n: int = 14) -> float:
    """RSI(n) de Wilder como señal contraria en [-1, 1]: sobreventa (RSI 0) = +1, sobrecompra = -1."""
    if len(closes) < n + 1:
        return 0.0
    deltas = [b - a for a, b in zip(closes[:-1], closes[1:], strict=True)]
    gain = sum(max(d, 0.0) for d in deltas[:n]) / n
    loss = sum(max(-d, 0.0) for d in deltas[:n]) / n
    for d in deltas[n:]:
        gain = (gain * (n - 1) + max(d, 0.0)) / n
        loss = (loss * (n - 1) + max(-d, 0.0)) / n
    if gain == loss == 0:
        return 0.0
    rsi = 100.0 if loss == 0 else 100 - 100 / (1 + gain / loss)
    return (50 - rsi) / 50


def bollinger_score(closes: list[float], n: int = 20, k: float = 2.0) -> float:
    """%B de Bollinger como señal contraria en [-1, 1]: banda inferior = +1, superior = -1."""
    if len(closes) < n:
        return 0.0
    window = closes[-n:]
    sd = statistics.pstdev(window)
    if sd == 0:
        return 0.0
    pct_b = (closes[-1] - (sma(window, n) - k * sd)) / (2 * k * sd)
    return max(-1.0, min(1.0, 1 - 2 * pct_b))


def momentum_score(closes: list[float], days: int = 7) -> float:
    """Momentum de serie temporal: retorno de `days` días; un 10 % ya es señal completa."""
    if len(closes) < days + 1:
        return 0.0
    return max(-1.0, min(1.0, (closes[-1] / closes[-days - 1] - 1) / 0.10))


def volume_score(candles: list[list[float]], n: int = 20) -> float:
    """Volumen del último día frente a su media de `n`, con el signo del movimiento del día:
    subir con mucho volumen = +, caer con mucho volumen = -. El doble de la media ya es completa."""
    if len(candles) < n + 1:
        return 0.0
    avg = sum(c[5] for c in candles[-n - 1:-1]) / n
    if avg <= 0:
        return 0.0
    last, prev = candles[-1], candles[-2]
    direction = 1.0 if last[4] > prev[4] else -1.0 if last[4] < prev[4] else 0.0
    return direction * max(0.0, min(1.0, last[5] / avg - 1))


def features(candles: list[list[float]], cfg: dict) -> dict:
    """Todo lo que la estrategia necesita del precio, calculado solo con velas cerradas."""
    s = cfg["strategy"]
    closes = [c[4] for c in candles]
    return {"trend": trend_score(closes, s["sma_fast"], s["sma_slow"]),
            "regime": regime(closes, s["sma_regime"]),
            "atr_pct": atr_pct(candles, cfg["risk"]["atr_days"]),
            "vol": realized_vol(closes),
            "rsi": rsi_score(closes), "bollinger": bollinger_score(closes),
            "momentum": momentum_score(closes), "volume": volume_score(candles)}
