"""Config, noticias, mercado y el flujo completo de run_once."""
import json
import sqlite3
from contextlib import closing
from datetime import UTC, timedelta
from pathlib import Path

import ccxt
import pytest
from conftest import NOW, ROOT, RSS, FakeExchange, uptrend

from newsbot import bot
from newsbot import config as config_mod
from newsbot.bot import run_once
from newsbot.features import market
from newsbot.news import cryptopanic, fng, ingest, rss
from newsbot.news.rss import Headline
from newsbot.storage import db

CRYPTOPANIC = {"results": [
    {"title": "Whales accumulate as price holds", "description": "On-chain data",
     "published_at": "2026-11-02T07:30:00Z", "original_url": "https://example.com/w",
     "votes": {"positive": 25, "negative": 5, "important": 3, "liked": 1}},
    {"title": "Old post", "published_at": "2026-10-20T07:30:00Z", "votes": None},
]}


# --- config -----------------------------------------------------------------------------------
@pytest.mark.parametrize("old,new,msg", [
    ("news = 0.40", "news = 0.5", "sumar 1"),
    ('order_type = "limit_then_market"', 'order_type = "twap"', "order_type"),
    ('"finbert", "lexicon"]', '"finbert", "magia"]', "desconocidos"),
])
def test_config_validation(tmp_path, old, new, msg):
    bad = (ROOT / "config.toml").read_text(encoding="utf-8").replace(old, new)
    (tmp_path / "c.toml").write_text(bad, encoding="utf-8")
    with pytest.raises(ValueError, match=msg):
        config_mod.load(tmp_path / "c.toml")


def test_config_apply_override_is_a_copy(cfg):
    new = config_mod.apply(cfg, {"strategy": {"buy_threshold": 0.3}})
    assert new["strategy"]["buy_threshold"] == 0.3 and cfg["strategy"]["buy_threshold"] == 0.25


# --- noticias ---------------------------------------------------------------------------------
def test_rss_window_keywords_and_dedupe():
    hs = ingest.select(rss.fetch({"a": RSS, "b": RSS}), ["bitcoin"], NOW, 24)
    assert [h.title for h in hs] == ["Bitcoin ETF inflows surge to record"]
    assert hs[0].summary == "Big day"


def test_fuzzy_dedupe_keeps_oldest():
    t0 = NOW.astimezone(UTC).replace(hour=1)
    items = [Headline("Bitcoin ETF inflows surge to a record", "", "decrypt", t0.replace(hour=3)),
             Headline("Bitcoin ETF inflows surge to record!", "", "coindesk", t0),
             Headline("Bitcoin miners sell reserves", "", "decrypt", t0)]
    hs = ingest.select(items, ["bitcoin"], NOW, 24)
    assert sorted(h.source + ":" + h.title for h in hs) == [
        "coindesk:Bitcoin ETF inflows surge to record!", "decrypt:Bitcoin miners sell reserves"]


def test_cryptopanic_parsing_and_votes():
    urls = []
    hs = cryptopanic.fetch("tok", "growth", get=lambda u: urls.append(u) or CRYPTOPANIC)
    assert urls[0].startswith("https://cryptopanic.com/api/growth/v2/posts/?auth_token=tok")
    assert hs[0].votes == (25, 5, 3) and hs[0].url == "https://example.com/w"
    assert hs[1].votes == (0, 0, 0)
    assert [h.title for h in ingest.select(hs, ["bitcoin"], NOW, 24)] == [
        "Whales accumulate as price holds"]  # CryptoPanic ya viene filtrado por moneda
    assert ingest.votes_score(hs) == pytest.approx(20 / 40)
    assert ingest.votes_score([]) == 0.0


def test_cryptopanic_api_error_raises():
    with pytest.raises(RuntimeError, match="Token not found"):
        cryptopanic.fetch("x", get=lambda u: {"status": "api_error", "info": "Token not found"})


def test_collect_survives_cryptopanic_failure(cfg, monkeypatch):
    monkeypatch.setenv("CRYPTOPANIC_TOKEN", "tok")

    def boom(*a, **k):
        raise OSError("down")

    monkeypatch.setattr(cryptopanic, "fetch", boom)
    assert len(ingest.collect(cfg, NOW)) == 1


def test_fng_parsing_and_contrarian_extremes():
    sample = {"data": [{"value": "37", "value_classification": "Fear"}]}
    assert fng.fetch(get=lambda u: sample) == 37
    assert fng.contrarian(10) == pytest.approx(0.8)
    assert fng.contrarian(90) == pytest.approx(-0.8)
    assert fng.contrarian(30) == 0.0 and fng.contrarian(70) == 0.0


def test_weight_source_and_recency():
    sw = {"coindesk": 1.0, "default": 0.5}
    now = NOW.astimezone(UTC)
    assert ingest.weight(Headline("t", "", "coindesk", now), now, sw, 6) == 1.0
    old = Headline("t", "", "coindesk", now - timedelta(hours=6))
    assert ingest.weight(old, now, sw, 6) == pytest.approx(0.5)
    assert ingest.weight(Headline("t", "", "otra", now), now, sw, 6) == 0.5


# --- mercado ----------------------------------------------------------------------------------
def test_closed_candles_drop_current_day():
    closed = market.closed_candles(FakeExchange(uptrend()), "BTC/EUR", int(NOW.timestamp() * 1000))
    assert len(closed) == 60 and closed[-1][0] + market.DAY_MS <= NOW.timestamp() * 1000


def test_trend_regime_atr_vol():
    assert market.trend_score(uptrend(40), 10, 30) == 1.0
    assert market.trend_score(list(reversed(uptrend(40))), 10, 30) == -1.0
    assert market.trend_score([1.0] * 10, 10, 30) == 0.0
    assert market.regime([1.0] * 199, 200) == "unknown"
    assert market.regime([1.0] * 199 + [0.9], 200) == "bear"
    assert market.regime([1.0] * 199 + [1.01], 200) == "lateral"
    assert market.regime([1.0] * 199 + [1.5], 200) == "bull"
    assert market.atr_pct([[0, 100, 102, 98, 100, 1]] * 15, 14) == pytest.approx(0.04)
    assert market.realized_vol([100.0] * 31) == 0.0 and market.realized_vol([1.0]) == 0.0


def test_retry_on_network_error():
    calls = []

    def flaky():
        calls.append(1)
        if len(calls) < 3:
            raise ccxt.ExchangeNotAvailable("down")
        return "ok"

    assert market.with_retry(flaky, sleep=lambda s: None) == "ok" and len(calls) == 3
    with pytest.raises(ccxt.NetworkError):
        market.with_retry(lambda: (_ for _ in ()).throw(ccxt.RequestTimeout("x")), tries=2,
                          sleep=lambda s: None)


# --- extremo a extremo ------------------------------------------------------------------------
def _db(cfg):
    return closing(sqlite3.connect(Path(cfg["general"]["data_dir"]) / "newsbot.db"))


def test_run_once_paper_end_to_end_and_daily_guard(cfg):
    ex = FakeExchange(uptrend())
    d = run_once(cfg, ex, NOW)
    assert d is not None and d.action == "BUY"
    assert run_once(cfg, ex, NOW) is None  # una sola decisión al día
    with _db(cfg) as con:
        assert con.execute("SELECT count(*) FROM headlines").fetchone()[0] == 1
        assert con.execute("SELECT client_order_id, status FROM orders").fetchone() == (
            "2026-11-02", "filled")
        cfg_hash, inputs, ema = con.execute(
            "SELECT config_hash, inputs_json, news_ema FROM decisions").fetchone()
        assert cfg_hash == cfg["_hash"] and "Bitcoin ETF" in inputs and ema > 0
        assert json.loads(inputs)["coins"]["BTC/EUR"]["features"]["atr_pct"] > 0
        qty, equity, high = con.execute(
            "SELECT qty, equity, high_since_entry FROM equity_daily").fetchone()
        assert qty > 0 and equity < 10.0 and high > 0  # pagó comisión + slippage
    journal = (Path(cfg["general"]["data_dir"]) / "journal.csv").read_text(encoding="utf-8")
    assert journal.startswith("day,mode,symbol,action")


def test_next_day_stop_loss_and_news_ema(cfg):
    run_once(cfg, FakeExchange(uptrend()), NOW)
    falling = uptrend() + [50_000 * 1.01**60 * 0.9]  # cae un 10 %: salta el stop-loss
    d = run_once(cfg, FakeExchange(falling), NOW.replace(day=3))
    assert d is not None and d.action == "SELL" and "stop-loss" in d.reason


def dbcon(cfg):
    data = Path(cfg["general"]["data_dir"])
    data.mkdir(parents=True, exist_ok=True)
    return closing(db.connect(data / "newsbot.db"))


def test_kill_switch_latches_and_resume_clears(cfg):
    with dbcon(cfg) as con, con:  # capital 7 € con máximo 10 €: drawdown del 30 %
        con.execute("INSERT INTO equity_daily(day, cash, qty, avg_entry, price, equity, peak, "
                    "high_since_entry) VALUES ('2026-11-01', 7, 0, 0, 1, 7, 10, 0)")
    d = run_once(cfg, FakeExchange(uptrend()), NOW)
    assert d.kill_switch and d.action == "HOLD" and "kill-switch" in d.reason
    with dbcon(cfg) as con:
        assert db.KV(con).get("kill_switch") == "1"
    bot.resume(cfg)
    with dbcon(cfg) as con:
        assert db.KV(con).get("kill_switch") is None


def test_pause_skips_decision(cfg):
    with dbcon(cfg) as con:
        db.KV(con).set("paused", "1")
    assert run_once(cfg, FakeExchange(uptrend()), NOW) is None


def test_applied_params_override_config(cfg):
    with dbcon(cfg) as con, con:
        con.execute("INSERT INTO params_history(created, params_json, status) VALUES "
                    "('x', '{\"strategy\": {\"buy_threshold\": 0.9}}', 'applied')")
    d = run_once(cfg, FakeExchange(uptrend()), NOW)
    assert d.action == "HOLD"  # con umbral 0,9 la señal ~0,67 ya no compra


def test_run_once_survives_fng_failure(cfg, monkeypatch):
    def boom():
        raise OSError("alternative.me caído")

    monkeypatch.setattr(fng, "fetch", boom)
    d = run_once(cfg, FakeExchange(uptrend()), NOW)
    assert d is not None and d.contributions["fng"] == 0.0


def test_dry_run_writes_nothing(cfg, capsys):
    d = run_once(cfg, FakeExchange(uptrend()), NOW, dry_run=True)
    assert d is not None and "[DRY-RUN]" in capsys.readouterr().out
    with _db(cfg) as con:
        assert con.execute("SELECT count(*) FROM decisions").fetchone()[0] == 0


def test_force_reruns_same_day_without_duplicate_decision(cfg):
    ex = FakeExchange(uptrend())
    run_once(cfg, ex, NOW)
    run_once(cfg, ex, NOW.replace(minute=30), force=True)
    with _db(cfg) as con:
        assert con.execute("SELECT count(*) FROM decisions").fetchone()[0] == 1


def test_migrations_from_v1(tmp_path):
    path = tmp_path / "old.db"
    with closing(sqlite3.connect(path)) as con:
        con.executescript(db.MIGRATIONS[0])
        con.execute("PRAGMA user_version = 1")
    with closing(db.connect(path)) as con:
        cols = {r["name"] for r in con.execute("PRAGMA table_info(headlines)")}
        assert {"url", "weight", "votes_json"} <= cols
        assert con.execute("PRAGMA user_version").fetchone()[0] == len(db.MIGRATIONS)


def test_healthcheck(cfg):
    assert bot.healthcheck(cfg, NOW) is False  # nunca ha decidido
    run_once(cfg, FakeExchange(uptrend()), NOW)
    assert bot.healthcheck(cfg, NOW + timedelta(hours=2)) is True
    assert bot.healthcheck(cfg, NOW + timedelta(hours=27)) is False


def test_load_dotenv_does_not_override(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("# c\nNB_A=1\nNB_B = x=y\n", encoding="utf-8")
    monkeypatch.setenv("NB_A", "keep")
    monkeypatch.delenv("NB_B", raising=False)
    bot.load_dotenv(str(tmp_path / ".env"))
    import os
    assert os.environ["NB_A"] == "keep" and os.environ["NB_B"] == "x=y"
    monkeypatch.delenv("NB_B")
