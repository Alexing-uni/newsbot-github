"""Varias monedas: se analizan todas y se mantiene como mucho una posición."""
import json
import sqlite3
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import NOW, FakeExchange, uptrend
from test_live import LIVE_ENV, FakeLive

from newsbot import config as config_mod
from newsbot.bot import run_once
from newsbot.news import ingest
from newsbot.news.rss import Headline
from newsbot.reporting import tax
from newsbot.sentiment import llm

ETH_RSS = """<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>
<item><title>Ethereum ETF inflows surge to record</title>
<pubDate>Mon, 02 Nov 2026 06:00:00 GMT</pubDate></item>
</channel></rss>"""
FLAT = [50_000.0] * 61


@pytest.fixture
def multi(cfg):
    cfg["general"]["symbols"] = ["BTC/EUR", "ETH/EUR"]
    cfg["news"]["feeds"] = {"a": ETH_RSS}
    return cfg


def rows(cfg, sql):
    with closing(sqlite3.connect(Path(cfg["general"]["data_dir"]) / "newsbot.db")) as con:
        return con.execute(sql).fetchall()


def test_buys_the_coin_with_the_best_signal(multi, capsys):
    d = run_once(multi, FakeExchange(FLAT, others={"ETH/EUR": uptrend()}), NOW)
    assert d.action == "BUY"
    assert rows(multi, "SELECT symbol, action FROM decisions") == [("ETH/EUR", "BUY")]
    assert rows(multi, "SELECT symbol, side, status FROM orders") == [("ETH/EUR", "buy", "filled")]
    assert rows(multi, "SELECT symbol FROM equity_daily")[0][0] == "ETH/EUR"
    inputs = json.loads(rows(multi, "SELECT inputs_json FROM decisions")[0][0])
    assert inputs["coins"]["BTC/EUR"]["action"] == "HOLD"  # BTC plano: sin ventaja
    assert inputs["coins"]["ETH/EUR"]["sentiment"]["method"] == "lexicon"
    out = capsys.readouterr().out
    assert "Señales del día" in out and "ETH/EUR" in out
    assert {r[0] for r in rows(multi, "SELECT symbol FROM sentiment_runs")} == {"BTC/EUR",
                                                                                "ETH/EUR"}


def test_with_a_position_only_that_coin_is_managed(multi):
    run_once(multi, FakeExchange(FLAT, others={"ETH/EUR": uptrend()}), NOW)
    # al día siguiente BTC sube fuerte, pero ya hay ETH: no se rota, solo se gestiona ETH
    eth = uptrend() + [uptrend()[-1]]
    run_once(multi, FakeExchange(uptrend(62), others={"ETH/EUR": eth}), NOW.replace(day=3))
    assert rows(multi, "SELECT symbol FROM decisions WHERE day='2026-11-03'") == [("ETH/EUR",)]
    assert rows(multi, "SELECT count(*) FROM orders WHERE symbol='BTC/EUR'") == [(0,)]


def test_stop_loss_closes_the_position_and_frees_cash(multi):
    run_once(multi, FakeExchange(FLAT, others={"ETH/EUR": uptrend()}), NOW)
    eth = uptrend() + [uptrend()[-1] * 0.9]
    d = run_once(multi, FakeExchange(FLAT + [50_000.0], others={"ETH/EUR": eth}),
                 NOW.replace(day=3))
    assert d.action == "SELL" and "stop-loss" in d.reason
    assert rows(multi, "SELECT symbol, qty FROM equity_daily WHERE day='2026-11-03'") == [
        (None, 0.0)]


def test_hold_when_no_coin_passes_the_filters(multi):
    multi["news"]["feeds"] = {}
    d = run_once(multi, FakeExchange(FLAT, others={"ETH/EUR": FLAT}), NOW)
    assert d.action == "HOLD" and rows(multi, "SELECT count(*) FROM orders") == [(0,)]


def test_live_detects_the_held_coin_from_balances(multi, monkeypatch, capsys):
    for k, v in LIVE_ENV.items():
        monkeypatch.setenv(k, v)
    multi["general"]["mode"] = "live"
    ex = FakeLive(eur=1.0)
    ex.series["ETH/EUR"] = ex.series["BTC/EUR"]
    ex.prices["ETH/EUR"] = 3_000.0
    ex.short["ETH/EUR"] = ex.short["BTC/EUR"]
    ex.bal["ETH"] = 0.01  # 30 € de ETH (la posición)
    ex.bal["BTC"] = 0.00000001  # polvo: no cuenta
    run_once(multi, ex, NOW, dry_run=True)
    assert "posición: ETH/EUR" in capsys.readouterr().out


def test_config_requires_keywords_for_every_coin(cfg):
    with pytest.raises(ValueError, match="DOGE/EUR"):
        config_mod.apply(cfg, {"general": {"symbols": ["BTC/EUR", "DOGE/EUR"]}})
    with pytest.raises(ValueError, match="misma divisa"):
        config_mod.apply(cfg, {"general": {"symbols": ["BTC/EUR", "ETH/USDC"]},
                               "news": {"assets": {"BTC": ["btc"], "ETH": ["eth"]}}})


def test_mentions_and_keywords(cfg):
    h = Headline("Solana validators upgrade", "", "x", NOW)
    assert ingest.mentions(h, ["solana", "sol"]) and not ingest.mentions(h, ["ethereum"])
    assert "ethereum" in ingest.keywords(config_mod.load(Path(__file__).parents[1] / "config.toml"))


def test_llm_prompt_is_about_the_asset(cfg, monkeypatch):
    prompts = []
    msg = SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(
        type="text", text='{"score": 0.2, "confidence": 0.5, "reason": "x"}')])
    client = SimpleNamespace(messages=SimpleNamespace(
        create=lambda **kw: prompts.append(kw["messages"][0]["content"]) or msg))
    monkeypatch.setattr(llm.anthropic, "Anthropic", lambda: client)
    hs = [Headline("Ethereum and Solana rally", "", "x", NOW),
          Headline("Coinbase lists new tokens", "", "x", NOW)]
    llm.analyze(hs, "m", [1.0, 1.0], asset="ETH", asset_words=("ethereum", "eth"))
    assert "precio de Ethereum" in prompts[0]
    assert "Ethereum and Solana rally" in prompts[0]  # trata del activo: no se anonimiza
    assert "Entidad A lists new tokens" in prompts[0]


def test_fifo_is_separate_per_asset():
    orders = [
        {"day": "2026-01-01", "symbol": "BTC/EUR", "side": "buy", "qty": 1.0, "eur": 100, "fee": 0},
        {"day": "2026-01-02", "symbol": "ETH/EUR", "side": "buy", "qty": 2.0, "eur": 50, "fee": 0},
        {"day": "2026-01-03", "symbol": "ETH/EUR", "side": "sell", "qty": 2.0, "eur": 60, "fee": 0},
    ]
    rows_ = tax.fifo(orders)
    assert len(rows_) == 1 and rows_[0]["activo"] == "ETH"
    assert rows_[0]["ganancia_perdida_eur"] == 10.0
    with pytest.raises(ValueError, match="SOL/EUR"):
        tax.fifo([{"day": "d", "symbol": "SOL/EUR", "side": "sell", "qty": 1, "eur": 1, "fee": 0}])
