"""Modo bucle (--loop): decide en cada tick con máx. operaciones/día y enfriamiento."""
import sqlite3
import threading
from contextlib import closing
from datetime import timedelta
from pathlib import Path

import pytest
from conftest import NOW, FakeExchange, uptrend

from newsbot import bot
from newsbot.bot import run_once
from newsbot.features import market
from newsbot.strategy.fusion import decide

PEAK = uptrend()[-1]


def rows(cfg, sql):
    with closing(sqlite3.connect(Path(cfg["general"]["data_dir"]) / "newsbot.db")) as con:
        return con.execute(sql).fetchall()


def test_intraday_score():
    assert market.intraday_score([100 * 1.002**i for i in range(100)], 8, 32) > 0.5
    assert market.intraday_score([100.0] * 100, 8, 32) == 0.0
    assert market.intraday_score([100.0] * 10, 8, 32) == 0.0  # historial insuficiente
    assert market.intraday_score([100 * 0.99**i for i in range(100)], 8, 32) == -1.0


def test_closed_candles_respects_timeframe():
    ex = FakeExchange(uptrend())
    now_ms = ex.series["BTC/EUR"][-1][0]  # justo cuando acaba la última vela de 15 min
    short = market.closed_candles(ex, "BTC/EUR", now_ms, limit=100, timeframe="15m")
    assert len(short) == 100
    assert len(market.closed_candles(ex, "BTC/EUR", now_ms - 1, 100, "15m")) == 99


def test_cooldown_blocks_signal_trades_but_not_stops(cfg):
    kw = dict(news=1.0, trend=1.0, intraday=1.0, fng=1.0, votes=1.0, price=100.0, cash=10.0,
              qty=0.0, avg_entry=0.0, min_order_eur=5.0, cfg=cfg, atr_pct=0.04)
    assert decide(**kw).action == "BUY"
    d = decide(**kw, cooldown=True)
    assert d.action == "HOLD" and "enfriamiento" in d.reason
    d = decide(**{**kw, "price": 91.0, "cash": 0.0, "qty": 0.1, "avg_entry": 100.0}, cooldown=True)
    assert d.action == "SELL" and "stop-loss" in d.reason


def test_intraday_ticks_trade_hold_and_stop_the_same_day(cfg):
    assert run_once(cfg, FakeExchange(uptrend()), NOW, intraday=True).action == "BUY"
    later = NOW + timedelta(minutes=5)
    d = run_once(cfg, FakeExchange(uptrend()), later, intraday=True)
    assert d.action == "HOLD" and "enfriamiento" in d.reason
    assert rows(cfg, "SELECT action FROM decisions") == [("BUY",)]  # un HOLD no se registra
    crash = FakeExchange(uptrend(), price=PEAK * 0.9)  # -10 %: el stop actúa aunque haya enfriamiento
    d = run_once(cfg, crash, NOW + timedelta(minutes=10), intraday=True)
    assert d.action == "SELL" and "stop-loss" in d.reason
    assert rows(cfg, "SELECT side FROM orders ORDER BY id") == [("buy",), ("sell",)]
    assert len({r[0] for r in rows(cfg, "SELECT client_order_id FROM orders")}) == 2
    assert rows(cfg, "SELECT action FROM decisions_archive") == [("BUY",)]


def test_max_trades_per_day(cfg):
    cfg["risk"]["cooldown_minutes"] = 0
    cfg["risk"]["max_trades_per_day"] = 1
    run_once(cfg, FakeExchange(uptrend()), NOW, intraday=True)
    falling = FakeExchange(list(reversed(uptrend())), price=PEAK)  # señal de venta, sin stop
    d = run_once(cfg, falling, NOW + timedelta(hours=3), intraday=True)
    assert d.action == "HOLD" and "agotó" in d.reason
    assert rows(cfg, "SELECT count(*) FROM orders") == [(1,)]


def test_loop_survives_errors_and_does_not_spam(cfg, monkeypatch):
    calls, sent, sleeps = [], [], []

    def flaky(*a, **k):
        calls.append(k)
        if len(calls) < 3:
            raise RuntimeError("Bitvavo caído")

    monkeypatch.setattr(bot, "run_once", flaky)
    monkeypatch.setattr(bot.telegram, "send", lambda text: sent.append(text))
    monkeypatch.setattr(bot, "listen_telegram", lambda cfg, stop: None)
    bot.loop(cfg, iterations=3, sleep=sleeps.append)
    assert len(calls) == 3 and all(k == {"intraday": True} for k in calls)
    assert len(sent) == 1  # el mismo error dos veces seguidas solo se avisa una
    assert len(sleeps) == 2 and all(0 < s <= 300 for s in sleeps)


def test_loop_listens_telegram_while_ticking(cfg, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    polls, polled, ticked = [], threading.Event(), threading.Event()

    def fake_poll(con, cfg, wait):
        polls.append(wait)
        polled.set()
        ticked.wait(5)  # simula el long polling: sigue abierto mientras corre el tick

    monkeypatch.setattr(bot.telegram, "poll", fake_poll)
    monkeypatch.setattr(bot.telegram, "send", lambda text: None)
    registered = []
    monkeypatch.setattr(bot.telegram, "register_commands", lambda: registered.append(1))
    monkeypatch.setattr(bot, "run_once", lambda *a, **k: polled.wait(5) and ticked.set())
    bot.loop(cfg, iterations=1, sleep=lambda s: None)
    # el tick no espera a que acabe el long polling (que sigue en bucle mientras dure el loop)
    assert ticked.is_set() and polls[0] == 10 and registered == [1]


def test_learn_step_never_breaks_the_loop(cfg, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("x")

    monkeypatch.setattr(bot.learn, "maybe_propose", boom)
    bot.learn_step(cfg)  # solo registra un aviso


def test_loop_skips_tick_when_locked(cfg, monkeypatch):
    calls = []
    monkeypatch.setattr(bot, "run_once", lambda *a, **k: calls.append(1))
    with bot.single_instance(cfg):
        bot.loop(cfg, iterations=1, sleep=lambda s: None)
    assert calls == []


@pytest.mark.parametrize("hours,expected", [(1, True), (3, False)])
def test_cooldown_window(cfg, hours, expected):
    run_once(cfg, FakeExchange(uptrend()), NOW, intraday=True)
    d = run_once(cfg, FakeExchange(list(reversed(uptrend())), price=PEAK),
                 NOW + timedelta(hours=hours), intraday=True)
    assert ("enfriamiento" in d.reason) == expected


def test_loop_warns_once_when_live_is_blocked(cfg, monkeypatch):
    sent = []

    def blocked(*a, **k):
        raise SystemExit("Modo live bloqueado. Falta: BITVAVO_API_KEY y BITVAVO_API_SECRET")

    monkeypatch.setattr(bot, "run_once", blocked)
    monkeypatch.setattr(bot, "learn_step", lambda cfg: None)
    monkeypatch.setattr(bot.telegram, "send", lambda text: sent.append(text))
    bot._ticks(cfg, 3, lambda s: None, 300)
    assert len(sent) == 1 and "BITVAVO_API_KEY" in sent[0]


def test_disabled_portfolio_only_beats(cfg, monkeypatch):
    calls = []
    monkeypatch.setattr(bot, "run_once", lambda *a, **k: calls.append(1))
    monkeypatch.setattr(bot, "learn_step", lambda cfg: calls.append(2))
    cfg["general"]["enabled"] = False
    bot._ticks(cfg, 2, lambda s: None, 300)
    assert calls == []
    with bot._connect(cfg) as con:
        assert bot.db.KV(con).get("loop:heartbeat")  # el vigilante sigue viéndolo vivo
