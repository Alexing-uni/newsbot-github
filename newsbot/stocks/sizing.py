"""Tamaño de cada compra de la cartera de bolsa (funciones puras).

Por riesgo: el stop va a `atr_stop_mult` × ATR (entre el 3 % y el 12 %) y la posición se dimensiona
para que, si salta, se pierda como mucho `risk_per_trade_pct` del capital. Encima, los topes de
[portfolio] (posición, sector, caja mínima) y execution.max_order_eur.
"""
from collections.abc import Iterable

STOP_MIN, STOP_MAX = 0.03, 0.12


def with_tier(cfg: dict, equity: float) -> dict:
    """Límites según el capital ([[portfolio.tiers]], por "hasta X €"): con poco dinero, pocas posiciones y
    objetivos de beneficio cortos; al crecer, más reparto y dejar correr las ganancias. Sin niveles, igual."""
    inf = float("inf")
    tiers = sorted(cfg.get("portfolio", {}).get("tiers") or [], key=lambda t: t.get("up_to_eur", inf))
    tier = next((t for t in tiers if equity <= t.get("up_to_eur", inf)), None)
    if not tier:
        return cfg
    over = {k: v for k, v in tier.items() if k not in ("up_to_eur", "name", "take_profit_pct")}
    return cfg | {"portfolio": cfg["portfolio"] | over,
                  "risk": cfg["risk"] | {"take_profit_pct": tier.get("take_profit_pct", 0.0)},
                  "_tier": tier.get("name", f"hasta {tier.get('up_to_eur')} €")}


def stop_pct(atr_pct: float, cfg: dict) -> float:
    """Distancia del stop-loss bajo el precio de entrada (fracción)."""
    return min(STOP_MAX, max(STOP_MIN, cfg["portfolio"]["atr_stop_mult"] * atr_pct))


def order_eur(equity: float, cash: float, signal: float, buy_th: float, atr_pct: float,
              held_groups: Iterable[str], group: str, group_eur: float, cfg: dict) -> tuple[float, str]:
    """Euros a comprar y el límite que manda (o 0 y el motivo).

    `held_groups`: sector de cada posición abierta (y de cada compra pendiente); `group_eur`: euros
    ya invertidos (o pendientes) en el sector de este valor. La fuerza de la señal por encima del
    umbral escala el tamaño entre la mitad y el total."""
    p, x = cfg["portfolio"], cfg["execution"]
    held = list(held_groups)
    min_order = cfg["risk"]["min_order_eur"]
    if equity <= 0:
        return 0.0, "sin capital"
    if len(held) >= p["max_positions"]:
        return 0.0, f"máximo de posiciones ({p['max_positions']})"
    if held.count(group) >= p["max_group_positions"]:
        return 0.0, f"sector {group} lleno ({p['max_group_positions']} valores)"
    strength = min(1.0, max(0.0, (signal - buy_th) / (1 - buy_th))) if buy_th < 1 else 0.0
    sp = stop_pct(atr_pct, cfg)
    by_risk = p["risk_per_trade_pct"] * equity / sp
    limits = [  # (euros, motivo): manda el menor
        (min(p["max_position_pct"] * equity, by_risk) * (0.5 + 0.5 * strength),
         "tamaño por riesgo" if by_risk < p["max_position_pct"] * equity else "tope por posición"),
        (x["max_order_eur"], "tope por orden"),
        (cash - p["min_cash_pct"] * equity, "caja mínima"),
        (p["max_group_pct"] * equity - group_eur, f"tope del sector {group}"),
    ]
    eur, why = min(limits, key=lambda t: t[0])
    if eur < min_order:
        return 0.0, f"{why}: {max(eur, 0.0):.2f} € < mínimo {min_order:.2f} €"
    return eur, why
