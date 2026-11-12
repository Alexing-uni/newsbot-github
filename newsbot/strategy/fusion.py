from collections.abc import Mapping
from dataclasses import dataclass

from newsbot.risk import sizing


@dataclass(frozen=True)
class Decision:
    action: str                      # BUY / SELL / HOLD
    signal: float
    contributions: dict[str, float]  # aporte de cada componente a la señal
    target_exposure: float
    order_eur: float                 # + compra, - venta, 0 nada
    reason: str
    kill_switch: bool = False        # True: compras bloqueadas (drawdown o bloqueo previo)


def smooth(prev: float | None, value: float, days: int) -> float:
    """EMA de `days` días sobre el sentimiento efectivo (score × confidence)."""
    alpha = 2 / (days + 1)
    return value if prev is None else alpha * value + (1 - alpha) * prev


def track_high(qty_before: float, qty_after: float, high: float, price: float) -> float:
    """Máximo de precio desde la entrada, para el trailing stop."""
    return 0.0 if qty_after <= 0 else max(high if qty_before > 0 else 0.0, price)


def decide(*, news: float, trend: float, price: float, cash: float, qty: float, avg_entry: float,
           min_order_eur: float, cfg: dict, fng: float = 0.0, votes: float = 0.0,
           regime: str = "unknown", atr_pct: float = 0.0, vol: float = 0.0,
           high_since_entry: float = 0.0, peak: float = 0.0, buy_blocked: bool = False,
           intraday: float = 0.0, cooldown: bool = False,
           extra: Mapping[str, float] | None = None) -> Decision:
    """Función pura y determinista: mismos inputs → misma decisión (bot y backtest la comparten).

    news: sentimiento efectivo suavizado (EMA); fng: Fear & Greed contrario; votes: CryptoPanic;
    intraday: impulso de las últimas horas. cooldown: operó hace poco o agotó las operaciones del
    día, así que solo actúan los stops. extra: componentes sombra (rsi, bollinger...) que solo
    entran en la señal si [weights] les da peso; sin peso se miden (ticks) pero no operan.
    Orden de prioridad: stop-loss > trailing stop > enfriamiento > señal (con régimen y vol target)
    > kill-switch y filtro de ventaja (que solo bloquean abrir o ampliar) > mínimos de orden.
    """
    w, s, r, c = cfg["weights"], cfg["strategy"], cfg["risk"], cfg["costs"]
    contributions = {"news": w["news"] * news, "trend": w["trend"] * trend,
                     "intraday": w.get("intraday", 0.0) * intraday,
                     "fng": w["fng"] * fng, "votes": w["votes"] * votes}
    contributions |= {k: w[k] * v for k, v in (extra or {}).items() if w.get(k, 0.0)}
    signal = sum(contributions.values())
    position = qty * price
    equity = cash + position
    current = position / equity if equity > 0 else 0.0
    drawdown = 1 - equity / peak if peak > 0 else 0.0
    kill = buy_blocked or drawdown >= r["max_drawdown_pct"]
    buy_th = s["buy_threshold_bear"] if regime == "bear" else s["buy_threshold"]

    if qty > 0 and avg_entry > 0 and price <= avg_entry * (1 - r["stop_loss_pct"]):
        target, reason = 0.0, f"stop-loss ({price:.2f} <= {avg_entry * (1 - r['stop_loss_pct']):.2f})"
    elif qty > 0 and r["trailing_stop_pct"] > 0 and high_since_entry > 0 and \
            high_since_entry >= avg_entry * (1 + r["trailing_stop_pct"]) and \
            price <= high_since_entry * (1 - r["trailing_stop_pct"]):
        # el trailing solo se arma cuando ya ha subido su propia distancia: si no, sería un stop-loss
        # del −1,5 % que vende por puro ruido (el stop de pérdidas es el de arriba, por ATR)
        target, reason = 0.0, f"trailing stop (máximo {high_since_entry:.2f})"
    elif cooldown:
        target, reason = current, "enfriamiento: operó hace poco o agotó las operaciones de hoy"
    else:
        target, reason = sizing.target_exposure(signal, current, buy_th, s["sell_threshold"],
                                                r["max_exposure"])
        if regime == "bear" and signal >= s["buy_threshold"]:
            reason += f" (régimen bajista: umbral {buy_th})"
        if signal >= buy_th and r["vol_target"] > 0 and vol > r["vol_target"]:
            target = sizing.vol_scale(target, vol, r["vol_target"])
            reason += f" (vol {vol:.0%} > objetivo {r['vol_target']:.0%})"
        if target > current + 1e-9:
            if kill:
                target, reason = current, f"kill-switch (drawdown {drawdown:.0%}): solo vender/mantener"
            elif not sizing.has_edge(atr_pct, signal, c["fee_pct"], c["slippage_pct"],
                                     r["edge_cost_multiple"]):
                target, reason = current, (f"sin ventaja: movimiento esperado "
                                           f"{atr_pct * min(1, abs(signal)):.2%} < coste")
    order = sizing.order_eur(target, equity, position, cash, min_order_eur,
                             r["min_rebalance_pct"], c["fee_pct"])
    action = "BUY" if order > 0 else "SELL" if order < 0 else "HOLD"
    if action == "HOLD" and abs(target - current) > 1e-9:
        reason += " (ajuste por debajo del mínimo)"
    return Decision(action, signal, contributions, target, order, reason, kill)
