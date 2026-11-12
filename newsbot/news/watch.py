"""Detección de "noticia fuerte" para el vigilante (python -m newsbot.bot --watch).

Una noticia es fuerte si, en las últimas `watch.window_hours`, al menos `watch.min_sources`
fuentes distintas hablan de la misma moneda y su sentimiento efectivo (score × confianza) supera
`watch.min_strength`. Sin posición solo cuentan las positivas (posible compra); con posición, solo
las negativas sobre esa moneda (posible venta). Lo demás lo decide la ejecución diaria.
"""
from datetime import datetime, timedelta

from newsbot import config as config_mod
from newsbot import sentiment
from newsbot.news import ingest
from newsbot.news.rss import Headline


def shock(cfg: dict, now: datetime, headlines: list[Headline], weights: list[float],
          held: str | None, cache=None) -> tuple[str, float] | None:
    w, assets = cfg["watch"], cfg["news"]["assets"]
    since = now - timedelta(hours=w["window_hours"])
    for sym in [held] if held else cfg["general"]["symbols"]:
        code = config_mod.asset(sym)
        pairs = [(h, wt) for h, wt in zip(headlines, weights, strict=True)
                 if h.published >= since and ingest.mentions(h, assets[code])]
        if len({h.source for h, _ in pairs}) < w["min_sources"]:
            continue
        s = sentiment.analyze([h for h, _ in pairs], cfg, [wt for _, wt in pairs], cache=cache,
                              asset=code)
        eff = s.score * s.confidence
        if (eff <= -w["min_strength"]) if held else (eff >= w["min_strength"]):
            return sym, eff
    return None
