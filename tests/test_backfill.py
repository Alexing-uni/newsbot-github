"""Relleno histórico: sin look-ahead, sin noticias, idempotente y fuera de las vistas en vivo."""
import json
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from newsbot.bot import _connect
from newsbot.features import market
from newsbot.research import backfill, learn
from newsbot.storage import db

DAY, Q = market.DAY_MS, market.TIMEFRAME_MS["15m"]
END = datetime(2026, 11, 2, tzinfo=UTC)
END_MS = int(END.timestamp() * 1000)


def candles(step, n, start_price=100.0, drift=0.001):
    first = END_MS - n * step
    return [[first + i * step, 1, 1, 1, start_price * (1 + drift) ** i, 10.0] for i in range(n)]


class HistExchange:
    def __init__(self):
        self.series = {"1d": candles(DAY, 800), "15m": candles(Q, 96 * 12),
                       "1h": candles(market.TIMEFRAME_MS["1h"], 24 * 400)}

    def fetch_ohlcv(self, symbol, timeframe, since=None, limit=None):
        out = [c for c in self.series[timeframe] if since is None or c[0] >= since]
        return out[:limit] if limit else out


def fng_get(url):
    return {"data": [{"value": "20", "timestamp": str(int((END - timedelta(days=d)).timestamp()))}
                     for d in range(15)]}


def test_ticks_only_use_closed_candles(cfg):
    daily, short = candles(DAY, 400), candles(Q, 96 * 12)
    start = int((END - timedelta(days=3)).timestamp())
    rows = backfill.build_ticks("BTC/EUR", daily, short, {}, cfg, start, int(END.timestamp()))
    assert len(rows) == 3 * 96 and rows[0]["ts"] == start
    for r in rows[::37]:  # el precio es el cierre de la última vela de 15 min ya cerrada
        closed = [c for c in short if c[0] + Q <= r["ts"] * 1000]
        assert r["price"] == closed[-1][4]
        assert r["signal"] is None and r["action"] == "BACKFILL" and "news" not in r["raw"]
        assert r["raw"]["trend"] == market.features(
            [c for c in daily if c[0] + DAY <= r["ts"] * 1000][-250:], cfg)["trend"]


def test_fng_uses_previous_day(cfg):
    """El F&G del día D se publica a lo largo de D: un tick de D usa el de D-1."""
    daily, short = candles(DAY, 400), candles(Q, 96 * 3)
    by_day = {"2026-10-31": 10, "2026-11-01": 90}  # 31: miedo extremo, 1: codicia extrema
    start = int(datetime(2026, 11, 1, 12, tzinfo=UTC).timestamp())
    rows = backfill.build_ticks("BTC/EUR", daily, short, by_day, cfg, start, start + 900)
    assert rows[0]["raw"]["fng"] > 0  # usa el 31 (miedo → suma), no el 1 (codicia → resta)


def test_run_is_idempotent_and_hidden_from_live_views(cfg):
    now = END
    with _connect(cfg) as con:
        db.save_ticks(con, now, [{"symbol": "BTC/EUR", "price": 1.0, "signal": 0.1,
                                  "action": "HOLD", "reason": "", "held": False, "raw": {},
                                  "contributions": {}}])
        first = backfill.run(con, cfg, HistExchange(), now, days=10, get=fng_get)
        again = backfill.run(con, cfg, HistExchange(), now, days=10, get=fng_get)
        n = con.execute("SELECT count(*) FROM ticks WHERE action = 'BACKFILL'").fetchone()[0]
        assert first == again == {"BTC/EUR": 10 * 96} and n == 10 * 96  # no se duplica
        assert [t["action"] for t in db.latest_ticks(con)] == ["HOLD"]  # /senales: solo en vivo
        raw = json.loads(con.execute("SELECT raw_json FROM ticks WHERE action = 'BACKFILL' "
                                     "ORDER BY ts DESC LIMIT 1").fetchone()[0])
        assert raw["fng"] == 1.0 * (30 / 50)  # F&G 20 (miedo extremo) suma
        ev = learn.evaluate(con, cfg, now + timedelta(hours=1), "4h")
        assert ev["n"] > 0 and ev["components"]["trend"]["n"] > 0
        assert ev["components"]["news"]["n"] == 0 and ev["fused"]["n"] == 0  # sin noticias
        p = learn.progress(con, cfg, now + timedelta(hours=1))
        assert p["backfill"] == 10 * 96 and "del histórico" in learn.progress_lines(p)[2]


# --- bolsa: horario de mercado, sin F&G y límite de 59 días de Yahoo ---------------------------------
MADRID = ZoneInfo("Europe/Madrid")
SESSION = {"tz": "Europe/Madrid", "days": [0, 1, 2, 3, 4], "open": "09:20", "close": "17:30"}


def session_candles(days, skip=()):
    """Velas de 15 min de 09:00 a 17:15 (hora de Madrid) en los días hábiles dados, salvo `skip`."""
    out = []
    for d in days:
        if d.weekday() < 5 and d not in skip:
            first = datetime.combine(d, datetime.min.time(), MADRID).replace(hour=9)
            out += [[int((first + timedelta(minutes=15 * i)).timestamp() * 1000), 1, 1, 1, 100.0 + i, 10.0]
                    for i in range(34)]
    return out


def test_session_keeps_only_open_market_steps(cfg):
    cfg["session"] = SESSION
    holiday = date(2026, 10, 29)
    short = session_candles([date(2026, 10, 12) + timedelta(days=i) for i in range(20)], skip={holiday})
    start = int(datetime(2026, 10, 28, tzinfo=UTC).timestamp())
    rows = backfill.build_ticks("RHM/EUR", candles(DAY, 400), short, {}, cfg, start, int(END.timestamp()))
    local = [datetime.fromtimestamp(r["ts"], MADRID) for r in rows]
    assert {t.date() for t in local} == {date(2026, 10, 28), date(2026, 10, 30)}  # ni festivo ni finde
    assert len(rows) == 2 * 32  # de 09:30 a 17:15: con la vela de 15 min anterior recién cerrada
    assert min(t.time() for t in local).isoformat() == "09:30:00"
    assert max(t.time() for t in local).isoformat() == "17:15:00"
    for r in rows[::7]:
        assert r["price"] == [c for c in short if c[0] + Q <= r["ts"] * 1000][-1][4]


def test_session_absent_keeps_round_the_clock(cfg):
    short = session_candles([date(2026, 10, 12) + timedelta(days=i) for i in range(20)])
    start = int(datetime(2026, 10, 28, tzinfo=UTC).timestamp())
    rows = backfill.build_ticks("BTC/EUR", candles(DAY, 400), short, {}, cfg, start, int(END.timestamp()))
    assert len(rows) == 5 * 96  # cripto: igual que antes, también con el mercado cerrado


def test_fng_gate(cfg):
    cfg["news"]["fng"] = False

    def no_fng(url):
        raise AssertionError("no debe pedir el F&G")
    with _connect(cfg) as con:
        assert backfill.run(con, cfg, HistExchange(), END, days=3, get=no_fng) == {"BTC/EUR": 3 * 96}
        raws = [json.loads(r[0]) for r in con.execute("SELECT raw_json FROM ticks")]
    assert raws and all("fng" not in r for r in raws)


class SinceExchange(HistExchange):
    def __init__(self):
        super().__init__()
        self.since = {}

    def fetch_ohlcv(self, symbol, timeframe, since=None, limit=None):
        self.since.setdefault(timeframe, since)
        return super().fetch_ohlcv(symbol, timeframe, since, limit)


def test_intraday_fetch_is_clamped_for_yahoo(cfg):
    end_ms = int(END.timestamp() * 1000)
    with _connect(cfg) as con:
        ex = SinceExchange()
        backfill.run(con, cfg, ex, END, days=60, get=fng_get)
        assert ex.since["15m"] == end_ms - 62 * DAY  # cripto: sin límite, como antes
        cfg["general"]["exchange"] = "yahoo"
        ex = SinceExchange()
        backfill.run(con, cfg, ex, END, days=60, get=fng_get)
        assert ex.since["15m"] == end_ms - 59 * DAY
        rows = con.execute("SELECT ts, raw_json FROM ticks WHERE action = 'BACKFILL' ORDER BY ts").fetchall()
        short = [r for r in rows if "intraday" in json.loads(r["raw_json"])]
        assert min(r["ts"] for r in short) >= (end_ms - 57 * DAY) // 1000  # 15 min: solo lo que da Yahoo
        assert rows[0]["ts"] < (end_ms - 59 * DAY) // 1000  # antes, el relleno en velas de 1 h


def test_yahoo_fills_older_history_with_hourly_ticks_without_intraday(cfg):
    cfg["general"]["exchange"] = "yahoo"
    with _connect(cfg) as con:
        backfill.run(con, cfg, HistExchange(), END, days=180, get=fng_get)
        rows = con.execute("SELECT ts, raw_json FROM ticks WHERE action = 'BACKFILL' ORDER BY ts").fetchall()
    cut = (END_MS - 57 * DAY) // 1000
    old, new = [r for r in rows if r["ts"] < cut], [r for r in rows if r["ts"] >= cut]
    assert old and new and rows[0]["ts"] <= (END_MS - 178 * DAY) // 1000
    old_raw = [json.loads(r["raw_json"]) for r in old]
    assert all("intraday" not in r and r["_tf"] == "1h" and "trend" in r for r in old_raw)
    assert all(b["ts"] - a["ts"] >= 3600 for a, b in zip(old, old[1:], strict=False))  # uno por hora
    assert all("intraday" in json.loads(r["raw_json"]) for r in new)
