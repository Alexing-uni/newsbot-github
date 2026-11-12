"""Fase 2 de EE. UU.: compra de caídas, pensamiento, repaso de pérdidas, gráfico y sendPhoto."""
import json
import urllib.request
from contextlib import closing
from datetime import UTC, datetime, timedelta

import pytest

from newsbot import http
from newsbot.reporting import chart
from newsbot.stocks import insight, technical
from newsbot.storage import db

NOW = datetime(2026, 11, 3, 18, 0, tzinfo=UTC)
NAMES = {"news": "noticias", "trend": "tendencia"}


def bars(closes):
    return [[i * 86_400_000, c, c * 1.005, c * 0.995, c, 1000.0] for i, c in enumerate(closes)]


def test_dip_score_rebound_vs_falling_knife():
    calm = [100 * 1.002**i + (0.3 if i % 2 else -0.3) for i in range(220)]
    assert technical.dip_score(bars(calm)) == 0.0  # sin caída
    up_dip = bars(calm[:-1] + [calm[-2] * 0.93])  # −7 % de golpe en tendencia alcista
    assert technical.dip_score(up_dip) > 0.5
    down = [100 * 0.998**i + (0.3 if i % 2 else -0.3) for i in range(220)]
    knife = technical.dip_score(bars(down[:-1] + [down[-2] * 0.93]))
    assert -0.5 <= knife < 0  # tendencia de fondo bajista: cuchillo cayendo
    assert technical.dip_score(bars(calm[:30])) == 0.0  # pocas velas


@pytest.fixture
def cfg():
    return {"costs": {"fee_pct": 0.0015, "slippage_pct": 0.0005}, "risk": {"take_profit_pct": 0.04},
            "general": {"symbols": ["A/EUR", "B/EUR", "C/EUR"]}}


def test_buy_and_sell_thought_count_commissions(cfg):
    plan = {"eur": 10.0, "price": 100.0, "stop": 95.0}
    t = insight.buy_thought(cfg, plan, "noticias +0.30", {"kronos": 0.4, "dip": 0.6})
    # objetivo +4 % de 10 € = 0.40 − 0.04 de comisiones; stop −5 % = −0.50 − 0.04
    assert "Objetivo +4.0 % (104.00 €): ≈ +0.36 € netos" in t and "pierdo ≈ −0.54 €" in t
    assert "Kronos prevé subida" in t and "caída fuerte" in t
    s = insight.sell_thought(cfg, {"qty": 0.1, "avg_entry": 100.0}, 110.0, "objetivo de beneficio")
    assert s.startswith("Vendo: objetivo de beneficio") and "+0.98 €" in s


def _tick(con, sym, ts, price, raw, contrib=None):
    with con:
        con.execute("INSERT INTO ticks(ts, created, symbol, price, signal, action, raw_json, "
                    "contributions_json) VALUES (?,?,?,?,0,'HOLD',?,?)",
                    (int(ts.timestamp()), ts.isoformat(), sym, price, json.dumps(raw),
                     json.dumps(contrib or {})))


def test_loss_review_blames_the_market_and_saves_the_lesson(cfg, tmp_path):
    t0 = NOW - timedelta(hours=30)
    with closing(db.connect(tmp_path / "n.db")) as con:
        _tick(con, "A/EUR", t0, 100.0, {"news": 0.6, "trend": 0.2}, {"news": 0.3, "trend": 0.1})
        _tick(con, "B/EUR", t0, 50.0, {})
        _tick(con, "C/EUR", t0, 20.0, {})
        _tick(con, "A/EUR", NOW, 95.0, {"news": -0.4, "trend": 0.1})
        _tick(con, "B/EUR", NOW, 47.0, {})
        _tick(con, "C/EUR", NOW, 18.8, {})
        lines = insight.loss_review(con, cfg, "A/EUR", t0.isoformat(), 100.0, 95.0, 0.1, 0.03, "stop-loss",
                                    NOW, NAMES)
        saved = json.loads(db.KV(con).get("lessons"))
    assert lines[0] == "A: −5.0 % (−0.53 €) en 30 h" and lines[1] == "Salida: stop-loss"
    assert "se movió −6.0 %" in lines[2] and "fue más el mercado" in lines[2]
    assert "Compré sobre todo por noticias (+0.60); ahora −0.40 → se giró" in lines
    assert lines[-1].startswith("Lección: compré con el mercado girándose")
    assert saved[-1]["symbol"] == "A/EUR" and saved[-1]["ret"] == pytest.approx(-0.05)


def test_tradingview_url_and_png():
    c = {"market": {"RHM": {"ticker": "RHM.DE"}, "NVDA": {"ticker": "NVDA"}, "X": {"tv": "NASDAQ:X"}}}
    assert chart.tradingview_url(c, "RHM/EUR").endswith("?symbol=XETR:RHM")
    assert chart.tradingview_url(c, "NVDA/EUR").endswith("?symbol=NVDA")
    assert chart.tradingview_url(c, "X/EUR").endswith("?symbol=NASDAQ:X")
    candles = [[1_762_000_000_000 + i * 900_000, 0, 0, 0, 100 + i * 0.1, 0] for i in range(60)]
    img = chart.png(candles, "RHM", {"entrada": 101.0, "stop": 97.0})
    assert img[:8] == b"\x89PNG\r\n\x1a\n" and len(img) > 5000


def test_post_multipart_builds_the_form(monkeypatch):
    seen = {}

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, *a):
            return b'{"ok": true}'

    def urlopen(req, timeout):
        seen["ctype"], seen["body"] = req.get_header("Content-type"), req.data
        return Resp()

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    assert http.post_multipart("https://x/sendPhoto", {"chat_id": "42", "caption": "hola"},
                               {"photo": ("g.png", b"PNGDATA", "image/png")}) == {"ok": True}
    boundary = seen["ctype"].split("boundary=")[1]
    body = seen["body"]
    assert body.startswith(f"--{boundary}\r\n".encode()) and body.endswith(f"--{boundary}--\r\n".encode())
    assert b'name="caption"\r\n\r\nhola\r\n' in body and b'filename="g.png"' in body and b"PNGDATA" in body


# --- noticias locales (FinBERT) y umbral adaptativo ---------------------------------------------
class FakeKV(dict):
    def set(self, k, v):
        self[k] = v

    def delete(self, k):
        self.pop(k, None)


def fake_pipe(titles, truncation=True, batch_size=16):
    fake_pipe.calls.append(list(titles))
    tone = lambda t: 0.9 if "soars" in t else -0.9 if "plunges" in t else 0.0  # noqa: E731
    return [[{"label": "positive", "score": max(0, tone(t))},
             {"label": "negative", "score": max(0, -tone(t))},
             {"label": "neutral", "score": 1 - abs(tone(t))}] for t in titles]


def test_local_news_component_and_breaking(monkeypatch, tmp_path):
    from newsbot.news.rss import Headline
    from newsbot.stocks import localnews
    monkeypatch.setattr(localnews, "_scores", {})
    monkeypatch.setattr(localnews, "CACHE_FILE", tmp_path / "fb.json")
    monkeypatch.setattr(localnews, "_loaded", False)
    fake_pipe.calls = []
    h = lambda t: Headline(t, "", "reuters", NOW)  # noqa: E731
    cfg = {"local_news": {"enabled": True, "breaking": 0.8, "min_weight": 0.5}, "sentiment": {}}
    kv = FakeKV()
    first = [(h("Nvidia soars"), 1.0), (h("Nvidia flat"), 1.0)]
    comp, brk = localnews.analyze(kv, cfg, "NVDA", first, fake_pipe)
    assert comp == pytest.approx(0.45) and brk == []  # la primera vez solo aprende lo que hay
    comp, brk = localnews.analyze(kv, cfg, "NVDA", [(h("Nvidia soars"), 1.0), (h("Nvidia plunges"), 1.0),
                                                     (h("Nvidia plunges again"), 0.2)], fake_pipe)
    assert brk == ["Nvidia plunges"]  # nueva y fuerte; la de peso 0.2 no cuenta como aviso
    assert fake_pipe.calls[-1] == ["Nvidia plunges", "Nvidia plunges again"]  # caché por título
    assert localnews.analyze(kv, {"local_news": {}}, "NVDA", [(h("x"), 1.0)]) == (0.0, [])
    saved = json.loads((tmp_path / "fb.json").read_text(encoding="utf-8"))  # sobrevive a un reinicio
    assert saved["Nvidia plunges"] == -0.9
    monkeypatch.setattr(localnews, "_scores", {})
    monkeypatch.setattr(localnews, "_loaded", False)
    assert localnews.score_titles(["Nvidia soars"], cfg, fake_pipe) == {"Nvidia soars": 0.9}
    assert fake_pipe.calls[-1] == ["Nvidia plunges", "Nvidia plunges again"]  # sin volver al modelo


def test_adaptive_threshold_follows_results(tmp_path):
    from newsbot.stocks import adaptive
    cfg = {"strategy": {"buy_threshold": 0.25, "buy_threshold_bear": 0.40},
           "adaptive": {"enabled": True, "min_trades": 4, "window": 6, "step": 0.03,
                        "min_offset": -0.06, "max_offset": 0.06}}
    with closing(db.connect(tmp_path / "a.db")) as con:
        kv = db.KV(con)
        msgs = [adaptive.record(con, cfg, "A/EUR", r, NOW) for r in (-0.02, -0.01, 0.01, -0.03)]
        assert msgs[:3] == [None] * 3 and "+0.25 → +0.28 (más exigente)" in msgs[3]
        eff = adaptive.apply(cfg, kv)
        assert eff["strategy"]["buy_threshold"] == pytest.approx(0.28) and eff["_adaptive"] == 0.03
        assert eff["strategy"]["buy_threshold_bear"] == pytest.approx(0.43)
        for _ in range(3):
            adaptive.record(con, cfg, "A/EUR", -0.02, NOW)
        assert float(kv.get("adaptive:offset")) == 0.06  # con tope
        for _ in range(8):  # cada operación ganadora baja un paso; con tope
            adaptive.record(con, cfg, "A/EUR", 0.02, NOW)
        assert float(kv.get("adaptive:offset")) == -0.06  # gana: más atrevido, también con tope
        assert len(adaptive.closed(kv)) == 15
        assert adaptive.apply(cfg | {"adaptive": {"enabled": False}}, kv) is not None
        assert adaptive.apply(cfg | {"adaptive": {}}, kv)["strategy"]["buy_threshold"] == 0.25


# --- dinero parado ------------------------------------------------------------------------------
class Sig:
    def __init__(self, signal):
        self.signal = signal


def _idle_db(tmp_path, backfill=True):
    con = db.connect(tmp_path / "idle.db")
    if backfill:  # 30 valores que caen un 5 % en un día y suben un 1 % en los 3 siguientes
        rows = []
        for k in range(30):
            t0 = 1_700_000_000 + k * 10 * 86400
            for ts, p in ((t0, 100.0), (t0 + 86400, 95.0), (t0 + 4 * 86400, 95.95)):
                rows.append((ts, f"S{k}/EUR", p))
        with con:
            con.executemany("INSERT INTO ticks(ts, created, symbol, price, signal, action, raw_json) "
                            "VALUES (?, '', ?, ?, 0, 'BACKFILL', '{}')", rows)
    return con


def idle_cfg():
    return {"costs": {"fee_pct": 0.0015, "slippage_pct": 0.0005}, "portfolio": {"min_cash_pct": 0.2}}


def test_idle_cash_waits_in_a_falling_market_with_history(tmp_path):
    with closing(_idle_db(tmp_path)) as con:
        mk = {"A/EUR": {"feats": {"intraday": -0.9}}, "B/EUR": {"feats": {"intraday": -0.6}}}
        ds = {"A/EUR": Sig(0.10), "B/EUR": Sig(-0.2)}
        out = insight.idle_cash(con, idle_cfg(), mk, ds, ["A/EUR", "B/EUR"], {}, 25.0, 30.0, 0.22, NOW)
        assert out[0].startswith("💶 Dinero parado: 19.00 € libres (63% del capital")
        verdict = out[-1]
        assert verdict.startswith("Qué haría: esperar. El mercado cae en bloque")
        assert "30 casos" in verdict and "+1.0 %" in verdict and "100%" in verdict
        assert "ahí sí suele haber rebote" in verdict  # +1 % > 2 × 0,4 % de costes
        assert json.loads(db.KV(con).get("idle:dips"))["stats"]["n"] == 30  # caché del día


def test_idle_cash_near_threshold_long_idle_and_little_cash(tmp_path):
    with closing(_idle_db(tmp_path, backfill=False)) as con:
        flat = {"A/EUR": {"feats": {"intraday": 0.0}}}
        near = insight.idle_cash(con, idle_cfg(), flat, {"A/EUR": Sig(0.19)}, ["A/EUR"], {}, 25.0, 30.0,
                                 0.22, NOW)
        assert "A está a 0.03 del umbral" in near[-1]
        far = insight.idle_cash(con, idle_cfg(), flat, {"A/EUR": Sig(0.0)}, ["A/EUR"], {}, 25.0, 30.0,
                                0.22, NOW)
        assert "sin estrenar este dinero" in far[-1] and "revisar el umbral (+0.22)" in far[-1]
        with con:
            con.execute("INSERT INTO orders(day, client_order_id, mode, side, created, status, symbol) "
                        "VALUES ('x', 'c1', 'live', 'buy', ?, 'filled', 'A/EUR')",
                        ((NOW - timedelta(hours=5)).isoformat(),))
        recent = insight.idle_cash(con, idle_cfg(), flat, {"A/EUR": Sig(0.0)}, ["A/EUR"], {}, 25.0, 30.0,
                                   0.22, NOW)
        assert "Última compra hace 5 h" in recent and recent[-1].startswith("Qué haría: esperar. El efectivo")
        assert insight.idle_cash(con, idle_cfg(), flat, {"A/EUR": Sig(0.0)}, ["A/EUR"], {}, 12.0, 30.0,
                                 0.22, NOW) == []  # 6 € libres = 20 %: no es dinero parado


def test_panic_stats_measures_the_day_after_for_the_worst_fallers(tmp_path):
    """Un día de pánico (todo −5 %; A, B y C −10 %) y el día siguiente los peores rebotan un 4 %."""
    con = db.connect(tmp_path / "p.db")
    syms = [f"S{k}/EUR" for k in range(10)]
    rows = []
    for d, day in enumerate(("2026-10-01", "2026-10-02", "2026-10-05", "2026-10-06")):
        for s in syms:
            worst = s in ("S0/EUR", "S1/EUR", "S2/EUR")
            p = [100.0, 90.0 if worst else 95.0, 93.6 if worst else 95.0, 94.0][d]
            rows.append((1_790_000_000 + d * 86400, f"{day}T21:00:00+00:00", s, p))
    with con:
        con.executemany("INSERT INTO ticks(ts, created, symbol, price, signal, action, raw_json) "
                        "VALUES (?, ?, ?, ?, 0, 'BACKFILL', '{}')", rows)
    st = insight.panic_stats(con, -0.02, 0.6, -0.025)
    assert st["n"] == 1 and st["cases"][0]["day"] == "2026-10-02"
    assert abs(st["worst_mean"] - 0.04) < 1e-9 and st["worst_up"] == 1.0 and st["all_mean"] == 0.0
    assert json.loads(db.KV(con).get("panic:stats"))["stats"]["n"] == 1  # caché del día
    con.close()
