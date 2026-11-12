import math
import statistics


def metrics(equity: list[float]) -> dict:
    """Métricas de una curva de capital diaria (365 días/año: cripto cotiza siempre)."""
    rets = [b / a - 1 for a, b in zip(equity, equity[1:], strict=False) if a > 0]
    years = len(rets) / 365
    cagr = (equity[-1] / equity[0]) ** (1 / years) - 1 if years > 0 and equity[0] > 0 else 0.0
    mean = statistics.fmean(rets) if rets else 0.0
    sd = statistics.pstdev(rets) if len(rets) > 1 else 0.0
    downside = math.sqrt(statistics.fmean([min(r, 0.0) ** 2 for r in rets])) if rets else 0.0
    peak, max_dd = equity[0], 0.0
    for e in equity:
        peak = max(peak, e)
        max_dd = max(max_dd, 1 - e / peak if peak > 0 else 0.0)
    return {"final": equity[-1], "cagr": cagr,
            "sharpe": mean / sd * math.sqrt(365) if sd else 0.0,
            "sortino": mean / downside * math.sqrt(365) if downside else 0.0,
            "max_drawdown": max_dd}
