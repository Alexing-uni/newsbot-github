"""Noticias puntuadas en tu ordenador (FinBERT), sin gastar Claude.

Dos usos ([local_news] en el config de la cartera):
- Componente sombra "finbert": tono medio de los titulares del valor (ponderado por fuente y
  recencia, como el de Claude). FinBERT mide el tono, no el efecto en el precio (una guerra es
  "negativa" y sube la defensa): por eso entra con peso 0 y learn decide si sirve.
- Aviso de noticia fuerte: un titular NUEVO de una fuente fiable con tono muy marcado adelanta la
  siguiente lectura de Claude de ese valor (sin esperar sentiment.llm_min_minutes). El tope diario
  de llamadas (llm_max_calls_day) sigue mandando.
Cada titular se puntúa una sola vez por proceso (caché por título).
"""
import hashlib
import json
import logging
import threading
from pathlib import Path

log = logging.getLogger(__name__)
_scores: dict[str, float] = {}  # título -> tono en [-1, 1]
_lock = threading.Lock()
MAX_CACHE = 20_000
SEEN = 300  # titulares recordados por valor para saber cuáles son nuevos
# los tonos sobreviven a los reinicios (FinBERT tarda ~0,1 s por titular en CPU); None = solo memoria
CACHE_FILE: Path | None = Path(__file__).resolve().parents[2] / "data" / "finbert_cache.json"
_loaded = False


def _load_cache() -> None:
    global _loaded
    if _loaded or CACHE_FILE is None:
        return
    _loaded = True
    try:
        _scores.update(json.loads(CACHE_FILE.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        pass


def _save_cache() -> None:
    if CACHE_FILE is None:
        return
    try:
        CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = CACHE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(_scores, ensure_ascii=False), encoding="utf-8")
        tmp.replace(CACHE_FILE)
    except OSError as exc:
        log.warning("No pude guardar la caché de FinBERT (%s)", type(exc).__name__)


def _model(cfg: dict):
    from newsbot.sentiment import finbert  # diferido: torch/transformers solo si se usa
    return finbert._pipeline(cfg["sentiment"].get("finbert_model", "ProsusAI/finbert"))


def score_titles(titles: list[str], cfg: dict, pipe=None) -> dict[str, float]:
    """Tono de cada título (positivo − negativo). Solo pasa por el modelo los que no conoce."""
    with _lock:
        _load_cache()
        new = list(dict.fromkeys(t for t in titles if t not in _scores))
        if new:
            results = (pipe or _model(cfg))(new, truncation=True, batch_size=16)
            for t, labels in zip(new, results, strict=True):
                p = {x["label"].lower(): x["score"] for x in labels}
                _scores[t] = round(p.get("positive", 0.0) - p.get("negative", 0.0), 4)
            if len(_scores) > MAX_CACHE:
                for k in list(_scores)[: len(_scores) - MAX_CACHE]:
                    del _scores[k]
            _save_cache()
        return {t: _scores[t] for t in titles}


def _key(title: str) -> str:
    return hashlib.sha1(title.encode()).hexdigest()[:10]


def analyze(con_kv, cfg: dict, code: str, pairs: list, pipe=None) -> tuple[float, list[str]]:
    """(componente "finbert" del valor, titulares nuevos y fuertes). pairs = [(Headline, peso)].
    Nunca lanza: si el modelo no está, (0, [])."""
    ln = cfg.get("local_news") or {}
    # los titulares viejos o de fuentes flojas casi no pesan: no merece la pena pasarlos por el modelo
    pairs = [(h, w) for h, w in pairs if w >= ln.get("score_min_weight", 0.05)]
    if not ln.get("enabled") or not pairs:
        return 0.0, []
    try:
        tone = score_titles([h.title for h, _ in pairs], cfg, pipe)
    except Exception as exc:
        log.warning("FinBERT no disponible (%s)", type(exc).__name__)
        return 0.0, []
    total = sum(w for _, w in pairs)
    comp = sum(w * tone[h.title] for h, w in pairs) / total if total > 0 else 0.0
    raw = con_kv.get(f"local:seen:{code}")
    seen = json.loads(raw) if raw else None
    fresh = [h for h, w in pairs if _key(h.title) not in set(seen or [])
             and abs(tone[h.title]) >= ln.get("breaking", 0.75) and w >= ln.get("min_weight", 0.5)]
    keys = list(dict.fromkeys([*(seen or []), *(_key(h.title) for h, _ in pairs)]))[-SEEN:]
    con_kv.set(f"local:seen:{code}", json.dumps(keys))
    # la primera vez todo es "nuevo": se aprende lo que hay sin disparar avisos
    return max(-1.0, min(1.0, comp)), ([h.title for h in fresh] if seen is not None else [])
