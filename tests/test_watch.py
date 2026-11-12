"""Vigilante de noticias: guarda titulares y adelanta la decisión ante una noticia fuerte."""
import json
import os
import sqlite3
import time
from contextlib import closing
from datetime import timedelta
from pathlib import Path

import pytest
from conftest import NOW, FakeExchange, uptrend

from newsbot import bot
from newsbot.bot import run_once
from newsbot.news import ingest, rss
from newsbot.news.watch import shock


def feed(title, hour="07:00"):
    return (f'<?xml version="1.0"?><rss version="2.0"><channel><title>t</title><item>'
            f"<title>{title}</title><pubDate>Mon, 02 Nov 2026 {hour}:00 GMT</pubDate>"
            "</item></channel></rss>")


BULL = {"a": feed("Bitcoin rally hits record high"), "b": feed("BTC surges as inflows soar"),
        "c": feed("Bitcoin breakout as bulls rebound")}
BEAR = {"a": feed("Bitcoin crash as exchange hacked"), "b": feed("BTC plunges in selloff"),
        "c": feed("Bitcoin slump deepens on fraud fears")}


def news(cfg, feeds):
    hs = ingest.select(rss.fetch(feeds), ingest.keywords(cfg), NOW, 24)
    return hs, [1.0] * len(hs)


def rows(cfg, sql):
    with closing(sqlite3.connect(Path(cfg["general"]["data_dir"]) / "newsbot.db")) as con:
        return con.execute(sql).fetchall()


def test_shock_needs_enough_sources_and_strength(cfg):
    assert shock(cfg, NOW, *news(cfg, BULL), held=None) == ("BTC/EUR", pytest.approx(0.7))
    two = {k: BULL[k] for k in "ab"}
    assert shock(cfg, NOW, *news(cfg, two), held=None) is None
    assert shock(cfg, NOW + timedelta(hours=5), *news(cfg, BULL), held=None) is None  # viejas


def test_shock_with_a_position_only_reacts_to_bad_news(cfg):
    assert shock(cfg, NOW, *news(cfg, BULL), held="BTC/EUR") is None
    assert shock(cfg, NOW, *news(cfg, BEAR), held="BTC/EUR")[1] < -0.5
    assert shock(cfg, NOW, *news(cfg, BEAR), held=None) is None


def test_watch_without_shock_only_saves_headlines(cfg):
    assert bot.watch(cfg, FakeExchange(uptrend()), NOW) is None
    assert rows(cfg, "SELECT count(*) FROM headlines") == [(1,)]
    assert rows(cfg, "SELECT count(*) FROM decisions") == [(0,)]


def test_watch_brings_the_decision_forward_once(cfg):
    cfg["news"]["feeds"] = BULL
    d = bot.watch(cfg, FakeExchange(uptrend()), NOW)
    assert d.action == "BUY"
    inputs = json.loads(rows(cfg, "SELECT inputs_json FROM decisions")[0][0])
    assert inputs["trigger"].startswith("noticia fuerte BTC/EUR")
    assert bot.watch(cfg, FakeExchange(uptrend()), NOW + timedelta(minutes=15)) is None
    assert run_once(cfg, FakeExchange(uptrend()), NOW + timedelta(hours=1)) is None  # 1/día
    assert rows(cfg, "SELECT count(*) FROM orders") == [(1,)]


def test_watch_replaces_a_hold_and_archives_it(cfg):
    cfg["news"]["feeds"] = {}
    assert run_once(cfg, FakeExchange(uptrend()), NOW).action == "HOLD"  # sin noticias: sin ventaja
    cfg["news"]["feeds"] = BULL
    d = bot.watch(cfg, FakeExchange(uptrend()), NOW + timedelta(minutes=30))
    assert d.action == "BUY"
    assert rows(cfg, "SELECT action FROM decisions_archive") == [("HOLD",)]
    assert rows(cfg, "SELECT action FROM decisions") == [("BUY",)]
    # una noticia fuerte más ese día ya no adelanta nada
    assert bot.watch(cfg, FakeExchange(uptrend()), NOW + timedelta(hours=1)) is None


def test_watch_does_nothing_when_disabled_or_paused(cfg):
    cfg["news"]["feeds"] = BULL
    cfg["watch"]["enabled"] = False
    assert bot.watch(cfg, FakeExchange(uptrend()), NOW) is None
    assert rows(cfg, "SELECT count(*) FROM headlines") == [(3,)]


def test_single_instance_lock(cfg):
    with bot.single_instance(cfg):
        lock = Path(cfg["general"]["data_dir"]) / ".lock"
        assert lock.exists()
        with pytest.raises(SystemExit, match="en curso"):
            with bot.single_instance(cfg):
                pass
        old = time.time() - 3600
        os.utime(lock, (old, old))  # candado abandonado (proceso muerto): se reutiliza
        with bot.single_instance(cfg):
            pass
    assert not lock.exists()
