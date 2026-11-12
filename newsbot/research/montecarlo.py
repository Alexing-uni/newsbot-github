"""Monte Carlo de sobreajuste (idea: Jesse). Remuestrea las velas en bloques para crear historias
alternativas con la misma volatilidad. Si el resultado real queda en el 5 % más afortunado de la
distribución, la estrategia probablemente se ajusta a ESTA historia concreta: no se promociona.
"""
import random
import statistics

from newsbot.features.market import DAY_MS
from newsbot.research.backtest import run


def synthetic(candles: list[list[float]], rng: random.Random, block: int = 5) -> list[list[float]]:
    rel = [(c[1] / p[4], c[2] / p[4], c[3] / p[4], c[4] / p[4])
           for p, c in zip(candles, candles[1:], strict=False)]
    out = [list(candles[0])]
    close = candles[0][4]
    while len(out) < len(candles):
        j = rng.randrange(len(rel) - block + 1)
        for o, h, low, c in rel[j:j + block]:
            if len(out) >= len(candles):
                break
            out.append([out[-1][0] + DAY_MS, close * o, close * h, close * low, close * c, 1.0])
            close *= c
    return out


def overfit_test(candles: list[list[float]], cfg: dict, runs: int = 100, seed: int = 42) -> dict:
    rng = random.Random(seed)
    original = run(candles, cfg)["final_equity"]
    sims = [run(synthetic(candles, rng), cfg)["final_equity"] for _ in range(runs)]
    pct = sum(x < original for x in sims) / runs
    return {"original": original, "median_sim": statistics.median(sims),
            "percentile": pct * 100, "suspicious": pct >= 0.95}
