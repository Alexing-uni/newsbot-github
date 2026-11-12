"""Datos de bolsa de Yahoo Finance (endpoint v8 chart, sin clave) con la interfaz de ccxt que usa el bot.

Los símbolos son "CODE/EUR" y [market.CODE].ticker dice qué valor pedir ("RHM.DE"). Solo datos:
las órdenes de la cartera de bolsa son paper. API no oficial ni documentada: puede romperse.
- Sin User-Agent de navegador Yahoo responde 429. /v7/quote exige crumb: no se usa.
- Diarias: marcadas en la apertura (09:00 local, a veces 08:00), con la vela de hoy en curso.
  Se deduplican por fecha local.
- 15m: como mucho 60 días atrás (422 si se pide más). La serie de hoy acaba con la subasta de
  cierre (17:30, volumen 0) y, con el mercado abierto, una barra fuera de la rejilla en
  regularMarketTime: ambas se descartan. Yahoo añade la última barra aunque period2 sea pasado.
- Errores de red, 429 y 5xx -> ccxt.NetworkError (market.with_retry reintenta); 404 -> BadSymbol.
- Valores en otra divisa ([market.CODE].currency = "USD"): precios convertidos a EUR con el cambio
  actual (EURUSD=X). Un factor común no altera medias, RSI ni ATR %: solo pasa los importes a euros.
"""
import json
import math
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

import ccxt

URL = "https://query1.finance.yahoo.com/v8/finance/chart/"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/130.0.0.0 Safari/537.36")
INTERVAL = {"1d": "1d", "1h": "60m", "15m": "15m"}
MAX_DAYS = {"15m": 59, "1h": 729}  # Yahoo: 15m hasta 60 días atrás, 1h hasta 730 (con margen)
TTL = {"1d": 3600.0, "1h": 240.0, "15m": 240.0}
BARS_PER_SESSION = {"1d": 1, "1h": 9, "15m": 34}
RANGES = {  # (range, sesiones que cubre aprox.)
    "1d": [("1mo", 20), ("3mo", 60), ("6mo", 125), ("1y", 250), ("2y", 500)],
    "1h": [("5d", 5), ("1mo", 20), ("3mo", 60), ("6mo", 125), ("1y", 250), ("2y", 500)],
    "15m": [("5d", 5), ("1mo", 20), ("60d", 60)],
}


def http_get(url: str, timeout: float = 20.0) -> dict:
    """GET JSON con User-Agent de navegador, traduciendo los errores a los de ccxt."""
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        if e.code == 429 or e.code >= 500:
            raise ccxt.NetworkError(f"Yahoo HTTP {e.code}: {url}") from e
        if e.code == 404:
            raise ccxt.BadSymbol(f"Yahoo 404 (valor inexistente): {url}") from e
        raise ccxt.BadRequest(f"Yahoo HTTP {e.code}: {url}") from e  # 400/422: rango no válido
    except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
        raise ccxt.RequestTimeout(f"Yahoo sin respuesta ({e}): {url}") from e
    except json.JSONDecodeError as e:  # página de error o respuesta cortada
        raise ccxt.NetworkError(f"Yahoo respuesta no JSON: {url}") from e


class YahooExchange:
    id = "yahoo"

    def __init__(self, markets: dict, fetch=http_get, clock=time.time, sleep=time.sleep,
                 spacing: float = 0.3):  # ~3 peticiones/s: rápido sin que Yahoo corte (429)
        self.markets_cfg = markets
        self.options: dict = {}
        self.has: dict = {}
        self._fetch, self._clock, self._sleep, self._spacing = fetch, clock, sleep, spacing
        self._cache: dict[tuple, tuple[float, list, dict]] = {}
        self._meta: dict[str, tuple[float, dict]] = {}  # ticker -> (cuándo, meta intradía)
        self._last = 0.0
        self._lock = threading.Lock()
        self._fx: dict[str, tuple[float, float]] = {}  # divisa -> (cuándo, unidades por 1 EUR)

    # --- interfaz ccxt ---------------------------------------------------------------------------
    def milliseconds(self) -> int:
        return int(self._clock() * 1000)

    def market(self, symbol: str) -> dict:
        code, _, quote = symbol.partition("/")
        m = self.markets_cfg.get(code)
        if not m or not m.get("ticker"):
            raise ccxt.BadSymbol(f"{symbol} no está en [market]")
        return {"symbol": symbol, "id": m["ticker"], "base": code, "quote": quote,
                "limits": {"cost": {"min": 1.0}, "amount": {"min": None}}, "precision": {},
                "info": m}

    def load_markets(self, reload: bool = False) -> dict:
        return {f"{code}/EUR": self.market(f"{code}/EUR") for code in self.markets_cfg}

    def fetch_ohlcv(self, symbol: str, timeframe: str = "1d", since: int | None = None,
                    limit: int | None = None, params: dict | None = None) -> list[list[float]]:
        if timeframe not in INTERVAL:
            raise ccxt.NotSupported(f"Yahoo no tiene velas de {timeframe}")
        now = self._clock()
        until = (params or {}).get("until")
        if since is None and until is None:
            window: tuple = (self._range(timeframe, limit),)
        else:
            p1 = since // 1000 if since is not None else now - 365 * 86400
            if timeframe in MAX_DAYS:
                p1 = max(p1, int(now - MAX_DAYS[timeframe] * 86400))
            p2 = min(int(until // 1000), int(now)) if until is not None else None
            if p1 >= (p2 or now):
                return []
            window = (int(p1), p2)
        try:
            bars, _ = self._chart(symbol, timeframe, window)
        except ccxt.BadSymbol:
            raise
        except ccxt.BadRequest:  # 400/422: ventana fuera de lo que Yahoo da
            return []
        if since is not None:
            bars = [b for b in bars if b[0] >= since]
            return bars[:limit] if limit else bars
        return bars[-limit:] if limit else bars

    def fx_rate(self, currency: str) -> float:
        """Unidades de `currency` por 1 EUR (1.0 para EUR), con caché de 4 min."""
        if currency in ("EUR", "", None):
            return 1.0
        seen = self._fx.get(currency)
        if seen and self._clock() - seen[0] < TTL["15m"]:
            return seen[1]
        with self._lock:
            wait = self._last + self._spacing - self._clock()
            if wait > 0:
                self._sleep(wait)
            self._last = self._clock()
        data = self._fetch(URL + urllib.parse.quote(f"EUR{currency}=X") + "?interval=15m&range=1d")
        rate = float(((data.get("chart") or {}).get("result") or [{}])[0]["meta"]["regularMarketPrice"])
        if not rate > 0:
            raise ccxt.NetworkError(f"Yahoo: cambio EUR{currency} no válido")
        self._fx[currency] = (self._clock(), rate)
        return rate

    def currency(self, symbol: str) -> str:
        """Divisa en la que cotiza el valor (la de [market.CODE].currency; EUR por defecto)."""
        return self.market(symbol)["info"].get("currency", "EUR")

    def fetch_ticker(self, symbol: str) -> dict:
        meta = self._intraday_meta(symbol)
        fx = self.fx_rate(self.currency(symbol))
        price, ts = float(meta["regularMarketPrice"]) / fx, int(meta["regularMarketTime"]) * 1000
        return {"symbol": symbol, "last": price, "close": price, "timestamp": ts}

    def trading_today(self, symbol: str, now_ms: int) -> bool:
        """True si el valor ha cotizado hoy (hora local de su bolsa): False en festivos."""
        meta = self._intraday_meta(symbol)
        tz = ZoneInfo(meta.get("exchangeTimezoneName") or "UTC")
        last = datetime.fromtimestamp(int(meta["regularMarketTime"]), tz).date()
        return last == datetime.fromtimestamp(now_ms / 1000, tz).date()

    # --- internos --------------------------------------------------------------------------------
    @staticmethod
    def _range(timeframe: str, limit: int | None) -> str:
        options = RANGES[timeframe]
        if not limit:
            return options[-1][0]
        sessions = math.ceil(limit / BARS_PER_SESSION[timeframe]) + 1  # +1: la de hoy, en curso
        return next((r for r, n in options if n >= sessions), options[-1][0])

    def _intraday_meta(self, symbol: str) -> dict:
        ticker = self.market(symbol)["id"]
        seen = self._meta.get(ticker)
        if seen and self._clock() - seen[0] < TTL["15m"]:
            return seen[1]
        return self._chart(symbol, "15m", ("1d",))[1]

    def _chart(self, symbol: str, timeframe: str, window: tuple) -> tuple[list, dict]:
        ticker = self.market(symbol)["id"]
        key = (ticker, timeframe, window)
        hit = self._cache.get(key)
        if hit and self._clock() < hit[0]:
            return hit[1], hit[2]
        q = {"interval": INTERVAL[timeframe]}
        q |= {"range": window[0]} if len(window) == 1 else {
            "period1": window[0], "period2": window[1] or int(self._clock())}
        data = self._get(URL + urllib.parse.quote(ticker) + "?" + urllib.parse.urlencode(q))
        chart = data.get("chart") or {}
        if not chart.get("result"):
            err = chart.get("error") or {}
            msg = f"Yahoo {ticker}: {err.get('code')} {err.get('description')}"
            raise (ccxt.BadSymbol if "not found" in str(err).lower() else ccxt.BadRequest)(msg)
        res = chart["result"][0]
        meta = res.get("meta") or {}
        quote, cur = symbol.partition("/")[2], self.currency(symbol)
        if meta.get("currency") != cur:
            raise ValueError(f"{symbol} ({ticker}) cotiza en {meta.get('currency')}, no en {cur}")
        bars = parse(res, timeframe, window[1] if len(window) == 2 else None)
        if cur != quote:  # p. ej. USD -> EUR: todos los precios por el mismo factor (volumen igual)
            fx = self.fx_rate(cur)
            bars = [[b[0], b[1] / fx, b[2] / fx, b[3] / fx, b[4] / fx, b[5]] for b in bars]
        now = self._clock()
        if timeframe != "1d":
            self._meta[ticker] = (now, meta)
        self._cache[key] = (now + TTL[timeframe], bars, meta)
        return bars, meta

    def _get(self, url: str) -> dict:
        with self._lock:  # como mucho ~3 peticiones por segundo (spacing)
            wait = self._last + self._spacing - self._clock()
            if wait > 0:
                self._sleep(wait)
            self._last = self._clock()
        return self._fetch(url)


def parse(res: dict, timeframe: str, period2: int | None = None) -> list[list[float]]:
    """Velas [ms, o, h, l, c, v] de un result del chart: sin filas nulas, sin la subasta de cierre
    ni la barra en vivo (15m/1h), hasta `period2` y las diarias únicas por fecha local."""
    meta = res.get("meta") or {}
    q = ((res.get("indicators") or {}).get("quote") or [{}])[0]
    tz = ZoneInfo(meta.get("exchangeTimezoneName") or "UTC")
    cols = [q.get(k) or [] for k in ("open", "high", "low", "close", "volume")]
    out: dict = {}
    for i, t in enumerate(res.get("timestamp") or []):
        o, h, lo, c, v = (col[i] if i < len(col) else None for col in cols)
        if None in (o, h, lo, c) or (period2 is not None and t > period2):
            continue
        if timeframe == "1d":
            key = datetime.fromtimestamp(t, tz).date()
            first = out.get(key, [t * 1000])[0]  # se queda la marca de la apertura
            out[key] = [first, float(o), float(h), float(lo), float(c), float(v or 0)]
        elif t % 900 == 0 and v:  # en la rejilla y con volumen (fuera: subasta y barra en vivo)
            out[t] = [t * 1000, float(o), float(h), float(lo), float(c), float(v)]
    return sorted(out.values(), key=lambda b: b[0])
