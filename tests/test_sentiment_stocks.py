"""Sentimiento en bolsa: sin léxico de reserva y con la descripción del activo en el prompt."""
from types import SimpleNamespace

import pytest
from conftest import NOW, fixed_headlines

from newsbot import sentiment
from newsbot.news.rss import Headline
from newsbot.reporting import report
from newsbot.sentiment import llm
from newsbot.strategy.fusion import Decision

DESC = "Rheinmetall (German defense manufacturer: ammunition, air defense)"


@pytest.fixture
def prompts(monkeypatch):
    out = []
    msg = SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(
        type="text", text='{"score": 0.4, "confidence": 0.5, "reason": "x"}')])
    client = SimpleNamespace(messages=SimpleNamespace(
        create=lambda **kw: out.append(kw["messages"][0]["content"]) or msg))
    monkeypatch.setattr(llm.anthropic, "Anthropic", lambda: client)
    return out


def test_fallback_none_leaves_news_unscored(cfg):
    cfg["sentiment"]["cascade"] = ["llm"]  # sin ANTHROPIC_API_KEY
    cfg["sentiment"]["fallback"] = "none"
    s = sentiment.analyze(fixed_headlines(), cfg)
    assert s == sentiment.Sentiment(0.0, 0.0, "sin LLM: noticias sin puntuar", "none")


def test_default_fallback_is_still_the_lexicon(cfg):
    cfg["sentiment"]["cascade"] = ["llm"]
    assert sentiment.analyze(fixed_headlines(), cfg).method == "lexicon"


def test_description_and_guide_only_for_stocks(cfg, prompts, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    cfg["news"]["assets"]["RHM"] = ["rheinmetall"]
    cfg["market"] = {"RHM": {"ticker": "RHM.DE", "isin": "DE0007030033", "name": DESC}}
    hs = [Headline("Rheinmetall wins ammunition order", "", "x", NOW)]
    assert sentiment.analyze(hs, cfg, asset="RHM").score == 0.4
    assert f"precio de {DESC}" in prompts[0] and "Horizonte: los próximos 1-3 días" in prompts[0]
    sentiment.analyze(hs, cfg, asset="BTC")  # cripto: sin [market.BTC], prompt de siempre
    assert "precio de Bitcoin" in prompts[1] and "Horizonte" not in prompts[1]


def test_crypto_prompt_bytes_unchanged(prompts):
    hs = [Headline("Bitcoin rallies", "", "x", NOW)]
    llm.analyze(hs, "m", [1.0])
    llm.analyze(hs, "m2", [1.0], description=None)
    expected = (llm.PROMPT.format(asset="Bitcoin") + llm.FORMAT
                + "\n\nTitulares:\n- (peso 1.00) [x] Bitcoin rallies: ")
    assert prompts == [expected, expected]


def test_description_changes_the_cache_key(prompts):
    cache: dict = {}
    kv = SimpleNamespace(get=cache.get, set=cache.__setitem__)
    hs = [Headline("Rheinmetall wins order", "", "x", NOW)]
    llm.analyze(hs, "m", [1.0], cache=kv, asset="RHM", asset_words=("rheinmetall",))
    llm.analyze(hs, "m", [1.0], cache=kv, asset="RHM", asset_words=("rheinmetall",),
                description=DESC)
    assert len(prompts) == 2 and len(cache) == 2


def test_daily_report_can_hide_fng():
    d = Decision("HOLD", 0.0, {"news": 0.0}, 0.0, 0.0, "x")
    args = ("2026-11-02", "paper", "RHM/EUR", d, 1_000.0, 10.0, 10.0, [], None)
    assert "F&G" in report.daily(*args) and "F&G" not in report.daily(*args, show_fng=False)
    assert report.daily(*args, show_fng=False) == report.daily(*args).replace(
        "F&G (alternative.me): None\n", "")
