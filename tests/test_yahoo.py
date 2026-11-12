"""Adaptador de Yahoo Finance: sin red (fetch inyectado), mismas formas que ccxt."""
import io
import urllib.error
import urllib.parse
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import ccxt
import pytest

from newsbot.features import market, yahoo

BERLIN = ZoneInfo("Europe/Berlin")
NOW = datetime(2026, 11, 2, 15, 0, tzinfo=UTC)  # lunes, 16:00 en Xetra
MARKETS = {"RHM": {"ticker": "RHM.DE", "isin": "DE0007030009", "name": "Rheinmetall"},
           "GOLD": {"ticker": "4GLD.DE"}}


def ts(day: int, hh: int, mm: int = 0) -> int:
    """28-oct..2-nov, hora de Xetra."""
    return int(datetime(2026, 10 if day > 15 else 11, day, hh, mm, tzinfo=BERLIN).timestamp())


def chart(stamps, closes, volumes=None, currency="EUR", rmt=None, price=None):
    closes = list(closes)
    return {"chart": {"error": None, "result": [{
        "meta": {"currency": currency, "exchangeTimezoneName": "Europe/Berlin",
                 "regularMarketPrice": price or closes[-1], "regularMarketTime": rmt or stamps[-1]},
        "timestamp": list(stamps),
        "indicators": {"quote": [{"open": closes, "high": closes, "low": closes, "close": closes,
                                  "volume": volumes or [100] * len(closes)}]}}]}}


class Clock:
    def __init__(self):
        self.t, self.slept = NOW.timestamp(), []

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.slept.append(s)
        self.t += s


class Fake:
    """fetch(url) que responde según el intervalo y apunta las URLs pedidas."""

    def __init__(self, by_interval):
        self.by_interval, self.urls = by_interval, []

    def __call__(self, url):
        self.urls.append(url)
        q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        out = self.by_interval[q["interval"][0]]
        if isinstance(out, Exception):
            raise out
        return out

    def query(self, i=-1):
        return {k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlparse(self.urls[i]).query).items()}


def intraday():
    """Lunes 2-nov: 09:00..10:00 + barra nula + barra en vivo fuera de rejilla + subasta 17:30."""
    stamps = [ts(2, 9), ts(2, 9, 15), ts(2, 9, 30), ts(2, 9, 45), ts(2, 10), ts(2, 10, 7), ts(2, 17, 30)]
    data = chart(stamps, [10, 11, 12, 13, 14, 15, 16], volumes=[5, 5, 5, 5, 5, 0, 0], rmt=ts(2, 15, 55),
                 price=15.5)
    data["chart"]["result"][0]["indicators"]["quote"][0]["close"][2] = None
    return data


def daily():
    """Diarias en la apertura; el 30-oct sale dos veces (08:00 y 09:00) y hay una fila nula."""
    stamps = [ts(28, 9), ts(29, 9), ts(30, 8), ts(30, 9), ts(31, 9), ts(2, 9)]
    data = chart(stamps, [1, 2, 3, 4, 5, 6])
    data["chart"]["result"][0]["indicators"]["quote"][0]["open"][4] = None
    return data


def make(by_interval, clock=None):
    clock = clock or Clock()
    fake = Fake(by_interval)
    return yahoo.YahooExchange(MARKETS, fetch=fake, clock=clock, sleep=clock.sleep), fake, clock


def test_intraday_drops_null_live_and_auction_bars():
    ex, fake, _ = make({"15m": intraday()})
    bars = ex.fetch_ohlcv("RHM/EUR", "15m", limit=100)
    assert [b[4] for b in bars] == [10, 11, 13, 14]  # sin nula, sin 10:07 (en vivo) ni 17:30 (subasta)
    assert bars[0] == [ts(2, 9) * 1000, 10.0, 10.0, 10.0, 10.0, 5.0]
    assert "/RHM.DE?" in fake.urls[0] and fake.query() == {"interval": "15m", "range": "5d"}
    assert ex.fetch_ohlcv("RHM/EUR", "15m", limit=2) == bars[-2:]


def test_daily_dedup_by_local_date_keeps_open_stamp():
    ex, fake, _ = make({"1d": daily()})
    bars = ex.fetch_ohlcv("RHM/EUR", "1d", limit=250)
    assert fake.query() == {"interval": "1d", "range": "2y"}
    assert [b[4] for b in bars] == [1, 2, 4, 6]  # 30-oct una vez (la última), 31-oct nula fuera
    assert bars[2][0] == ts(30, 8) * 1000  # marcada en la primera apertura del día
    now_ms = int(NOW.timestamp() * 1000)
    closed = [b for b in bars if b[0] + market.DAY_MS <= now_ms]
    assert closed == bars[:-1]  # la de hoy (en curso) no está cerrada


def test_currency_mismatch_names_the_symbol():
    ex, _, _ = make({"1d": chart([ts(2, 9)], [1], currency="USD")})
    with pytest.raises(ValueError, match="RHM/EUR"):
        ex.fetch_ohlcv("RHM/EUR", "1d")


def test_since_is_clamped_to_yahoo_limits():
    ex, fake, clock = make({"15m": intraday(), "60m": intraday(), "1d": daily()})
    old = int((NOW - timedelta(days=100)).timestamp() * 1000)
    ex.fetch_ohlcv("RHM/EUR", "15m", since=old)
    assert int(fake.query()["period1"]) == int(clock.t - 59 * 86400)
    assert int(fake.query()["period2"]) == int(clock.t)
    ex.fetch_ohlcv("RHM/EUR", "1h", since=int((NOW - timedelta(days=1000)).timestamp() * 1000))
    assert abs(int(fake.query()["period1"]) - (clock.t - 729 * 86400)) <= 2  # el espaciado avanza el reloj
    ex.fetch_ohlcv("RHM/EUR", "1d", since=old)  # diarias: sin recorte
    assert int(fake.query()["period1"]) == old // 1000
    n = len(fake.urls)
    assert ex.fetch_ohlcv("RHM/EUR", "15m", since=int(clock.t * 1000) + 1000) == []  # futuro: sin pedir
    assert len(fake.urls) == n


def test_since_limit_and_until_filtering():
    ex, fake, _ = make({"15m": intraday()})
    bars = ex.fetch_ohlcv("RHM/EUR", "15m", since=ts(2, 9, 15) * 1000, limit=2)
    assert [b[4] for b in bars] == [11, 13]  # desde since, las primeras `limit` (como ccxt)
    bars = ex.fetch_ohlcv("RHM/EUR", "15m", since=ts(2, 9) * 1000, params={"until": ts(2, 9, 45) * 1000})
    assert int(fake.query()["period2"]) == ts(2, 9, 45)
    assert [b[4] for b in bars] == [10, 11, 13]  # Yahoo añade barras posteriores: se filtran


def test_cache_ttl_per_interval():
    ex, fake, clock = make({"15m": intraday(), "1d": daily()})
    for _ in range(3):
        ex.fetch_ohlcv("RHM/EUR", "15m", limit=100)
        ex.fetch_ohlcv("RHM/EUR", "1d", limit=250)
    assert len(fake.urls) == 2
    clock.t += 5 * 60  # caduca el intradía (4 min), no el diario (1 h)
    ex.fetch_ohlcv("RHM/EUR", "15m", limit=100)
    ex.fetch_ohlcv("RHM/EUR", "1d", limit=250)
    assert len(fake.urls) == 3
    clock.t += 3600
    ex.fetch_ohlcv("RHM/EUR", "1d", limit=250)
    assert len(fake.urls) == 4


def test_requests_are_spaced():
    ex, fake, clock = make({"15m": intraday(), "1d": daily()})
    ex.fetch_ohlcv("RHM/EUR", "15m")
    ex.fetch_ohlcv("RHM/EUR", "1d")
    assert len(fake.urls) == 2 and clock.slept and sum(clock.slept) == pytest.approx(0.3)  # spacing


def test_ticker_and_trading_today_reuse_intraday_meta():
    ex, fake, _ = make({"15m": intraday()})
    ex.fetch_ohlcv("RHM/EUR", "15m", limit=100)
    t = ex.fetch_ticker("RHM/EUR")
    assert t["last"] == 15.5 and t["timestamp"] == ts(2, 15, 55) * 1000
    assert ex.trading_today("RHM/EUR", int(NOW.timestamp() * 1000))
    assert not ex.trading_today("RHM/EUR", int((NOW + timedelta(days=1)).timestamp() * 1000))
    assert len(fake.urls) == 1  # sin peticiones extra
    ex2, fake2, _ = make({"15m": chart([ts(30, 17)], [9.0], rmt=ts(30, 17, 30))})  # festivo: viernes
    assert not ex2.trading_today("RHM/EUR", int(NOW.timestamp() * 1000))
    assert fake2.query() == {"interval": "15m", "range": "1d"}


def test_markets_and_factory():
    ex = market.make_exchange("yahoo", markets=MARKETS)
    assert isinstance(ex, yahoo.YahooExchange) and ex.options == {} and ex.has == {}
    assert market.min_order_eur(ex, "RHM/EUR", 5.0) == 1.0
    assert set(ex.load_markets()) == {"RHM/EUR", "GOLD/EUR"}
    assert ex.market("GOLD/EUR")["id"] == "4GLD.DE"
    with pytest.raises(ccxt.BadSymbol):
        ex.market("XXX/EUR")
    with pytest.raises(ccxt.NotSupported):
        ex.fetch_ohlcv("RHM/EUR", "4h")


def test_bad_range_is_empty_and_missing_symbol_raises():
    ex, _, _ = make({"15m": ccxt.BadRequest("422"), "1d": ccxt.BadSymbol("404")})
    assert ex.fetch_ohlcv("RHM/EUR", "15m", limit=10) == []
    with pytest.raises(ccxt.BadSymbol):
        ex.fetch_ohlcv("RHM/EUR", "1d")
    ex, _, _ = make({"1d": {"chart": {"result": None, "error": {"code": "Not Found",
                                                                  "description": "No data found"}}}})
    with pytest.raises(ccxt.BadSymbol):
        ex.fetch_ohlcv("RHM/EUR", "1d")


class Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.mark.parametrize("exc, expected", [
    (urllib.error.HTTPError("u", 429, "Too Many", {}, None), ccxt.NetworkError),
    (urllib.error.HTTPError("u", 503, "Unavailable", {}, None), ccxt.NetworkError),
    (urllib.error.URLError("dns"), ccxt.NetworkError),
    (TimeoutError("timed out"), ccxt.RequestTimeout),
    (urllib.error.HTTPError("u", 404, "Not Found", {}, None), ccxt.BadSymbol),
    (urllib.error.HTTPError("u", 422, "Unprocessable", {}, None), ccxt.BadRequest),
])
def test_http_errors_map_to_ccxt(monkeypatch, exc, expected):
    def boom(req, timeout):
        raise exc
    monkeypatch.setattr(yahoo.urllib.request, "urlopen", boom)
    with pytest.raises(expected):
        yahoo.http_get("https://query1.finance.yahoo.com/v8/finance/chart/RHM.DE")


def test_http_get_sends_browser_agent_and_timeout(monkeypatch):
    seen = {}

    def ok(req, timeout):
        seen.update(ua=req.get_header("User-agent"), timeout=timeout)
        return Resp(b'{"chart": {"result": []}}')
    monkeypatch.setattr(yahoo.urllib.request, "urlopen", ok)
    assert yahoo.http_get("https://x") == {"chart": {"result": []}}
    assert seen == {"ua": yahoo.UA, "timeout": 20.0}


def test_network_errors_are_retried():
    calls = []

    def flaky(url):
        calls.append(url)
        if len(calls) < 3:
            raise ccxt.NetworkError("429")
        return intraday()
    clock = Clock()
    ex = yahoo.YahooExchange(MARKETS, fetch=flaky, clock=clock, sleep=clock.sleep)
    bars = market.with_retry(ex.fetch_ohlcv, "RHM/EUR", "15m", limit=100, sleep=lambda s: None)
    assert len(calls) == 3 and len(bars) == 4
