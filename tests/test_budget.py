"""Freno de gasto de Claude y "cuánto falta para comprar"."""
import json

import pytest

from newsbot import sentiment
from newsbot.reporting import telegram
from newsbot.sentiment import Sentiment, _llm_budgeted, usage_today

SC = {"llm_min_minutes": 120, "llm_max_calls_day": 2}
T0 = 1_800_000_000.0


class Cache(dict):
    def set(self, k, v):
        self[k] = v


def fake(score, calls=1):
    def call(usage):
        if usage is not None and calls:
            usage.update({"calls": calls, "in": 4000, "out": 300})
        return Sentiment(score, 0.8, "r", "llm:claude-haiku-5-5")
    return call


def test_reuses_result_within_window_and_counts_usage():
    cache = Cache()
    assert _llm_budgeted(fake(0.5), SC, cache, "BTC", T0).score == 0.5
    assert _llm_budgeted(fake(-0.9), SC, cache, "BTC", T0 + 3600).score == 0.5  # < 2 h: reutiliza
    assert usage_today(cache, T0) == {"calls": 1, "in": 4000, "out": 300}
    assert _llm_budgeted(fake(-0.9), SC, cache, "BTC", T0 + 7300).score == -0.9  # > 2 h: nueva


def test_daily_cap_falls_back_to_next_method():
    cache = Cache()
    _llm_budgeted(fake(0.1), SC, cache, "BTC", T0)
    _llm_budgeted(fake(0.2), SC, cache, "ETH", T0)
    with pytest.raises(sentiment.NotConfigured, match="agotado"):
        _llm_budgeted(fake(0.3), SC, cache, "SOL", T0)


def test_cache_hit_costs_nothing():
    cache = Cache()
    _llm_budgeted(fake(0.1, calls=0), SC, cache, "BTC", T0)
    assert usage_today(cache, T0)["calls"] == 0


def test_cascade_uses_lexicon_when_budget_is_exhausted(cfg, monkeypatch):
    from conftest import fixed_headlines

    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    cfg["sentiment"]["llm_max_calls_day"] = 0
    cache = Cache()
    s = sentiment.analyze(fixed_headlines(), cfg, cache=cache)
    assert s.method == "lexicon"


def test_needed_to_buy_uses_threshold_or_fee_filter(cfg):
    # coste ida y vuelta 2 × (0,25 % + 0,1 %) = 0,7 %; × 2 = 1,4 %; con ATR 2,5 % → 0,56
    assert telegram.needed_to_buy(cfg, {"_atr": 0.025, "_regime": "bull"}) == pytest.approx(0.56)
    assert telegram.needed_to_buy(cfg, {"_atr": 0.10, "_regime": "bull"}) == 0.25
    assert telegram.needed_to_buy(cfg, {"_atr": 0.10, "_regime": "bear"}) == 0.40
    assert telegram.needed_to_buy(cfg, {"_atr": 0.01}) is None  # nunca compensa
    assert telegram.needed_to_buy(cfg, {}) is None


def test_estado_shows_gap_and_claude_usage(cfg, monkeypatch):
    from datetime import UTC, timedelta

    from conftest import NOW

    from newsbot.bot import _connect
    from newsbot.storage import db
    with _connect(cfg) as con:
        db.save_ticks(con, NOW - timedelta(minutes=1), [{
            "symbol": "BTC/EUR", "price": 1.0, "signal": -0.25, "action": "HOLD", "reason": "r",
            "held": False, "contributions": {}, "raw": {"_atr": 0.025, "_regime": "bull"}}])
        out = telegram.handle(con, cfg, "/estado", NOW.astimezone(UTC))
        assert "🎯 Para comprar BTC necesita señal ≥ +0.56 (le faltan +0.81)" in out
        assert "🤖 Claude: sin API key" in out
        monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
        day = NOW.astimezone().strftime("%Y-%m-%d")
        db.KV(con).set(f"llm:usage:{day}", json.dumps({"calls": 3, "in": 12000, "out": 900}))
        line = telegram._claude_line(con, cfg)
    assert line.startswith("🤖 Claude hoy:") and "/60 llamadas" in line
