"""Motor de bolsa (stocks/engine.py): propuestas, confirmación, sincronización y bot._extra_ticks.

Sin red: Yahoo, sentimiento, eventos, Telegram y broker simulados."""
import argparse
import json
import sqlite3
import threading
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from conftest import ROOT, candle

from newsbot import bot, portfolio
from newsbot import config as config_mod
from newsbot.execution.paper import Fill
from newsbot.features import market
from newsbot.news.rss import Headline
from newsbot.reporting import chart, telegram
from newsbot.sentiment import Sentiment
from newsbot.stocks import engine
from newsbot.stocks.paper import StocksPaperBroker
from newsbot.storage import db

MADRID = ZoneInfo("Europe/Madrid")
NOW = datetime(2026, 11, 3, 10, 30, tzinfo=MADRID)  # martes, mercado abierto
N = 260
SYMS = ["RHM/EUR", "LDO/EUR", "ASML/EUR", "IFX/EUR"]
NEWS = {"RHM": 0.8, "LDO": 0.3, "ASML": -0.5, "IFX": 0.0}  # señales: RHM > LDO > IFX > umbral > ASML


class FakeYahoo:
    """YahooExchange simulado: velas diarias cerradas hasta ayer (+1 %/día), 15 min planas,
    precio (`prices`) y festivos (`closed`)."""

    def __init__(self, last=None):
        self.last = {s: 100.0 for s in SYMS} | (last or {})
        self.prices = dict(self.last)
        self.closed: set[str] = set()
        self.options: dict = {}

    def fetch_ohlcv(self, symbol, timeframe="1d", since=None, limit=None):
        now_ms, last = int(NOW.timestamp() * 1000), self.last[symbol]
        if timeframe == "1d":
            out = [candle(now_ms - (N - i) * market.DAY_MS, last / 1.01 ** (N - 1 - i)) for i in range(N)]
        else:
            step = market.TIMEFRAME_MS[timeframe]
            out = [candle(now_ms - (101 - i) * step, last) for i in range(100)]
        return out[-limit:] if limit else out

    def fetch_ticker(self, symbol):
        return {"last": self.prices[symbol], "timestamp": int(NOW.timestamp() * 1000)}

    def trading_today(self, symbol, now_ms):
        return symbol not in self.closed


class FakeEvents:
    def __init__(self, scores=None):
        self.scores, self.refreshed = scores or {}, []

    def due(self, con, cfg, now):
        return True

    def refresh(self, con, cfg, now, headlines):
        self.refreshed.append(len(headlines))
        return 0

    def score(self, con, cfg, code, now):
        return self.scores.get(code, 0.5)


class FakeBroker:
    """Broker con la interfaz de T212Broker que apunta cada llamada."""

    def __init__(self, cash=50.0, held=None):
        self.cash, self.held, self.calls = cash, held or {}, []

    def snapshot(self):
        return self.cash, {s: dict(v) for s, v in self.held.items()}

    def buy(self, symbol, eur, price):
        self.calls.append(("buy", symbol, eur, price))
        q = eur / price
        h = self.held.setdefault(symbol, {"qty": 0.0, "avg": price, "price": price})
        h["qty"] += q
        self.cash -= eur
        return Fill("buy", q, price, 0.0, eur)

    def sell(self, symbol, qty, price):
        self.calls.append(("sell", symbol, qty, price))
        q = min(qty, self.held[symbol]["qty"])
        self.held[symbol]["qty"] -= q
        self.cash += q * price
        return Fill("sell", q, price, 0.0, q * price)

    def place_stop(self, symbol, qty, stop_price):
        self.calls.append(("stop", symbol, qty, stop_price))
        return f"stop-{len(self.calls)}"

    def cancel_stop(self, order_id):
        self.calls.append(("cancel", order_id))


@pytest.fixture(autouse=True)
def _no_freeze_guard(monkeypatch):
    """FakeYahoo da el mismo precio en cada ciclo: la guarda de precios congelados se prueba aparte."""
    monkeypatch.setattr(engine, "FROZEN_MINUTES", 10**7)


@pytest.fixture
def scfg(tmp_path, monkeypatch):
    for k in ("ANTHROPIC_API_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "CONFIRM_LIVE",
              "TRADING212_API_KEY", "TRADING212_API_SECRET"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(portfolio, "REGISTRY", {})
    c = config_mod.load(ROOT / "config_bolsa.toml")
    c["general"] |= {"mode": "paper", "symbols": list(SYMS), "data_dir": str(tmp_path / "bolsa"),
                      "wind_down": False}  # el config real está en cierre desde el 6-nov
    c["events"]["enabled"] = True  # el config real (en cierre) ya no usa Claude: las pruebas sí
    c["sentiment"]["llm_max_calls_day"] = 60
    c["execution"]["broker"] = "paper"
    c["news"]["feeds"] = {}
    c["portfolio"].pop("budget_eur", None)  # toda la cuenta; el presupuesto tiene su propia prueba
    c["general"]["initial_capital"] = 50.0  # efectivo de papel de las pruebas (el config real: 20 €)
    c.pop("local_news", None)  # sin FinBERT real en las pruebas (test_localnews usa uno falso)
    return c


@pytest.fixture
def env(monkeypatch):
    """Sentimiento, titulares, eventos y Telegram simulados; devuelve lo enviado y los eventos."""
    sent, notices, ids = [], [], iter(range(100, 1000))
    ev = FakeEvents()
    monkeypatch.setattr(engine.sentiment, "analyze", lambda hs, cfg, ws=None, cache=None, asset="":
                        Sentiment(NEWS.get(asset, 0.0), 1.0, "fake", "fake"))
    h = Headline("Rheinmetall wins big Bundeswehr order", "", "reuters", NOW.astimezone(UTC))
    monkeypatch.setattr(engine.ingest, "collect", lambda cfg, now: [h])
    monkeypatch.setattr(engine, "_events", lambda: ev)

    def send_get_id(text, env=None, post=None, html=True, silent=False, markup=None):
        sent.append((text, markup))
        return next(ids)

    monkeypatch.setattr(telegram, "send_get_id", send_get_id, raising=False)
    monkeypatch.setattr(telegram, "send", lambda text, *a, **k: notices.append(text) or True)
    return SimpleNamespace(sent=sent, notices=notices, events=ev)


def q(cfg, sql, args=()):
    with closing(sqlite3.connect(Path(cfg["general"]["data_dir"]) / "newsbot.db")) as con:
        con.row_factory = sqlite3.Row
        return con.execute(sql, args).fetchall()


def run(cfg, ex=None, now=NOW, **kw):
    return engine.run_tick(cfg, now, exchange=ex or FakeYahoo(), **kw)


def setup_db(cfg, positions=(), kv=None):
    """positions: (símbolo, qty, avg, stop, stop_order_id)."""
    Path(cfg["general"]["data_dir"]).mkdir(parents=True, exist_ok=True)
    with closing(db.connect(Path(cfg["general"]["data_dir"]) / "newsbot.db")) as con:
        with con:
            con.executemany("INSERT INTO positions(symbol, qty, avg_entry, high, opened, stop_price, "
                            "stop_order_id) VALUES (?,?,?,?,?,?,?)",
                            [(s, qty, avg, avg, NOW.isoformat(), stop, sid)
                             for s, qty, avg, stop, sid in positions])
        for k, v in (kv or {}).items():
            db.KV(con).set(k, v)


# --- run_tick -----------------------------------------------------------------------------------
def test_tick_creates_one_buy_proposal_and_saves_everything(scfg, env):
    ids = run(scfg)
    assert len(ids) == 1
    p = q(scfg, "SELECT * FROM proposals")[0]
    assert (p["id"], p["symbol"], p["side"], p["status"], p["urgent"], p["msg_id"]) == (
        ids[0], "RHM/EUR", "buy", "proposed", 0, 100)
    assert 1 < p["eur"] <= scfg["execution"]["max_order_eur"] and p["qty"] == pytest.approx(p["eur"] / 100)
    assert 0.88 <= p["stop_price"] / p["ref_price"] <= 0.97  # stop por ATR (~4 % × 2,5)
    assert datetime.fromisoformat(p["expires"]) == NOW + timedelta(minutes=10)
    text, markup = env.sent[0]
    assert "🟢 <b>Propuesta de compra</b> · RHM (Rheinmetall)" in text and "¿La compro?" in text
    assert "acciones a 100.00 €" in text and "Stop-loss" in text and "caduca 10:40" in text
    assert "(noticias +0.32, tendencia +0.30, eventos +0.0" in text
    assert markup["inline_keyboard"][0] == [{"text": "✅ Comprar", "callback_data": f"o:bolsa:{ids[0]}:y"},
                                            {"text": "❌ No", "callback_data": f"o:bolsa:{ids[0]}:n"}]
    graph, tv = markup["inline_keyboard"][1]
    assert graph == {"text": "📈 Gráfico", "callback_data": "g:bolsa:RHM"}
    assert tv["url"].startswith("https://www.tradingview.com/chart/?symbol=")
    assert "💭 <i>Compro por" in text and "Si toca el stop pierdo" in text and "Comisiones" in text
    assert text.index("💭") < text.index("¿La compro?")
    ticks = {t["symbol"]: t for t in q(scfg, "SELECT * FROM ticks")}
    assert set(ticks) == set(SYMS) and ticks["RHM/EUR"]["action"] == "BUY"
    assert ticks["LDO/EUR"]["action"] == "HOLD" and ticks["LDO/EUR"]["reason"].startswith("candidata")
    raw = json.loads(ticks["RHM/EUR"]["raw_json"])
    assert set(raw) == {"news", "trend", "intraday", "fng", "votes", "rsi", "bollinger", "momentum",
                        "volume", "events", "breakout", "macd", "high52", "kronos", "dip", "finbert",
                        "_regime",
                        "_atr"}
    assert raw["news"] == 0.8 and raw["events"] == 0.5 and raw["_regime"] == "bull" and raw["_atr"] > 0
    assert raw["kronos"] == 0.0  # sin [kronos] no se carga el modelo
    assert set(json.loads(ticks["RHM/EUR"]["contributions_json"])) >= {"news", "trend", "events"}
    eq = q(scfg, "SELECT * FROM equity_daily")[0]
    assert (eq["cash"], eq["qty"], eq["symbol"], eq["equity"], eq["peak"]) == (50.0, 0.0, None, 50.0, 50.0)
    dec = q(scfg, "SELECT * FROM decisions")[0]
    assert (dec["action"], dec["symbol"]) == ("BUY", "RHM/EUR") and f"#{ids[0]}" in dec["reason"]
    assert q(scfg, "SELECT count(*) FROM headlines")[0][0] == 1 and env.events.refreshed == [1]
    kv = {r["key"]: r["value"] for r in q(scfg, "SELECT * FROM kv")}
    assert json.loads(kv["news_ema"])["RHM/EUR"][1] == 0.8 and float(kv["peak"]) == 50.0


def test_pending_proposal_is_not_repeated(scfg, env):
    first = run(scfg)
    second = run(scfg, now=NOW + timedelta(minutes=5))
    rows = q(scfg, "SELECT symbol FROM proposals ORDER BY id")
    assert [r["symbol"] for r in rows] == ["RHM/EUR", "LDO/EUR"] and first != second  # la siguiente mejor
    rhm = q(scfg, "SELECT reason FROM ticks WHERE symbol = 'RHM/EUR' ORDER BY id DESC LIMIT 1")[0]
    assert rhm["reason"] == "compra ya propuesta, esperando tu respuesta"


def test_rejection_cooldown(scfg, env):
    [pid] = run(scfg)
    assert "Descartada" in engine.confirm(scfg, pid, False, NOW + timedelta(minutes=1))
    assert [r["symbol"] for r in q(scfg, "SELECT symbol FROM proposals WHERE id > ?", (pid,))] == []
    run(scfg, now=NOW + timedelta(minutes=15))
    assert [r["symbol"] for r in q(scfg, "SELECT symbol FROM proposals WHERE id > ?", (pid,))] == ["LDO/EUR"]
    later = run(scfg, now=NOW + timedelta(hours=4, minutes=5))  # pasadas 4 h vuelve a proponerla
    assert symbols(scfg, later) == ["RHM/EUR"]


def test_max_proposals_per_day(scfg, env):
    scfg["execution"]["max_proposals_day"] = 1
    assert len(run(scfg)) == 1
    assert run(scfg, now=NOW + timedelta(minutes=15)) == []
    reason = q(scfg, "SELECT reason FROM ticks WHERE symbol = 'LDO/EUR' ORDER BY id DESC LIMIT 1")[0][0]
    assert reason == "máximo de propuestas hoy (1)"


def test_max_positions(scfg, env):
    setup_db(scfg, [("LDO/EUR", 0.1, 100.0, 90.0, None), ("ASML/EUR", 0.1, 100.0, 90.0, None),
                    ("IFX/EUR", 0.1, 100.0, 90.0, None)])
    assert run(scfg) == []
    reason = q(scfg, "SELECT reason FROM ticks WHERE symbol = 'RHM/EUR'")[0][0]
    assert reason == "sin tamaño: máximo de posiciones (3)"
    assert q(scfg, "SELECT equity FROM equity_daily")[0][0] == pytest.approx(80.0)


def test_kill_switch_blocks_buys(scfg, env):
    setup_db(scfg, kv={"kill_switch": "1"})
    assert run(scfg) == []
    assert q(scfg, "SELECT reason FROM ticks WHERE symbol = 'RHM/EUR'")[0][0].startswith("kill-switch")


def test_drawdown_arms_kill_switch(scfg, env):
    setup_db(scfg, kv={"peak": "100.0"})  # 50 € frente a un máximo de 100 €: drawdown del 50 %
    assert run(scfg) == []
    assert q(scfg, "SELECT value FROM kv WHERE key = 'kill_switch'")[0][0] == "1"
    assert len(env.notices) == 1 and "Kill-switch" in env.notices[0]


def test_pause_and_closed_session_do_nothing(scfg, env):
    setup_db(scfg, kv={"paused": "1"})
    assert run(scfg) == [] and q(scfg, "SELECT count(*) FROM ticks")[0][0] == 0
    with closing(db.connect(Path(scfg["general"]["data_dir"]) / "newsbot.db")) as con:
        db.KV(con).delete("paused")
    assert run(scfg, now=NOW.replace(hour=18)) == []  # 18:00: mercado cerrado
    assert run(scfg, now=datetime(2026, 11, 7, 11, tzinfo=MADRID)) == []  # sábado
    assert q(scfg, "SELECT count(*) FROM ticks")[0][0] == 0 and env.sent == []


def test_holiday_symbol_is_skipped(scfg, env):
    ex = FakeYahoo()
    ex.closed = {"RHM/EUR"}
    run(scfg, ex)
    assert [r["symbol"] for r in q(scfg, "SELECT symbol FROM proposals")] == ["LDO/EUR"]
    assert q(scfg, "SELECT reason FROM ticks WHERE symbol = 'RHM/EUR'")[0][0] == \
        "mercado cerrado hoy para este valor"


def test_proposal_expires_at_close(scfg, env):
    [pid] = run(scfg, now=NOW.replace(hour=17, minute=25))
    assert q(scfg, "SELECT expires FROM proposals")[0][0] == NOW.replace(hour=17, minute=30).isoformat()
    run(scfg, now=NOW.replace(hour=17, minute=29))  # el siguiente tick no la ha caducado aún
    assert q(scfg, "SELECT status FROM proposals WHERE id = ?", (pid,))[0][0] == "proposed"


def test_stop_loss_sell_is_urgent_and_ignores_cooldown(scfg, env):
    setup_db(scfg, [("RHM/EUR", 0.1, 100.0, 95.0, None)],
             kv={"reject:RHM/EUR": (NOW - timedelta(hours=1)).isoformat()})
    ex = FakeYahoo()
    ex.prices["RHM/EUR"] = 90.0
    ids = run(scfg, ex)
    sell, buy = (q(scfg, "SELECT * FROM proposals WHERE id = ?", (i,))[0] for i in ids)
    assert (sell["symbol"], sell["side"], sell["urgent"], sell["qty"]) == ("RHM/EUR", "sell", 1, 0.1)
    assert sell["eur"] == pytest.approx(9.0) and sell["stop_price"] == 95.0
    assert (buy["symbol"], buy["side"]) == ("LDO/EUR", "buy")  # y como mucho una compra
    text, markup = env.sent[0]
    assert text.startswith("⚠️ <b>STOP-LOSS</b> · 🔴 <b>Propuesta de venta</b> · RHM (Rheinmetall)")
    assert "Motivo: stop-loss" in text and "¿La vendo?" in text
    assert markup["inline_keyboard"][0][0] == {"text": "✅ Vender", "callback_data": f"o:bolsa:{ids[0]}:y"}
    run(scfg, ex, now=NOW + timedelta(minutes=5))  # la venta ya está propuesta: no la repite
    assert q(scfg, "SELECT count(*) FROM proposals WHERE side = 'sell'")[0][0] == 1
    assert "venta ya propuesta" in q(scfg, "SELECT reason FROM ticks WHERE symbol = 'RHM/EUR' "
                                           "ORDER BY id DESC LIMIT 1")[0][0]


def test_signal_sell_respects_rejection_cooldown(scfg, env, monkeypatch):
    monkeypatch.setitem(NEWS, "RHM", -1.0)
    env.events.scores["RHM"] = -1.0  # -0,40 + 0,30 - 0,15 = -0,25 <= -0,15
    setup_db(scfg, [("RHM/EUR", 0.1, 100.0, 90.0, None)],
             kv={"reject:RHM/EUR": (NOW - timedelta(hours=1)).isoformat()})
    assert "RHM/EUR" not in symbols(scfg, run(scfg))
    assert "rechazaste venderla" in q(scfg, "SELECT reason FROM ticks WHERE symbol = 'RHM/EUR'")[0][0]
    ids = run(scfg, now=NOW + timedelta(hours=3, minutes=1))
    sells = q(scfg, "SELECT * FROM proposals WHERE side = 'sell'")
    assert len(sells) == 1 and sells[0]["id"] in ids and sells[0]["urgent"] == 0
    assert "señal de venta" in sells[0]["reason"]


def test_positions_sync_with_broker(scfg, env):
    setup_db(scfg, [("LDO/EUR", 0.1, 100.0, 90.0, "s1")])
    with closing(db.connect(Path(scfg["general"]["data_dir"]) / "newsbot.db")) as con, con:
        con.execute("INSERT INTO proposals(created, expires, symbol, side, eur, qty, ref_price) "
                    "VALUES (?,?,?,?,?,?,?)", (NOW.isoformat(), (NOW + timedelta(minutes=5)).isoformat(),
                                               "LDO/EUR", "sell", 9.0, 0.1, 90.0))
    broker = FakeBroker(cash=60.0, held={"IFX/EUR": {"qty": 0.2, "avg": 95.0, "price": 100.0}})
    run(scfg, broker=broker)  # 1.ª lectura sin LDO: puede ser un fallo puntual de la API → se conserva
    assert {r["symbol"] for r in q(scfg, "SELECT symbol FROM positions")} == {"IFX/EUR", "LDO/EUR"}
    assert not any("🛑" in n for n in env.notices)
    run(scfg, broker=broker, now=NOW + timedelta(minutes=1))  # 2.ª lectura seguida sin LDO: cerrada
    pos = {r["symbol"]: r for r in q(scfg, "SELECT * FROM positions")}
    assert set(pos) == {"IFX/EUR"} and pos["IFX/EUR"]["qty"] == 0.2 and pos["IFX/EUR"]["avg_entry"] == 95.0
    assert pos["IFX/EUR"]["stop_price"] < 95.0 and pos["IFX/EUR"]["high"] == 100.0
    assert any("🛑 Stop ejecutado en Trading 212" in n and "LDO" in n for n in env.notices)
    assert q(scfg, "SELECT status FROM proposals WHERE symbol = 'LDO/EUR'")[0][0] == "failed"
    assert q(scfg, "SELECT equity FROM equity_daily")[0][0] == pytest.approx(80.0)
    assert ("buy", "RHM/EUR") not in [c[:2] for c in broker.calls]  # proponer no opera


def test_telegram_failure_does_not_break_the_tick(scfg, env, monkeypatch):
    def boom(*a, **k):
        raise OSError("sin red")

    monkeypatch.setattr(telegram, "send_get_id", boom, raising=False)
    [pid] = run(scfg)
    assert tuple(q(scfg, "SELECT msg_id, status FROM proposals")[0]) == (None, "proposed")


def test_paper_broker_round_trip(scfg):
    with closing(_con(scfg)) as con:
        b = StocksPaperBroker(scfg, con)
        assert b.snapshot() == (50.0, {}) and b.sell("RHM/EUR", 1.0, 100.0) is None
        f = b.buy("RHM/EUR", 20.0, 100.0)
        assert (f.side, f.eur, f.price) == ("buy", 20.0, pytest.approx(100.1))
        assert b.cash == pytest.approx(30.0)
        assert b.buy("RHM/EUR", 1000.0, 100.0).eur == pytest.approx(30.0) and b.cash == pytest.approx(0.0)
        with con:  # las posiciones las escribe el motor tras el fill
            con.execute("INSERT INTO positions(symbol, qty, avg_entry) VALUES (?, ?, ?)",
                        ("RHM/EUR", f.qty, 100.1))
        assert b.snapshot()[1] == {"RHM/EUR": {"qty": f.qty, "avg": 100.1, "price": None}}
        s = b.sell("RHM/EUR", 99.0, 110.0)  # nunca más de lo que hay
        assert s.qty == f.qty and s.price == pytest.approx(109.89) and b.cash == pytest.approx(f.qty * 109.89)
        assert b.place_stop("RHM/EUR", 0.1, 90.0).startswith("paper-stop-")
        assert b.place_stop("RHM/EUR", 0, 90.0) is None
        b.cancel_stop("paper-stop-1")


def test_falls_back_to_send_without_send_get_id(scfg, env, monkeypatch):
    monkeypatch.delattr(telegram, "send_get_id", raising=False)
    got = []
    monkeypatch.setattr(telegram, "send",
                        lambda text, html=False, markup=None, **k: got.append((html, markup)))
    [pid] = run(scfg)
    assert got == [(True, engine.proposal_markup({"id": pid, "side": "buy", "symbol": "RHM/EUR"}, scfg))]


def test_dry_run_writes_nothing(scfg, env, capsys):
    assert run(scfg, dry_run=True) == []
    out = capsys.readouterr().out
    assert out.startswith("[DRY-RUN] 📈 Bolsa") and "Propondría: compra RHM" in out and "LDO" in out
    for table in ("proposals", "ticks", "positions", "equity_daily", "headlines", "decisions", "orders"):
        assert q(scfg, f"SELECT count(*) FROM {table}")[0][0] == 0, table
    keys = {r[0] for r in q(scfg, "SELECT key FROM kv")}
    assert not keys & {"paper:cash", "news_ema", "peak", "kill_switch"}
    assert env.sent == env.notices == env.events.refreshed == []
    # también con el mercado cerrado (avisa de que no propondría nada)
    assert run(scfg, now=NOW.replace(hour=20), dry_run=True) == []
    assert "mercado cerrado: no propondría nada" in capsys.readouterr().out


def test_confirm_false_paper_executes_directly(scfg, env):
    scfg["execution"]["confirm"] = False
    [pid] = run(scfg)
    p = q(scfg, "SELECT * FROM proposals")[0]
    assert p["status"] == "executed" and p["result"].startswith("✅ Comprado")
    assert q(scfg, "SELECT symbol FROM positions")[0][0] == "RHM/EUR"
    assert len(env.sent) == 1 and env.sent[0][0].startswith("✅ Comprado") and env.sent[0][1] is None


# --- confirm ------------------------------------------------------------------------------------
def test_confirm_buy_paper_updates_everything(scfg, env):
    ex = FakeYahoo()
    [pid] = run(scfg, ex)
    eur = q(scfg, "SELECT eur FROM proposals")[0][0]
    ex.prices["RHM/EUR"] = 100.5  # deriva +0,5 %: dentro del 1,5 %
    text = engine.confirm(scfg, pid, True, NOW + timedelta(minutes=2), exchange=ex)
    assert text.startswith("✅ Comprado ") and " RHM a 100.60 € (" in text and "· stop en " in text
    pos = q(scfg, "SELECT * FROM positions")[0]
    assert pos["symbol"] == "RHM/EUR" and pos["qty"] == pytest.approx(eur / 100.6005)
    assert pos["avg_entry"] == pytest.approx(100.6005) and pos["stop_order_id"].startswith("paper-stop-")
    assert 0.88 < pos["stop_price"] / pos["avg_entry"] < 0.97 and pos["opened"]
    o = q(scfg, "SELECT * FROM orders")[0]
    assert (o["client_order_id"], o["status"], o["side"], o["symbol"]) == (
        f"paper:{pid}", "filled", "buy", "RHM/EUR")
    assert o["eur"] == pytest.approx(eur)
    cash = float(q(scfg, "SELECT value FROM kv WHERE key = 'paper:cash'")[0][0])
    assert cash == pytest.approx(50 - eur)
    p = q(scfg, "SELECT status, result, decided FROM proposals")[0]
    assert p["status"] == "executed" and p["result"] == text and p["decided"]
    assert q(scfg, "SELECT action FROM decisions")[0][0] == "BUY"
    again = engine.confirm(scfg, pid, True, NOW + timedelta(minutes=3), exchange=ex)
    assert again == "Ya respondida: la propuesta #1 (RHM (Rheinmetall)) está ejecutada."
    # el siguiente tick ve la posición (paper: la tabla) y no la vuelve a proponer
    run(scfg, ex, now=NOW + timedelta(minutes=15))
    assert q(scfg, "SELECT held FROM ticks WHERE symbol = 'RHM/EUR' ORDER BY id DESC LIMIT 1")[0][0] == 1
    with closing(_con(scfg)) as con:
        lines = engine.positions_lines(con, scfg, NOW + timedelta(minutes=16))
    assert lines[0] == "<b>Posiciones</b> 1/3" and lines[1].startswith("• RHM ")
    assert any(x.startswith("Efectivo ") for x in lines)
    assert any(x.startswith("⏳ Propuesta #") and "compra LDO" in x for x in lines)


def _con(cfg):
    Path(cfg["general"]["data_dir"]).mkdir(parents=True, exist_ok=True)
    return db.connect(Path(cfg["general"]["data_dir"]) / "newsbot.db")


def symbols(cfg, ids):
    marks = ",".join("?" * len(ids))
    rows = q(cfg, f"SELECT symbol FROM proposals WHERE id IN ({marks}) ORDER BY id", ids)
    return [r["symbol"] for r in rows]


def test_confirm_reject_expired_and_unknown(scfg, env):
    a, = run(scfg)
    assert engine.confirm(scfg, a, False, NOW + timedelta(minutes=1)) == \
        "❌ Descartada: no compro RHM (Rheinmetall). No lo vuelvo a proponer en 4 h."
    p = q(scfg, "SELECT status FROM proposals WHERE id = ?", (a,))[0]
    assert p["status"] == "rejected"
    assert q(scfg, "SELECT value FROM kv WHERE key = 'reject:RHM/EUR'")[0][0] == \
        (NOW + timedelta(minutes=1)).isoformat()
    b, = run(scfg, now=NOW + timedelta(minutes=15))
    text = engine.confirm(scfg, b, True, NOW + timedelta(minutes=26))
    assert text.startswith("⌛ La propuesta") and "caducó a las 10:55" in text
    assert q(scfg, "SELECT status FROM proposals WHERE id = ?", (b,))[0][0] == "expired"
    assert engine.confirm(scfg, 999, True, NOW) == "No existe la propuesta #999."
    assert q(scfg, "SELECT count(*) FROM orders")[0][0] == 0


def test_confirm_price_drift_fails(scfg, env):
    ex = FakeYahoo()
    [pid] = run(scfg, ex)
    ex.prices["RHM/EUR"] = 102.0  # +2 % > 1,5 %
    text = engine.confirm(scfg, pid, True, NOW + timedelta(minutes=1), exchange=ex)
    assert "se ha movido +2.0 %" in text and "100.00 → 102.00 €" in text
    p = q(scfg, "SELECT status, result FROM proposals")[0]
    assert p["status"] == "failed" and "deriva" in p["result"]
    assert q(scfg, "SELECT count(*) FROM orders")[0][0] == 0
    assert q(scfg, "SELECT count(*) FROM positions")[0][0] == 0


def test_confirm_insufficient_cash(scfg, env):
    ex = FakeYahoo()
    [pid] = run(scfg, ex)
    setup_db(scfg, kv={"paper:cash": "1.0"})
    text = engine.confirm(scfg, pid, True, NOW + timedelta(minutes=1), exchange=ex)
    assert text.startswith("⚠️ Efectivo insuficiente") and "hay 1.00 €" in text
    assert q(scfg, "SELECT status FROM proposals")[0][0] == "failed"
    assert q(scfg, "SELECT count(*) FROM orders")[0][0] == 0


def test_confirm_market_closed(scfg, env):
    [pid] = run(scfg, now=NOW.replace(hour=17, minute=25))
    assert engine.confirm(scfg, pid, True, NOW.replace(hour=17, minute=30)).startswith("⌛")  # al cierre
    with closing(_con(scfg)) as con, con:
        pid = con.execute("INSERT INTO proposals(created, expires, symbol, side, eur, qty, ref_price) "
                          "VALUES (?,?,?,?,?,?,?)", (NOW.isoformat(), (NOW + timedelta(days=1)).isoformat(),
                                                     "LDO/EUR", "buy", 5.0, 0.05, 100.0)).lastrowid
    text = engine.confirm(scfg, pid, True, NOW.replace(hour=18), exchange=FakeYahoo())
    assert text == "🔒 Mercado cerrado: no ejecuto la compra de LDO (Leonardo S.p.A.) (propuesta #2)."
    assert q(scfg, "SELECT status FROM proposals WHERE id = ?", (pid,))[0][0] == "failed"
    assert q(scfg, "SELECT count(*) FROM orders")[0][0] == 0


def test_broker_stop_on_buy_and_cancel_on_sell(scfg, env):
    ex, broker = FakeYahoo(), FakeBroker()
    [pid] = run(scfg, ex, broker=broker)
    text = engine.confirm(scfg, pid, True, NOW + timedelta(minutes=1), exchange=ex, broker=broker)
    (_, sym, eur, price), (_, sym2, qty, stop) = broker.calls
    assert (sym, sym2, price) == ("RHM/EUR", "RHM/EUR", 100.0) and qty == pytest.approx(eur / 100)
    pos = q(scfg, "SELECT * FROM positions")[0]
    assert pos["stop_order_id"] == "stop-2" and pos["stop_price"] == pytest.approx(stop)
    assert f"stop en {stop:.2f} €" in text
    # cae bajo el stop: venta urgente; al aceptarla, primero se cancela el stop del broker
    ex.prices["RHM/EUR"] = stop * 0.99
    ids = run(scfg, ex, now=NOW + timedelta(minutes=15), broker=broker)
    sell = q(scfg, "SELECT * FROM proposals WHERE side = 'sell'")[0]
    assert sell["id"] in ids and sell["urgent"] == 1
    text = engine.confirm(scfg, sell["id"], True, NOW + timedelta(minutes=16), exchange=ex, broker=broker)
    assert broker.calls[2:] == [("cancel", "stop-2"), ("sell", "RHM/EUR", pytest.approx(qty), stop * 0.99)]
    assert text.startswith("✅ Vendido") and "resultado −" in text and "(−" in text
    assert "🧐 <b>Por qué perdí</b>" in text and "• Lección: " in text and "Salida: stop" in text
    lessons = json.loads(q(scfg, "SELECT value FROM kv WHERE key = 'lessons'")[0][0])
    assert [x["symbol"] for x in lessons] == ["RHM/EUR"] and lessons[0]["ret"] < 0
    assert q(scfg, "SELECT count(*) FROM positions")[0][0] == 0
    orders = q(scfg, "SELECT side, status, client_order_id FROM orders ORDER BY id")
    assert [tuple(o) for o in orders] == [("buy", "filled", f"paper:{pid}"),
                                          ("sell", "filled", f"paper:{sell['id']}")]


def test_failed_sell_restores_broker_stop(scfg, env):
    class NoFill(FakeBroker):
        def sell(self, symbol, qty, price):
            self.calls.append(("sell", symbol, qty, price))
            return None

    setup_db(scfg, [("RHM/EUR", 0.1, 100.0, 95.0, "old")])
    broker = NoFill(held={"RHM/EUR": {"qty": 0.1, "avg": 100.0, "price": 90.0}})
    ex = FakeYahoo()
    ex.prices["RHM/EUR"] = 90.0
    sell_id = run(scfg, ex, broker=broker)[0]
    text = engine.confirm(scfg, sell_id, True, NOW + timedelta(minutes=1), exchange=ex, broker=broker)
    assert text.startswith("⚠️ La orden de venta de RHM (Rheinmetall) no se llenó")
    assert broker.calls == [("cancel", "old"), ("sell", "RHM/EUR", 0.1, 90.0), ("stop", "RHM/EUR", 0.1, 95.0)]
    assert q(scfg, "SELECT stop_order_id FROM positions")[0][0] == "stop-3"
    assert q(scfg, "SELECT status FROM orders")[0][0] == "nofill"


def test_live_order_ids_and_make_broker(scfg, env, monkeypatch):
    con = _con(scfg)
    assert isinstance(engine.make_broker(scfg, con), StocksPaperBroker)
    live = scfg | {"general": scfg["general"] | {"mode": "live"},
                   "execution": scfg["execution"] | {"broker": "trading212"}}
    with pytest.raises(SystemExit, match="CONFIRM_LIVE=yes, TRADING212_API_KEY"):
        engine.make_broker(live, con, env={})
    made = []
    fake = SimpleNamespace(
        credentials=lambda env: (env["TRADING212_API_KEY"], env["TRADING212_API_SECRET"]),
        T212Client=lambda key, secret, env="live": made.append((key, secret, env)) or "client",
        T212Broker=lambda cfg, client, fx=None: ("t212", client))
    monkeypatch.setattr(engine, "_t212", lambda: fake)
    keys = {"CONFIRM_LIVE": "yes", "TRADING212_API_KEY": "k", "TRADING212_API_SECRET": "s"}
    assert engine.make_broker(live, con, env=keys) == ("t212", "client")
    assert engine.make_broker(live, con, env=keys) == ("t212", "client")
    assert made == [("k", "s", "live")]  # un solo cliente por cartera (límites de la API compartidos)
    con.close()
    # una propuesta en live se apunta como t212:<id>
    [pid] = run(live, broker=FakeBroker())
    engine.confirm(live, pid, True, NOW + timedelta(minutes=1), exchange=FakeYahoo(), broker=FakeBroker())
    assert tuple(q(scfg, "SELECT client_order_id, mode FROM orders")[0]) == (f"t212:{pid}", "live")


def test_live_blocked_warns_once_and_never_exits_the_telegram_thread(scfg, env):
    live = scfg | {"general": scfg["general"] | {"mode": "live"},
                   "execution": scfg["execution"] | {"broker": "trading212"}}
    for minutes in (0, 15):
        with pytest.raises(SystemExit, match="bloqueado"):
            run(live, now=NOW + timedelta(minutes=minutes))
    assert len(env.notices) == 1 and "CONFIRM_LIVE=yes" in env.notices[0]
    assert q(scfg, "SELECT count(*) FROM ticks")[0][0] == 0
    with closing(_con(scfg)) as con, con:
        expires = (NOW + timedelta(minutes=10)).isoformat()
        pid = con.execute("INSERT INTO proposals(created, expires, symbol, side, eur, qty, ref_price) "
                          "VALUES (?,?,?,?,?,?,?)",
                          (NOW.isoformat(), expires, "RHM/EUR", "buy", 5.0, 0.05, 100.0)).lastrowid
    text = engine.confirm(live, pid, True, NOW + timedelta(minutes=1), exchange=FakeYahoo())  # no lanza
    assert text.startswith("⚠️ Modo live de bolsa (Trading 212) bloqueado") and "No ejecuto la compra" in text
    assert q(scfg, "SELECT status FROM proposals")[0][0] == "failed"
    assert q(scfg, "SELECT count(*) FROM orders")[0][0] == 0


def test_confirm_and_tick_share_the_portfolio_lock(scfg, env):
    [pid] = run(scfg)
    assert engine.lock(scfg) is engine.lock(dict(scfg, general=dict(scfg["general"])))
    done = threading.Event()
    with engine.lock(scfg):
        t = threading.Thread(target=lambda: (engine.confirm(scfg, pid, False, NOW), done.set()))
        t.start()
        assert not done.wait(0.3)  # espera a que el tick (aquí, el candado) termine
    t.join(5)
    assert done.is_set()


# --- bot ----------------------------------------------------------------------------------------
def test_extra_ticks_dispatch_to_the_stocks_engine(scfg, env, monkeypatch):
    calls = []
    monkeypatch.setattr(bot.stocks, "run_tick", lambda c, now=None, **k: calls.append(("tick", k)))
    monkeypatch.setattr(bot, "run_once", lambda *a, **k: calls.append("run_once"))
    monkeypatch.setattr(bot, "reconcile", lambda *a, **k: calls.append("reconcile"))
    monkeypatch.setattr(bot, "learn_step", lambda c: calls.append("learn"))
    monkeypatch.setattr(bot.session, "is_open", lambda c, now: True)
    bot._extra_ticks(scfg, threading.Event(), iterations=1)
    assert calls == [("tick", {}), "learn"]
    calls.clear()
    monkeypatch.setattr(bot.session, "is_open", lambda c, now: False)
    bot._extra_ticks(scfg, threading.Event(), iterations=1)
    assert calls == [] and env.events.refreshed == [1]  # cerrado: titulares + eventos si toca
    assert q(scfg, "SELECT count(*) FROM headlines")[0][0] == 1


def test_main_dry_run_uses_the_stocks_engine(scfg, monkeypatch):
    calls = []
    monkeypatch.setattr(bot.stocks, "run_tick", lambda c, **k: calls.append(("tick", k)))
    monkeypatch.setattr(bot, "run_once", lambda c, **k: calls.append(("run", k)))
    off = dict.fromkeys(("compare_sentiment", "report", "healthcheck", "telegram_poll", "reconcile",
                         "resume", "watch", "loop", "force"), False)
    args = argparse.Namespace(**off, dry_run=True)
    bot._dispatch(args, scfg, [])
    assert calls == [("tick", {"dry_run": True})]


# --- correcciones de la revisión: el dinero real nunca se mueve por un fallo de estado -------------------
def test_buy_is_refused_if_paused_or_kill_switch_after_proposing(scfg, env):
    ex = FakeYahoo()
    [pid] = run(scfg, ex)
    setup_db(scfg, kv={"paused": "1"})
    text = engine.confirm(scfg, pid, True, NOW + timedelta(minutes=1), exchange=ex)
    assert text.startswith("⛔ No compro") and "pausa" in text
    assert q(scfg, "SELECT status FROM proposals")[0][0] == "failed"
    assert q(scfg, "SELECT count(*) FROM orders")[0][0] == 0


def test_stop_loss_sell_is_not_blocked_by_price_drift(scfg, env):
    ex, broker = FakeYahoo(), FakeBroker()
    [pid] = run(scfg, ex, broker=broker)
    engine.confirm(scfg, pid, True, NOW + timedelta(minutes=1), exchange=ex, broker=broker)
    stop = q(scfg, "SELECT stop_price FROM positions")[0][0]
    ex.prices["RHM/EUR"] = stop * 0.99
    run(scfg, ex, now=NOW + timedelta(minutes=15), broker=broker)
    sell = q(scfg, "SELECT id FROM proposals WHERE side = 'sell'")[0][0]
    ex.prices["RHM/EUR"] = stop * 0.95  # sigue cayendo (−4 % desde la propuesta): hay que vender igual
    text = engine.confirm(scfg, sell, True, NOW + timedelta(minutes=16), exchange=ex, broker=broker)
    assert text.startswith("✅ Vendido")


def test_sell_is_aborted_if_the_broker_stop_cannot_be_cancelled(scfg, env):
    class StuckStop(FakeBroker):
        def cancel_stop(self, order_id):
            self.calls.append(("cancel", order_id))
            raise RuntimeError("no confirmado")

    ex, broker = FakeYahoo(), StuckStop()
    [pid] = run(scfg, ex, broker=broker)
    engine.confirm(scfg, pid, True, NOW + timedelta(minutes=1), exchange=ex, broker=broker)
    stop = q(scfg, "SELECT stop_price FROM positions")[0][0]
    ex.prices["RHM/EUR"] = stop * 0.99
    run(scfg, ex, now=NOW + timedelta(minutes=15), broker=broker)
    sell = q(scfg, "SELECT id FROM proposals WHERE side = 'sell'")[0][0]
    text = engine.confirm(scfg, sell, True, NOW + timedelta(minutes=16), exchange=ex, broker=broker)
    assert "no vendo para no duplicar órdenes" in text
    assert not [c for c in broker.calls if c[0] == "sell"]  # nunca dos órdenes sobre las mismas acciones
    assert q(scfg, "SELECT stop_order_id FROM positions")[0][0] == "stop-2"


def test_positions_without_broker_stop_get_one(scfg, env):
    setup_db(scfg, [("LDO/EUR", 0.1, 100.0, 90.0, None)])
    broker = FakeBroker(cash=40.0, held={"LDO/EUR": {"qty": 0.1, "avg": 100.0, "price": 100.0}})
    run(scfg, broker=broker)
    assert ("stop", "LDO/EUR", 0.1, 90.0) in broker.calls
    assert q(scfg, "SELECT stop_order_id FROM positions WHERE symbol = 'LDO/EUR'")[0][0].startswith("stop-")
    n = len(broker.calls)
    run(scfg, broker=broker, now=NOW + timedelta(minutes=15))  # ya lo tiene: no se repite
    assert not [c for c in broker.calls[n:] if c[0] == "stop"]


# --- auditoría: saldo real, posiciones ajenas, caja tras confirmar, órdenes huérfanas --------------------
def test_broker_balance_is_cached_and_never_raises(scfg):
    with closing(_con(scfg)) as con:
        b = FakeBroker(cash=50.0, held={"RHM/EUR": {"qty": 0.01, "avg": 900.0, "price": 1000.0}})
        r = engine.broker_balance(scfg, con, NOW, broker=b)
        assert (r["cash"], r["positions"], r["value"]) == (50.0, 1, 10.0)

        class Down(FakeBroker):
            def snapshot(self):
                raise SystemExit("Modo live bloqueado")

        assert engine.broker_balance(scfg, con, NOW + timedelta(seconds=30), broker=Down())["cash"] == 50.0
        r = engine.broker_balance(scfg, con, NOW + timedelta(seconds=120), broker=Down())
        assert "Modo live bloqueado" in r["error"]


def test_manually_bought_position_gets_no_order_without_permission(scfg, env):
    broker = FakeBroker(cash=40.0, held={"LDO/EUR": {"qty": 0.1, "avg": 100.0, "price": 100.0}})
    run(scfg, broker=broker)
    run(scfg, broker=broker, now=NOW + timedelta(minutes=15))
    assert not [c for c in broker.calls if c[0] == "stop"]  # nunca una orden que no aprobaste
    assert sum("comprada fuera del bot" in n for n in env.notices) == 1


def test_confirmed_trade_updates_cash_and_equity_at_once(scfg, env):
    ex, broker = FakeYahoo(), FakeBroker()
    [pid] = run(scfg, ex, broker=broker)
    before = q(scfg, "SELECT cash, equity FROM equity_daily")[0]
    engine.confirm(scfg, pid, True, NOW + timedelta(minutes=1), exchange=ex, broker=broker)
    eur = q(scfg, "SELECT eur FROM orders")[0][0]
    after = q(scfg, "SELECT cash, equity FROM equity_daily")[0]
    assert after["cash"] == pytest.approx(before["cash"] - eur)
    assert after["equity"] == pytest.approx(before["equity"])  # efectivo -> acciones: mismo capital


def test_orphan_pending_order_is_reported(scfg, env):
    setup_db(scfg)
    with closing(_con(scfg)) as con, con:
        con.execute("INSERT INTO orders(day, client_order_id, mode, side, created, status, symbol) VALUES "
                    "(?,?,?,?,?,?,?)", ("2026-11-03", "t212:9", "live", "buy",
                                        (NOW - timedelta(minutes=20)).isoformat(), "pending", "RHM/EUR"))
    run(scfg)
    assert q(scfg, "SELECT status FROM orders WHERE client_order_id = 't212:9'")[0][0] == "unknown"
    assert any("quedó sin confirmar" in n for n in env.notices)


def test_take_profit_proposes_a_sale(scfg, env):
    scfg["portfolio"]["tiers"] = [{"name": "semilla", "up_to_eur": 1000.0, "take_profit_pct": 0.04}]
    setup_db(scfg, [("RHM/EUR", 0.1, 90.0, 80.0, None)])  # entró a 90; ahora vale 100 (+11 %)
    broker = FakeBroker(cash=40.0, held={"RHM/EUR": {"qty": 0.1, "avg": 90.0, "price": 100.0}})
    run(scfg, broker=broker)
    sell = q(scfg, "SELECT reason, urgent FROM proposals WHERE side = 'sell'")[0]
    assert sell["reason"].startswith("objetivo de beneficio: +11.1%") and sell["urgent"] == 0


def test_kronos_runs_once_per_tick_in_shadow(scfg, env, monkeypatch):
    calls = []

    def scores(series, pred_len, size):
        calls.append((sorted(series), pred_len, size))
        return {s: 0.7 for s in series}
    monkeypatch.setattr(engine.kronos_signal, "scores", scores)
    scfg["kronos"] = {"enabled": True, "size": "mini", "pred_len": 8}
    run(scfg)
    assert calls == [(sorted(SYMS), 8, "mini")]  # un solo lote con todos los valores
    raw = json.loads(q(scfg, "SELECT raw_json FROM ticks WHERE symbol = 'RHM/EUR'")[0][0])
    assert raw["kronos"] == 0.7
    assert "kronos" not in json.loads(q(scfg, "SELECT contributions_json FROM ticks LIMIT 1")[0][0]) or \
        scfg["weights"].get("kronos", 0) > 0  # sombra: sin peso no cuenta


def test_thinking_is_saved_and_every_command_and_button_works(scfg, env, monkeypatch):
    """Pulsa todos los comandos de /ayuda y todos los botones de una propuesta: ninguno falla."""
    ex = FakeYahoo()
    monkeypatch.setattr(chart.market, "make_exchange", lambda *a, **k: ex)
    photos = []
    monkeypatch.setattr(telegram, "send_photo", lambda img, cap="", env=None, markup=None:
                        photos.append((img[:8], cap, markup)) or True)
    [pid] = run(scfg, ex)
    th = json.loads(q(scfg, "SELECT value FROM kv WHERE key = 'thinking'")[0][0])
    assert th["lines"][0].startswith("Capital 50.00 €")
    assert any("Propongo comprar RHM" in x for x in th["lines"])
    assert any(x.startswith("Vigilo ") for x in th["lines"])
    portfolio.register([scfg])
    with closing(db.connect(Path(scfg["general"]["data_dir"]) / "newsbot.db")) as con:
        for name, (_, usage, *_rest) in telegram._commands(scfg).items():
            arg = {"<id>": str(pid), "SYM": "RHM", "VALOR": "RHM", "CONFIRMAR": "CONFIRMAR"}.get(usage, "")
            out = telegram.handle(con, scfg, f"/{name} {arg}".strip(), NOW)
            assert out and out != telegram.ERROR and "Comando no reconocido" not in out, name
        assert "💭 <b>Lo que pienso</b>" in telegram.handle(con, scfg, "/pensamiento", NOW)
    assert photos and photos[-1][0] == b"\x89PNG\r\n\x1a\n" and "RHM" in photos[-1][1]
    # botones de la propuesta: 📈 (callback) y TradingView (enlace)
    _, markup = env.sent[0]
    data = markup["inline_keyboard"][1][0]["callback_data"]
    n = len(photos)
    calls = []
    with closing(db.connect(Path(scfg["general"]["data_dir"]) / "newsbot.db")) as con:
        upd = {"update_id": 1, "callback_query": {"id": "c", "data": data,
                                                  "message": {"message_id": 7, "chat": {"id": 42}}}}
        telegram.poll(con, scfg, {"TELEGRAM_BOT_TOKEN": "T", "TELEGRAM_CHAT_ID": "42"},
                      get=lambda u: {"result": [upd]}, post=lambda u, p: calls.append(p) or {"ok": True})
    assert len(photos) == n + 1 and calls[0]["text"] == "Dibujando el gráfico…"
    assert markup["inline_keyboard"][1][1]["url"] == "https://www.tradingview.com/chart/?symbol=XETR:RHM"


def test_budget_limits_cash_and_compounds_with_trades(scfg, env):
    """Dos carteras en una cuenta: cada una ve solo su presupuesto, que sube y baja con sus operaciones."""
    scfg["portfolio"]["budget_eur"] = 20.0
    setup_db(scfg, [("LDO/EUR", 0.05, 100.0, 90.0, None)])  # ya tenía 5 € invertidos
    broker = FakeBroker(cash=45.0, held={"LDO/EUR": {"qty": 0.05, "avg": 100.0, "price": 100.0}})
    run(scfg, now=NOW, broker=broker, dry_run=True)  # el dry-run no apunta nada
    assert q(scfg, "SELECT count(*) FROM kv WHERE key LIKE 'budget:%'")[0][0] == 0
    [pid] = run(scfg, broker=broker)
    assert float(q(scfg, "SELECT value FROM kv WHERE key = 'budget:cash:paper'")[0][0]) == 15.0
    assert q(scfg, "SELECT cash FROM equity_daily")[0][0] == 15.0  # no los 45 € de la cuenta
    eur = q(scfg, "SELECT eur FROM proposals WHERE id = ?", (pid,))[0][0]
    engine.confirm(scfg, pid, True, NOW + timedelta(minutes=1), exchange=FakeYahoo(), broker=broker)
    fee = 0.0  # FakeBroker no cobra comisión
    assert float(q(scfg, "SELECT value FROM kv WHERE key = 'budget:cash:paper'")[0][0]) == \
        pytest.approx(15.0 - eur - fee)
    scfg["portfolio"]["budget_eur"] = 25.0  # subes el presupuesto: se suma la diferencia
    with closing(db.connect(Path(scfg["general"]["data_dir"]) / "newsbot.db")) as con:
        assert engine.budget_cash(scfg, con) == pytest.approx(20.0 - eur - fee)
        assert engine._own_cash(scfg, con, 3.0) == 3.0  # nunca más de lo que hay en la cuenta


def test_next_tick_speeds_up_when_the_market_moves():
    sp = {"min_minutes": 2, "max_minutes": 10, "fast_above": 0.5, "slow_below": 0.15, "near_stop_pct": 0.015}
    mk = lambda v: {s: {"feats": {"intraday": v}, "price": 100.0} for s in ("A", "B", "C")}  # noqa: E731
    assert engine._next_minutes(sp, mk(0.8), {}, 5) == 2
    assert engine._next_minutes(sp, mk(0.05), {}, 5) == 10
    assert engine._next_minutes(sp, mk(0.3), {}, 5) == 5
    assert engine._next_minutes(sp, mk(0.05), {"A": {"stop_price": 99.0}}, 5) == 2  # cerca del stop


def test_breaking_local_news_brings_claude_forward(scfg, env, monkeypatch):
    setup_db(scfg, kv={"llm:last:RHM": '{"ts": 1, "sentiment": {}}', "llm:last:LDO": '{"ts": 1}'})
    monkeypatch.setattr(engine.localnews, "analyze", lambda kv, cfg, code, pairs:
                        (0.6, ["Rheinmetall wins big Bundeswehr order"]) if code == "RHM" else (0.1, []))
    run(scfg)
    keys = {r[0] for r in q(scfg, "SELECT key FROM kv WHERE key LIKE 'llm:last:%'")}
    assert "llm:last:LDO" in keys and "llm:last:RHM" not in keys  # solo se adelanta el de la noticia
    [alert] = [n for n in env.notices if n.startswith("⚡")]  # un solo aviso por ciclo
    assert "• RHM: «Rheinmetall wins big Bundeswehr order»" in alert
    raw = json.loads(q(scfg, "SELECT raw_json FROM ticks WHERE symbol = 'RHM/EUR'")[0][0])
    assert raw["finbert"] == 0.6


def test_general_market_headline_is_not_breaking_news(scfg, env, monkeypatch):
    """Un titular que menciona muchos valores (comentario de mercado) no adelanta Claude en ninguno."""
    scfg["news"]["assets"] = {c: ["market"] for c in scfg["news"]["assets"]}
    setup_db(scfg, kv={f"llm:last:{engine._code(x)}": '{"ts": 1}' for x in SYMS})
    h = Headline("Market rallies as inflation cools", "", "reuters", NOW.astimezone(UTC))
    monkeypatch.setattr(engine.ingest, "collect", lambda cfg, now: [h])
    monkeypatch.setattr(engine.localnews, "analyze", lambda kv, cfg, code, pairs: (0.5, [h.title]))
    scfg["local_news"] = {"enabled": True}
    run(scfg)
    assert len(q(scfg, "SELECT key FROM kv WHERE key LIKE 'llm:last:%'")) == len(SYMS)
    assert not [n for n in env.notices if n.startswith("⚡")]


def test_headlines_are_reused_for_refresh_minutes(scfg, monkeypatch):
    calls = []
    monkeypatch.setattr(engine, "_NEWS", {})
    monkeypatch.setattr(engine.ingest, "collect", lambda cfg, now: calls.append(now) or [])
    scfg["news"]["refresh_minutes"] = 3
    for m in (0, 1, 2, 3, 4):
        engine._headlines(scfg, NOW + timedelta(minutes=m))
    assert calls == [NOW, NOW + timedelta(minutes=3)]  # cada 3 min, no en cada ciclo de 1 min
    scfg["news"]["refresh_minutes"] = 0
    engine._headlines(scfg, NOW + timedelta(minutes=4))
    assert len(calls) == 3  # sin refresh_minutes, como antes: en cada ciclo


def test_trailing_only_arms_after_gaining_its_distance():
    from newsbot.strategy.fusion import decide
    cfg = config_mod.load(ROOT / "config_usa.toml")  # trailing 1,5 %
    kw = dict(news=0.0, trend=0.0, cash=10.0, qty=0.1, avg_entry=100.0, min_order_eur=1.0, cfg=cfg)
    # recién comprada (máximo = entrada): cae un 1,6 % → NO es trailing (sería un stop por ruido)
    assert "trailing" not in decide(price=98.4, high_since_entry=100.0, **kw).reason
    # ya subió un 3 % (trailing armado) y cae un 1,6 % desde el máximo → vende
    assert "trailing" in decide(price=101.3, high_since_entry=103.0, **kw).reason


def test_no_rebuy_after_selling_and_new_entry_filters(scfg, env):
    setup_db(scfg, kv={"sold:RHM/EUR": NOW.isoformat()})
    scfg["risk"]["cooldown_minutes"] = 60
    run(scfg)  # RHM es la mejor señal, pero se acaba de vender: propone otra
    rows = q(scfg, "SELECT symbol FROM proposals WHERE side = 'buy'")
    assert [r["symbol"] for r in rows] != ["RHM/EUR"]
    tick = {t["symbol"]: t["reason"] for t in q(scfg, "SELECT symbol, reason FROM ticks")}
    assert tick["RHM/EUR"].startswith("la vendí hace poco")


def test_open_and_market_filters_block_buys(scfg, env):
    scfg["session"]["open"] = "10:20"  # abrió hace 10 min
    scfg["strategy"]["no_buy_first_minutes"] = 15
    assert run(scfg) == []
    tick = {t["symbol"]: t["reason"] for t in q(scfg, "SELECT symbol, reason FROM ticks")}
    assert tick["RHM/EUR"].startswith("primeros 15 min tras la apertura")
    scfg["strategy"]["no_buy_first_minutes"] = 0
    scfg["strategy"]["min_market_intraday"] = 2.0  # imposible de cumplir: el mercado "cae"
    assert run(scfg, now=NOW + timedelta(minutes=5)) == []


def test_loss_sends_a_postmortem_with_an_improvement_to_accept(scfg, env, monkeypatch):
    monkeypatch.setenv("NEWSBOT_SYNC_POSTMORTEM", "1")
    ex, broker = FakeYahoo(), FakeBroker()
    [pid] = run(scfg, ex, broker=broker)
    engine.confirm(scfg, pid, True, NOW + timedelta(minutes=1), exchange=ex, broker=broker)
    stop = q(scfg, "SELECT stop_price FROM positions")[0][0]
    ex.prices["RHM/EUR"] = stop * 0.99
    run(scfg, ex, now=NOW + timedelta(minutes=15), broker=broker)
    sell = q(scfg, "SELECT id FROM proposals WHERE side = 'sell'")[0][0]
    engine.confirm(scfg, sell, True, NOW + timedelta(minutes=16), exchange=ex, broker=broker)
    autopsy = [(t, m) for t, m in env.sent if t.startswith("🔬 <b>Autopsia de la pérdida</b>")]
    assert len(autopsy) == 1
    text, markup = autopsy[0]
    assert "<b>Por qué compré:</b> Compro por" in text and "<b>Qué pasó:</b>" in text
    prop = q(scfg, "SELECT id, status, note FROM params_history")
    if prop:  # se propuso una mejora: llega con sus botones
        assert prop[0]["status"] == "proposed" and json.loads(prop[0]["note"])["origin"] == "postmortem"
        assert markup["inline_keyboard"][0][0]["callback_data"] == f"m:bolsa:{prop[0]['id']}:y"
    else:
        assert markup is None and "ningún cambio" in text


def test_position_sold_by_hand_uses_the_real_fill_from_the_broker(scfg, env):
    """Venta a mano en la app: el bot lee el precio real del historial (no el stop), la apunta en
    orders y estudia la pérdida."""
    class Hist(FakeBroker):
        def closed_sell(self, symbol, since, qty=None):
            self.calls.append(("history", symbol, qty))
            return Fill("sell", 0.1, 95.0, 0.0, 9.5), "manual"

    setup_db(scfg, [("LDO/EUR", 0.1, 100.0, 90.0, "s1")])
    broker = Hist(cash=60.0)
    run(scfg, broker=broker)
    run(scfg, broker=broker, now=NOW + timedelta(minutes=1))
    assert ("history", "LDO/EUR", 0.1) in broker.calls
    o = q(scfg, "SELECT * FROM orders WHERE client_order_id LIKE 'ext:%'")[0]
    assert (o["side"], o["status"], o["qty"], o["price"], o["eur"]) == ("sell", "filled", 0.1, 95.0, 9.5)
    note = next(n for n in env.notices if "LDO" in n and "fuera del bot" in n)
    assert note.startswith("🖐") and "a 95.00 €" in note and "estimado" not in note
    assert "Stop ejecutado" not in " ".join(env.notices)


def test_vanished_position_without_history_is_marked_estimated(scfg, env):
    setup_db(scfg, [("LDO/EUR", 0.1, 100.0, 90.0, "s1")])
    broker = FakeBroker(cash=60.0)
    run(scfg, broker=broker)
    run(scfg, broker=broker, now=NOW + timedelta(minutes=1))
    o = q(scfg, "SELECT status, price FROM orders WHERE client_order_id LIKE 'ext:%'")[0]
    assert tuple(o) == ("estimated", 90.0)
    assert any("Stop ejecutado" in n and "estimado" in n for n in env.notices)


def test_frozen_prices_block_buys_and_warn_once(scfg, env, monkeypatch):
    """El 5-nov Yahoo dio el mismo precio a todos los valores durante 1 h 40 min: sin compras."""
    monkeypatch.setattr(engine, "FROZEN_MINUTES", 14)
    ex = FakeYahoo()
    run(scfg, ex)  # ciclo de referencia
    later = NOW + timedelta(minutes=15)
    assert run(scfg, ex, now=later) == []  # todos idénticos 15 min después: congelados
    reasons = [r[0] for r in q(scfg, f"SELECT reason FROM ticks WHERE ts = {int(later.timestamp())}")]
    assert any("precios congelados" in r for r in reasons)
    assert sum("congelada" in n for n in env.notices) == 1
    run(scfg, ex, now=later + timedelta(minutes=1))
    assert sum("congelada" in n for n in env.notices) == 1  # un solo aviso
    ex.prices = {s: p * 1.01 for s, p in ex.prices.items()}
    run(scfg, ex, now=later + timedelta(minutes=2))
    assert any("vuelven a moverse" in n for n in env.notices)


def test_idle_note_once_a_day_in_the_last_hour(scfg, env):
    setup_db(scfg)
    with closing(db.connect(Path(scfg["general"]["data_dir"]) / "newsbot.db")) as con:
        kv, lines = db.KV(con), ["💶 Dinero parado: 9.00 € libres", "Qué haría: esperar."]
        close = engine._close_at(scfg, NOW)
        engine._idle_note(scfg, kv, lines, close - timedelta(hours=2))
        assert not any("Dinero parado" in n for n in env.notices)  # aún no es la última hora
        engine._idle_note(scfg, kv, lines, close - timedelta(minutes=30))
        engine._idle_note(scfg, kv, lines, close - timedelta(minutes=10))
        notes = [n for n in env.notices if "Dinero parado" in n]
        assert len(notes) == 1 and notes[0].startswith("<b>💶 Dinero parado") and "• Qué haría" in notes[0]


def test_panic_day_warns_once_records_the_day_and_follows_up(scfg, env):
    """Casi todo cae a la vez: un aviso al día (sin comprar por ello) y seguimiento en /pensamiento."""
    ex = FakeYahoo()
    run(scfg, ex)
    assert not any("pánico" in n for n in env.notices)  # día normal
    ex.prices = {s: 95.0 for s in SYMS}  # −5 % todos frente al cierre de ayer
    run(scfg, ex, now=NOW + timedelta(minutes=5))
    run(scfg, ex, now=NOW + timedelta(minutes=10))
    alerts = [n for n in env.notices if "Día de pánico" in n]
    assert len(alerts) == 1 and "4 de 4 valores" in alerts[0] and "−5.0 %" in alerts[0]
    assert "No compro por esto" in alerts[0]
    with closing(db.connect(Path(scfg["general"]["data_dir"]) / "newsbot.db")) as con:
        days = json.loads(db.KV(con).get("panic:days"))
        assert days[0]["day"] == NOW.date().isoformat() and days[0]["prices"]["RHM/EUR"] == 95.0
    ex.prices = {s: 99.75 for s in SYMS}  # rebota un 5 % desde el pánico
    run(scfg, ex, now=NOW + timedelta(minutes=15))
    with closing(db.connect(Path(scfg["general"]["data_dir"]) / "newsbot.db")) as con:
        lines = json.loads(db.KV(con).get("thinking"))["lines"]
    assert any(ln.startswith("📉 Desde el pánico del 03/11") and "mediana +5.0 %" in ln for ln in lines)


def test_wind_down_never_buys_and_sells_after_the_set_hour(scfg, env):
    """Cartera en cierre: no compra nada y propone vender lo que tiene, pero solo desde wind_down_after."""
    scfg["general"] |= {"wind_down": True, "wind_down_after": "15:45"}
    setup_db(scfg, [("RHM/EUR", 0.1, 99.0, 80.0, None)])
    broker = FakeBroker(cash=40.0, held={"RHM/EUR": {"qty": 0.1, "avg": 99.0, "price": 100.0}})
    run(scfg, broker=broker)  # 10:30: aún no
    assert q(scfg, "SELECT count(*) FROM proposals")[0][0] == 0
    run(scfg, broker=broker, now=NOW.replace(hour=15, minute=50))
    rows = q(scfg, "SELECT symbol, side, reason FROM proposals")
    assert [(r["symbol"], r["side"]) for r in rows] == [("RHM/EUR", "sell")]
    assert rows[0]["reason"].startswith(engine.WIND_DOWN_REASON)


def test_wind_down_warns_once_when_the_portfolio_is_empty(scfg, env):
    scfg["general"] |= {"wind_down": True}
    run(scfg)
    run(scfg, now=NOW + timedelta(minutes=5))
    assert q(scfg, "SELECT count(*) FROM proposals")[0][0] == 0
    assert len([n for n in env.notices if "ya está vacía" in n]) == 1
