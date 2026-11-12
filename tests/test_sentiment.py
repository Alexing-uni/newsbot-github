"""Cascada de sentimiento con el LLM y FinBERT mockeados."""
import re
from datetime import UTC
from types import SimpleNamespace

import pytest
from conftest import NOW, fixed_headlines

from newsbot import bot, sentiment
from newsbot.news.rss import Headline
from newsbot.sentiment import finbert, lexicon, llm

GOOD = '```json\n{"score": -0.4, "confidence": 0.8, "drivers": ["a"], "reason": "x"}```'


class FakeClient:
    def __init__(self, *texts):
        self.texts, self.prompts = list(texts), []
        self.messages = SimpleNamespace(create=self.create)

    def create(self, **kw):
        self.prompts.append(kw["messages"][0]["content"])
        text = self.texts.pop(0) if len(self.texts) > 1 else self.texts[0]
        return SimpleNamespace(stop_reason="end_turn",
                               content=[SimpleNamespace(type="text", text=text)])


@pytest.fixture
def client(monkeypatch):
    def install(*texts):
        c = FakeClient(*texts)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
        monkeypatch.setattr(llm.anthropic, "Anthropic", lambda: c)
        return c
    return install


class Cache(dict):
    def set(self, k, v):
        self[k] = v


def test_lexicon_used_without_api_key(cfg):
    s = sentiment.analyze(fixed_headlines(), cfg)
    assert s.method == "lexicon" and s.score == 1.0 and s.confidence <= 0.7


def test_lexicon_is_weighted():
    now = NOW.astimezone(UTC)
    hs = [Headline("Bitcoin rally", "", "a", now), Headline("Bitcoin crash", "", "b", now)]
    s = lexicon.analyze(hs, [3.0, 1.0])
    assert s.score == pytest.approx(0.5) and s.confidence == pytest.approx(0.7)


def test_no_headlines(cfg):
    assert sentiment.analyze([], cfg).method == "none"


def test_llm_json_validated(cfg, client):
    client(GOOD)
    s = sentiment.analyze(fixed_headlines(), cfg)
    assert (s.score, s.confidence, s.method) == (-0.4, 0.8, "llm:claude-haiku-5-5")


def test_llm_retries_once_on_invalid_json(cfg, client):
    c = client("no es json", GOOD)
    assert sentiment.analyze(fixed_headlines(), cfg).score == -0.4 and len(c.prompts) == 2


@pytest.mark.parametrize("text", ["no json", '{"score": 3, "confidence": 0.5}'])
def test_llm_bad_output_falls_back_to_lexicon(cfg, client, text):
    client(text)
    assert sentiment.analyze(fixed_headlines(), cfg).method == "lexicon"


def test_llm_cache_avoids_paying_twice(cfg, client):
    c, cache = client(GOOD), Cache()
    sentiment.analyze(fixed_headlines(), cfg, cache=cache)
    sentiment.analyze(fixed_headlines(), cfg, cache=cache)
    assert len(c.prompts) == 1 and sum(len(k) == 68 for k in cache) == 1  # una entrada llm:<hash>


def test_llm_debate_makes_three_calls(cfg, client):
    cfg["sentiment"]["debate"] = True
    c = client("argumento alcista", "argumento bajista", GOOD)
    s = sentiment.analyze(fixed_headlines(), cfg)
    assert len(c.prompts) == 3 and "argumento bajista" in c.prompts[2]
    assert s.method.endswith(":debate")


def test_anonymize_only_when_not_about_the_asset():
    btc = re.compile(r"\b(bitcoin|btc)\b", re.IGNORECASE)
    assert llm.anonymize_text("Solana and Coinbase rally, Solana up", btc) == \
        "Entidad A and Entidad B rally, Entidad A up"
    assert llm.anonymize_text("Coinbase lists Bitcoin options", btc) == \
        "Coinbase lists Bitcoin options"


def test_finbert_in_cascade(cfg, monkeypatch):
    cfg["sentiment"]["cascade"] = ["llm", "finbert", "lexicon"]
    fake = [[{"label": "positive", "score": 0.7}, {"label": "negative", "score": 0.1},
             {"label": "neutral", "score": 0.2}]]
    monkeypatch.setattr(finbert, "_pipeline", lambda model: lambda texts, truncation: fake)
    s = sentiment.analyze(fixed_headlines(), cfg)
    assert s.method == "finbert" and s.score == pytest.approx(0.6)
    assert s.confidence == pytest.approx(0.8)


def test_compare_sentiment_stores_correlation(cfg, client, monkeypatch):
    client(GOOD)
    bot.compare_sentiment(cfg, NOW)
    client('{"score": 0.5, "confidence": 0.9, "reason": "y"}')
    monkeypatch.setattr(lexicon, "analyze",
                        lambda hs, w: sentiment.Sentiment(0.9, 0.7, "", "lexicon"))
    corr = bot.compare_sentiment(cfg, NOW.replace(day=3, hour=0, minute=30))  # titular aún en ventana
    assert corr["lexicon~llm"] == pytest.approx(-1.0)  # LLM sube y léxico baja
