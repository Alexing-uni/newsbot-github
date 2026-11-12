import hashlib
import json
import logging
import os
import time
from dataclasses import asdict, dataclass

from newsbot.news.rss import Headline

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Sentiment:
    score: float        # -1..1
    confidence: float   # 0..1
    reason: str
    method: str         # "llm:<modelo>", "finbert", "lexicon" o "none"


def headlines_hash(headlines: list[Headline]) -> str:
    return hashlib.sha256("\n".join(h.title for h in headlines).encode()).hexdigest()[:16]


class NotConfigured(RuntimeError):
    """El método no está disponible por configuración (p. ej. sin API key)."""


def _run(method: str, headlines: list[Headline], cfg: dict, weights: list[float], cache,
         asset: str = "BTC", budget: bool = True) -> Sentiment:
    from newsbot.sentiment import finbert, lexicon, llm

    sc = cfg["sentiment"]
    if method == "llm":
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise NotConfigured("sin ANTHROPIC_API_KEY")
        return _llm_budgeted(lambda usage: llm.analyze(
            headlines, sc["llm_model"], weights, debate=sc["debate"], anonymize=sc["anonymize"],
            cache=cache, asset=asset, asset_words=tuple(cfg["news"]["assets"][asset]),
            usage=usage, description=(cfg.get("market") or {}).get(asset, {}).get("name")),
            sc, cache if budget else None, asset)
    if method == "finbert":
        return finbert.analyze(headlines, weights, sc["finbert_model"])
    return lexicon.analyze(headlines, weights)


def usage_today(cache, now_ts: float | None = None) -> dict:
    """Llamadas y tokens de Claude de hoy (hora local), para el presupuesto y /estado."""
    day = time.strftime("%Y-%m-%d", time.localtime(now_ts or time.time()))
    return json.loads(cache.get(f"llm:usage:{day}") or '{"calls": 0, "in": 0, "out": 0}')


def _llm_budgeted(call, sc: dict, cache, asset: str, now_ts: float | None = None) -> Sentiment:
    """Claude con freno de gasto: reutiliza el último resultado de la moneda durante
    `llm_min_minutes` y no pasa de `llm_max_calls_day` llamadas al día. Agotado el presupuesto,
    la cascada sigue con FinBERT o el léxico: el bot nunca depende de Claude."""
    if cache is None:  # sin estado (p. ej. --compare-sentiment sin base de datos)
        return call(None)
    now_ts = now_ts or time.time()
    last = json.loads(cache.get(f"llm:last:{asset}") or "null")
    if last and now_ts - last["ts"] < sc.get("llm_min_minutes", 120) * 60:
        return Sentiment(**last["sentiment"])
    used = usage_today(cache, now_ts)
    if used["calls"] >= sc.get("llm_max_calls_day", 60):
        raise NotConfigured("presupuesto diario de Claude agotado")
    usage: dict = {}
    s = call(usage)
    if usage.get("calls"):  # un acierto de la caché de titulares no cuesta nada
        day = time.strftime("%Y-%m-%d", time.localtime(now_ts))
        cache.set(f"llm:usage:{day}", json.dumps({k: used[k] + usage.get(k, 0)
                                                  for k in ("calls", "in", "out")}))
    cache.set(f"llm:last:{asset}", json.dumps({"ts": now_ts, "sentiment": asdict(s)}))
    return s


def analyze(headlines: list[Headline], cfg: dict, weights: list[float] | None = None,
            cache=None, asset: str = "BTC") -> Sentiment:
    """Cascada configurable (por defecto Claude → FinBERT → léxico). El primer método que funciona
    gana. El LLM solo puntúa, nunca decide. `weights` (fuente × recencia) pondera el agregado."""
    if not headlines:
        return Sentiment(0.0, 0.0, "sin titulares", "none")
    weights = weights or [1.0] * len(headlines)
    for method in cfg["sentiment"]["cascade"]:
        try:
            return _run(method, headlines, cfg, weights, cache, asset)
        except (NotConfigured, ImportError) as exc:  # sin key o sin el extra: normal, no es un fallo
            log.debug("Sentimiento %s no configurado (%s)", method, exc)
        except Exception as exc:  # fallo real (red, JSON...): pasa al siguiente nivel y se avisa
            log.warning("Sentimiento %s falló (%s); paso al siguiente método", method, exc)
    if cfg["sentiment"].get("fallback", "lexicon") == "none":  # bolsa: el tono no es la dirección
        return Sentiment(0.0, 0.0, "sin LLM: noticias sin puntuar", "none")
    return _run("lexicon", headlines, cfg, weights, cache, asset)


def compare(headlines: list[Headline], cfg: dict, weights: list[float], cache=None) -> dict:
    """Ejecuta todos los métodos disponibles sobre los mismos titulares (--compare-sentiment)."""
    out = {}
    for method in ("llm", "finbert", "lexicon"):
        try:
            out[method] = _run(method, headlines, cfg, weights, cache, budget=False)
        except Exception as exc:
            log.info("compare: %s no disponible (%s)", method, exc)
    return out
