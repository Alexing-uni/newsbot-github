"""Walk-forward: optimiza en una ventana, valida en la siguiente y avanza.

La rejilla solo toca umbrales: sin histórico propio de noticias, optimizar el peso de las noticias
sobre un backtest sin noticias no tiene sentido (solo cambiaría la escala de la tendencia).
"""
from newsbot.config import apply
from newsbot.research.backtest import WARMUP, precompute, run


def identity(cfg: dict) -> dict:
    s = cfg["strategy"]
    return {"strategy": {"buy_threshold": round(s["buy_threshold"], 4),
                         "sell_threshold": round(s["sell_threshold"], 4)}}


def grid(cfg: dict, step: float = 0.05) -> list[dict]:
    """3 × 3 = 9 combinaciones alrededor de los valores actuales: pocas, para no sobreajustar."""
    s = cfg["strategy"]
    return [{"strategy": {"buy_threshold": round(s["buy_threshold"] + db, 4),
                          "sell_threshold": round(s["sell_threshold"] + ds, 4)}}
            for db in (-step, 0.0, step) for ds in (-step, 0.0, step)]


def _ret(candles, feats, cfg, lo, hi) -> float:
    return run(candles, cfg, feats, lo, hi)["final_equity"] / cfg["general"]["initial_capital"]


def walk_forward(candles: list[list[float]], cfg: dict, overrides: list[dict], train_days: int,
                 test_days: int, feats: list[dict] | None = None) -> dict:
    feats = feats or precompute(candles, cfg)
    current = identity(cfg)
    folds, cand, cur = [], 1.0, 1.0
    lo = WARMUP
    while lo + train_days + test_days <= len(candles):
        mid, hi = lo + train_days, lo + train_days + test_days
        # a igualdad de resultado gana la configuración actual
        best = max(overrides, key=lambda o: (_ret(candles, feats, apply(cfg, o), lo, mid),
                                             o == current))
        rb, rc = _ret(candles, feats, apply(cfg, best), mid, hi), _ret(candles, feats, cfg, mid, hi)
        cand, cur = cand * rb, cur * rc
        folds.append({"test": [mid, hi], "best": best, "oos_best": rb, "oos_current": rc})
        lo += test_days
    return {"folds": folds, "oos_candidate": cand, "oos_current": cur,
            "last_best": folds[-1]["best"] if folds else None}
