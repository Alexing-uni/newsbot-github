import logging
import os
import re
from datetime import datetime, timedelta
from difflib import SequenceMatcher

from newsbot.news import cryptopanic, rss
from newsbot.news.rss import Headline

log = logging.getLogger(__name__)


def _norm(title: str) -> str:
    return re.sub(r"\W+", " ", title.lower()).strip()


def is_duplicate(a: str, b: str) -> bool:
    # idea: rapidfuzz fuzz.ratio > 90; difflib da el mismo ratio 2·M/T sin añadir dependencia
    return a == b or SequenceMatcher(None, a, b).ratio() > 0.9


def pattern(keywords: list[str]) -> re.Pattern:
    return re.compile(r"\b(" + "|".join(map(re.escape, keywords)) + r")\b", re.IGNORECASE)


def mentions(h: Headline, keywords: list[str]) -> bool:
    return bool(pattern(keywords).search(f"{h.title} {h.summary}"))


def keywords(cfg: dict) -> list[str]:
    """Todas las palabras clave del universo de monedas."""
    return sorted({k for words in cfg["news"]["assets"].values() for k in words})


def select(items: list[Headline], keywords: list[str], now: datetime,
           window_hours: float) -> list[Headline]:
    """Ventana de `window_hours` hasta `now`, filtro por keywords y dedupe por similitud de título.

    Ante duplicados se queda el más antiguo (la fuente original). CryptoPanic ya viene filtrado por
    moneda, así que no se le aplica el filtro de keywords.
    """
    since = now - timedelta(hours=window_hours)
    pattern_ = pattern(keywords)
    kept: list[Headline] = []
    keys: list[str] = []
    for h in sorted(items, key=lambda h: h.published):
        key = _norm(h.title)
        if not (since <= h.published <= now) or not key:
            continue
        if h.source != "cryptopanic" and not pattern_.search(f"{h.title} {h.summary}"):
            continue
        if any(is_duplicate(key, k) for k in keys):
            continue
        keys.append(key)
        kept.append(h)
    return sorted(kept, key=lambda h: h.published, reverse=True)


def drop_unwanted(items: list[Headline], news: dict) -> list[Headline]:
    """Quita los titulares con una frase de news.exclude ("gold medal", "blue chip"...) y los de
    fuentes con peso 0 explícito en news.source_weights (spam). Sin esas claves no quita nada."""
    exclude = [x.lower() for x in news.get("exclude", [])]
    zero = {k for k, v in news.get("source_weights", {}).items() if v == 0}
    return [h for h in items if h.source not in zero
            and not any(x in f"{h.title} {h.summary}".lower() for x in exclude)]


def collect(cfg: dict, now: datetime) -> list[Headline]:
    n = cfg["news"]
    items = rss.fetch(n["feeds"])
    token = os.environ.get("CRYPTOPANIC_TOKEN")
    if token and n.get("cryptopanic_plan"):  # solo cripto: CryptoPanic no tiene acciones
        try:
            items += cryptopanic.fetch(token, n["cryptopanic_plan"], ",".join(n["assets"]))
        except Exception as exc:  # una fuente caída no para el bot
            log.warning("CryptoPanic falló (%s)", exc)
    return select(drop_unwanted(items, n), keywords(cfg), now, n["window_hours"])


def weight(h: Headline, now: datetime, source_weights: dict[str, float],
           half_life_hours: float) -> float:
    """Peso de la fuente × decaimiento exponencial por antigüedad."""
    age = max(0.0, (now - h.published).total_seconds() / 3600)
    return source_weights.get(h.source, source_weights.get("default", 0.5)) * 0.5 ** (
        age / half_life_hours)


def votes_score(headlines: list[Headline], prior: int = 10) -> float:
    """Votos comunitarios de CryptoPanic en [-1, 1]. El `prior` evita que 2 votos pesen como 200."""
    pos = sum(h.votes[0] for h in headlines)
    neg = sum(h.votes[1] for h in headlines)
    return (pos - neg) / (pos + neg + prior)
