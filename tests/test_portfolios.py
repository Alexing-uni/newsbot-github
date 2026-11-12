"""Carteras extra (bolsa) en el mismo proceso: hilo propio, horario de mercado y aislamiento."""
import copy
import io
import json
import logging
import sqlite3
import sys
import threading
from contextlib import closing
from pathlib import Path

import pytest
from conftest import NOW, FakeExchange, fixed_headlines, uptrend

from newsbot import bot, portfolio
from newsbot import config as config_mod
from newsbot.news import fng
from newsbot.watchdog import check

SESSION = {"tz": "Europe/Madrid", "days": [0, 1, 2, 3, 4], "open": "09:00", "close": "17:30"}


@pytest.fixture
def bolsa(cfg, tmp_path):
    b = copy.deepcopy(cfg)
    b["general"] |= {"portfolio": "bolsa", "label": "📈 Bolsa", "tick_minutes": 15,
                     "data_dir": str(tmp_path / "bolsa")}
    b["session"] = dict(SESSION)
    return b


def rows(c, sql):
    with closing(sqlite3.connect(Path(c["general"]["data_dir"]) / "newsbot.db")) as con:
        return con.execute(sql).fetchall()


def join_extras():
    for t in threading.enumerate():
        if t.name.startswith("cartera-"):
            t.join(5)


class ClosedToday(FakeExchange):
    """Festivo: el adaptador dice que el valor no ha cotizado hoy."""

    def trading_today(self, symbol, now_ms):
        return False


def test_loop_runs_extra_portfolio_in_its_own_thread(cfg, bolsa, monkeypatch):
    seen, extra_done = [], threading.Event()

    def fake_run_once(c, **k):
        seen.append((c["general"]["data_dir"], portfolio.CURRENT.get(), k))
        if portfolio.CURRENT.get():
            extra_done.set()
        else:
            extra_done.wait(5)  # cripto espera a que la cartera extra haya hecho su tick

    monkeypatch.setattr(bot, "run_once", fake_run_once)
    monkeypatch.setattr(bot, "learn_step", lambda c: None)
    monkeypatch.setattr(bot, "listen_telegram", lambda cfg, stop: None)
    monkeypatch.setattr(bot.session, "is_open", lambda c, now: True)
    bot.loop(cfg, iterations=1, sleep=lambda s: None, extras=[bolsa])
    join_extras()
    assert sorted(seen) == sorted([(cfg["general"]["data_dir"], "", {"intraday": True}),
                                   (bolsa["general"]["data_dir"], "bolsa", {"intraday": True})])
    assert rows(bolsa, "SELECT key FROM kv WHERE key = 'loop:heartbeat'")  # su propio latido
    assert portfolio.CURRENT.get() == ""  # el hilo principal sigue siendo cripto


def test_loop_without_extras_starts_no_portfolio_thread(cfg, monkeypatch):
    names = []

    class Rec(threading.Thread):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            names.append(self.name)

    monkeypatch.setattr(bot.threading, "Thread", Rec)
    monkeypatch.setattr(bot, "run_once", lambda *a, **k: None)
    monkeypatch.setattr(bot, "listen_telegram", lambda cfg, stop: None)
    bot.loop(cfg, iterations=1, sleep=lambda s: None)
    assert len(names) == 1 and not any(n.startswith("cartera-") for n in names)


def test_closed_market_only_collects_headlines(bolsa, monkeypatch):
    calls = []
    monkeypatch.setattr(bot, "run_once", lambda *a, **k: calls.append("run"))
    monkeypatch.setattr(bot, "learn_step", lambda c: calls.append("learn"))
    monkeypatch.setattr(bot.session, "is_open", lambda c, now: False)
    monkeypatch.setattr(bot.ingest, "collect", lambda c, now: fixed_headlines())
    monkeypatch.setattr(fng, "fetch", lambda: calls.append("fng"))
    bot._extra_ticks(bolsa, threading.Event(), iterations=1)
    assert calls == []
    assert rows(bolsa, "SELECT count(*) FROM headlines")[0][0] == len(fixed_headlines()) > 0
    assert rows(bolsa, "SELECT count(*) FROM ticks") == [(0,)]


def test_extra_errors_are_isolated_and_not_spammed(cfg, bolsa, monkeypatch):
    sent, crypto = [], []

    def fake_run_once(c, **k):
        if portfolio.CURRENT.get():
            raise RuntimeError("Yahoo caído")
        crypto.append(1)

    monkeypatch.setattr(bot, "run_once", fake_run_once)
    monkeypatch.setattr(bot, "learn_step", lambda c: None)
    monkeypatch.setattr(bot.session, "is_open", lambda c, now: True)
    monkeypatch.setattr(bot.telegram, "send", lambda text: sent.append((portfolio.LABEL.get(), text)))
    bot._extra_ticks(bolsa, threading.Event(), iterations=3, wait=lambda s: None)  # no lanza
    assert len(sent) == 1 and sent[0][0] == "📈 Bolsa" and "Yahoo caído" in sent[0][1]
    monkeypatch.setattr(bot, "listen_telegram", lambda cfg, stop: None)
    bot.loop(cfg, iterations=2, sleep=lambda s: None, extras=[bolsa])
    join_extras()
    assert crypto == [1, 1]


def test_extra_thread_stops_promptly(bolsa, monkeypatch):
    monkeypatch.setattr(bot.session, "is_open", lambda c, now: False)
    monkeypatch.setattr(bot.ingest, "collect", lambda c, now: [])
    stop = threading.Event()
    t = threading.Thread(target=bot._extra_ticks, args=(bolsa, stop), daemon=True)
    t.start()
    stop.set()
    t.join(5)
    assert not t.is_alive()


def test_fng_gate(cfg, monkeypatch):
    sent = []
    monkeypatch.setattr(bot.telegram, "send", sent.append)

    def boom():
        raise AssertionError("no se consulta F&G")

    monkeypatch.setattr(fng, "fetch", boom)
    cfg["news"]["fng"] = False
    bot.run_once(cfg, FakeExchange(uptrend()), NOW)
    inputs = json.loads(rows(cfg, "SELECT inputs_json FROM decisions")[0][0])
    assert inputs["fng_value"] is None and inputs["fng_signal"] == 0.0
    assert not any("F&G" in s for s in sent)


def test_holiday_forces_hold(bolsa, monkeypatch):
    monkeypatch.setattr(bot.telegram, "send", lambda text: None)
    with portfolio.active(bolsa):
        d = bot.run_once(bolsa, ClosedToday(uptrend()), NOW)
    assert d.action == "HOLD" and d.order_eur == 0 and d.reason == "mercado cerrado hoy para este valor"
    assert rows(bolsa, "SELECT count(*) FROM orders") == [(0,)]
    # sin [session] (cripto) el mismo exchange no fuerza nada
    del bolsa["session"]
    bolsa["general"]["data_dir"] += "2"
    assert bot.run_once(bolsa, ClosedToday(uptrend()), NOW).action == "BUY"


def test_logging_filter_tags_only_extra_portfolios(bolsa):
    out = io.StringIO()
    h = logging.StreamHandler(out)
    h.setFormatter(logging.Formatter("%(message)s"))
    h.addFilter(bot.PortfolioTag())
    lg = logging.getLogger("newsbot.test_tag")
    lg.addHandler(h)
    lg.setLevel(logging.INFO)
    try:
        lg.info("tick %s", 1)
        with portfolio.active(bolsa):
            lg.info("tick %s", 2)
    finally:
        lg.removeHandler(h)
    assert out.getvalue().splitlines() == ["tick 1", "[bolsa] tick 2"]


def _main(monkeypatch, cfg, argv, loader):
    cfg["general"]["portfolios"] = ["config_bolsa.toml"]
    calls, sent = [], []
    monkeypatch.setattr(sys, "argv", ["newsbot", *argv])
    monkeypatch.setattr(config_mod, "load", lambda path: cfg)
    monkeypatch.setattr(config_mod, "load_portfolios", loader, raising=False)
    monkeypatch.setattr(bot, "setup_logging", lambda c: None)
    monkeypatch.setattr(bot, "load_dotenv", lambda: None)
    monkeypatch.setattr(bot.telegram, "send", sent.append)
    monkeypatch.setattr(bot, "run_once", lambda c, **k: calls.append(("run", k)))
    monkeypatch.setattr(bot, "loop", lambda c, **k: calls.append(("loop", k)))
    bot.main()
    return calls, sent


def test_main_with_broken_extra_config_still_runs_crypto(cfg, monkeypatch):
    def broken(c, path):
        raise ValueError("config_bolsa.toml: falta [market.RHM]")

    calls, sent = _main(monkeypatch, cfg, ["--loop"], broken)
    assert calls == [("loop", {"extras": []})]
    assert len(sent) == 1 and "falta [market.RHM]" in sent[0]
    assert portfolio.REGISTRY == {"cripto": cfg}


def test_main_registers_extras_and_passes_them_only_to_loop(cfg, bolsa, monkeypatch):
    calls, sent = _main(monkeypatch, cfg, ["--loop"], lambda c, path: [bolsa])
    assert calls == [("loop", {"extras": [bolsa]})] and sent == []
    assert set(portfolio.REGISTRY) == {"cripto", "bolsa"}
    calls, _ = _main(monkeypatch, cfg, ["--dry-run"], lambda c, path: [bolsa])
    assert calls == [("run", {"dry_run": True})]


def test_watchdog_clears_extra_locks(cfg, bolsa, monkeypatch):
    cfg["general"]["portfolios"] = ["config_bolsa.toml"]
    monkeypatch.setattr(config_mod, "load_portfolios", lambda c, path: [bolsa], raising=False)
    lock = Path(bolsa["general"]["data_dir"]) / ".lock"
    lock.parent.mkdir(parents=True)
    lock.touch()
    with bot._connect(cfg) as con:
        bot.db.KV(con).set("loop:heartbeat", "0")
    started = []
    assert check(cfg, 1_800_000_000.0, pids=lambda: [11], kill=lambda p: None,
                 start=lambda: started.append(1), notify=lambda t: None) == "restarted"
    assert not lock.exists() and started == [1]
