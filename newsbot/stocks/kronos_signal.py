"""Componente "kronos": previsión de las próximas velas con Kronos (shiyu-coder/Kronos, MIT), en local (CPU).

Kronos es un modelo preentrenado con velas de 45 bolsas: dadas las últimas velas OHLCV predice las siguientes.
El componente es el retorno previsto a `pred_len` velas, medido en ATR de esas velas y acotado a [-1, 1].
Corre en tu ordenador (no gasta Claude). Entra en SOMBRA (peso 0): learn mide si acierta antes de que cuente.
Código del modelo en vendor/Kronos (clonado y revisado); pesos de Hugging Face (NeoQuasar/*, safetensors).
"""
import logging
import sys
import threading
from pathlib import Path

log = logging.getLogger(__name__)
VENDOR = Path(__file__).resolve().parents[2] / "vendor" / "Kronos"
MODELS = {"mini": ("NeoQuasar/Kronos-mini", "NeoQuasar/Kronos-Tokenizer-2k", 2048),
          "small": ("NeoQuasar/Kronos-small", "NeoQuasar/Kronos-Tokenizer-base", 512)}
_lock = threading.Lock()
_predictor = {}


def available() -> bool:
    return (VENDOR / "model" / "kronos.py").exists()


def _load(size: str):
    """Carga una sola vez por proceso (tarda unos segundos; luego queda en memoria)."""
    with _lock:
        if size not in _predictor:
            if str(VENDOR) not in sys.path:
                sys.path.insert(0, str(VENDOR))
            from model import Kronos, KronosPredictor, KronosTokenizer  # vendor/Kronos/model
            name, tok, ctx = MODELS[size]
            model, tokenizer = Kronos.from_pretrained(name), KronosTokenizer.from_pretrained(tok)
            _predictor[size] = KronosPredictor(model, tokenizer, device="cpu", max_context=ctx)
        return _predictor[size]


def forecast(candles: list[list[float]], pred_len: int = 8, size: str = "small", lookback: int = 400,
             samples: int = 3, step_ms: int = 900_000):
    """Velas previstas (DataFrame de Kronos) para las `pred_len` siguientes a `candles` ([ms,o,h,l,c,v])."""
    import pandas as pd
    hist = candles[-lookback:]
    df = pd.DataFrame(hist, columns=["ts", "open", "high", "low", "close", "volume"])
    x_ts = pd.to_datetime(df["ts"], unit="ms")
    y_ts = pd.Series(pd.to_datetime([hist[-1][0] + step_ms * (i + 1) for i in range(pred_len)], unit="ms"))
    return _load(size).predict(df=df[["open", "high", "low", "close", "volume"]], x_timestamp=x_ts,
                               y_timestamp=y_ts, pred_len=pred_len, T=1.0, top_p=0.9, sample_count=samples,
                               verbose=False)


def score(candles: list[list[float]], pred_len: int = 8, size: str = "small") -> float:
    """[-1, 1]: retorno previsto al final del horizonte en múltiplos de 2 ATR de las velas (0 si falla)."""
    if not available() or len(candles) < 60:
        return 0.0
    try:
        pred = forecast(candles, pred_len, size)
        return _to_score(candles, float(pred["close"].iloc[-1]))
    except Exception as exc:  # el modelo nunca rompe un tick
        log.warning("Kronos falló (%s)", type(exc).__name__)
        return 0.0


_cache: dict[str, tuple[int, float]] = {}  # símbolo -> (marca de la última vela, puntuación)


def scores(series: dict[str, list[list[float]]], pred_len: int = 8, size: str = "mini", lookback: int = 256,
           step_ms: int = 900_000) -> dict[str, float]:
    """Puntuación de todos los valores en UN lote (Kronos-mini: ~7 s para 13 valores en CPU). Solo recalcula
    los que tienen una vela nueva; el resto sale de la caché. Nunca lanza: si falla, 0."""
    out, todo = {}, {}
    for sym, c in series.items():
        if len(c) < 60:
            out[sym] = 0.0
        elif (hit := _cache.get(sym)) and hit[0] == c[-1][0]:
            out[sym] = hit[1]
        else:
            todo[sym] = c[-lookback:]
    if not todo or not available():
        return out | {s: 0.0 for s in todo}
    try:
        import pandas as pd
        dfs, xs, ys = [], [], []
        n = min(len(c) for c in todo.values())  # Kronos exige la misma longitud en el lote
        for c in todo.values():
            c = c[-n:]
            df = pd.DataFrame(c, columns=["ts", "open", "high", "low", "close", "volume"])
            dfs.append(df[["open", "high", "low", "close", "volume"]])
            xs.append(pd.to_datetime(df["ts"], unit="ms"))
            nxt = [c[-1][0] + step_ms * (i + 1) for i in range(pred_len)]
            ys.append(pd.Series(pd.to_datetime(nxt, unit="ms")))
        preds = _load(size).predict_batch(df_list=dfs, x_timestamp_list=xs, y_timestamp_list=ys,
                                          pred_len=pred_len, T=1.0, top_p=0.9, sample_count=1, verbose=False)
        for (sym, c), pred in zip(todo.items(), preds, strict=True):
            out[sym] = _to_score(c, float(pred["close"].iloc[-1]))
            _cache[sym] = (c[-1][0], out[sym])
    except Exception as exc:  # el modelo nunca rompe un tick
        log.warning("Kronos (lote) falló (%s)", type(exc).__name__)
        out |= {s: 0.0 for s in todo if s not in out}
    return out


def _to_score(candles: list[list[float]], predicted_close: float) -> float:
    last = candles[-1][4]
    trs = [max(c[2] - c[3], abs(c[2] - p[4]), abs(c[3] - p[4]))
           for p, c in zip(candles[-15:-1], candles[-14:], strict=True)]
    atr = sum(trs) / len(trs) if trs else 0.0
    if last <= 0 or atr <= 0:
        return 0.0
    return max(-1.0, min(1.0, (predicted_close - last) / (2 * atr)))
