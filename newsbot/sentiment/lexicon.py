import re

from newsbot.news.rss import Headline
from newsbot.sentiment import Sentiment

POSITIVE = {
    "surge", "surges", "rally", "rallies", "gain", "gains", "bull", "bullish", "record",
    "adoption", "approve", "approves", "approval", "approved", "inflow", "inflows", "rise",
    "rises", "soar", "soars", "breakout", "rebound", "upgrade", "accumulate",
}
NEGATIVE = {
    "crash", "crashes", "drop", "drops", "fall", "falls", "bear", "bearish", "hack", "hacked",
    "ban", "bans", "lawsuit", "outflow", "outflows", "plunge", "plunges", "selloff", "fraud",
    "liquidation", "liquidations", "decline", "declines", "fear", "exploit", "dump", "slump",
}
MAX_CONFIDENCE = 0.7  # el léxico es el último recurso: nunca más confianza que esto


def analyze(headlines: list[Headline], weights: list[float]) -> Sentiment:
    scores, signal_weights = [], []
    for h, w in zip(headlines, weights, strict=True):
        words = re.findall(r"[a-z]+", f"{h.title} {h.summary}".lower())
        pos = sum(word in POSITIVE for word in words)
        neg = sum(word in NEGATIVE for word in words)
        if pos + neg:
            scores.append((pos - neg) / (pos + neg))
            signal_weights.append(w)
    if not scores or sum(signal_weights) <= 0:
        return Sentiment(0.0, 0.0, "léxico: sin palabras clave", "lexicon")
    score = sum(s * w for s, w in zip(scores, signal_weights, strict=True)) / sum(signal_weights)
    confidence = MAX_CONFIDENCE * sum(signal_weights) / sum(weights)
    return Sentiment(score, confidence, f"léxico: {len(scores)}/{len(headlines)} con señal",
                     "lexicon")
