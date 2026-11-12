"""FinBERT (ProsusAI/finbert) vía transformers. Extra opcional: pip install .[finbert]"""
from functools import lru_cache

from newsbot.news.rss import Headline
from newsbot.sentiment import Sentiment


@lru_cache(maxsize=1)
def _pipeline(model: str):
    from transformers import pipeline  # import perezoso: torch es pesado y opcional

    return pipeline("text-classification", model=model, top_k=None)


def analyze(headlines: list[Headline], weights: list[float], model: str) -> Sentiment:
    results = _pipeline(model)([h.title for h in headlines], truncation=True)
    total = sum(weights)
    score = conf = 0.0
    for labels, w in zip(results, weights, strict=True):
        p = {x["label"].lower(): x["score"] for x in labels}
        score += w * (p.get("positive", 0.0) - p.get("negative", 0.0))
        conf += w * (1 - p.get("neutral", 0.0))
    return Sentiment(score / total, conf / total, f"finbert sobre {len(headlines)} titulares",
                     "finbert")
