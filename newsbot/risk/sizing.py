def target_exposure(signal: float, current: float, buy_threshold: float, sell_threshold: float,
                    max_exposure: float) -> tuple[float, str]:
    """Exposición objetivo (fracción del capital total) según la señal."""
    if signal >= buy_threshold:
        strength = min(1.0, (signal - buy_threshold) / (1 - buy_threshold))
        return max_exposure * (0.5 + 0.5 * strength), "señal de compra"
    if signal <= sell_threshold:
        return 0.0, "señal de venta"
    return current, "señal neutra"


def has_edge(atr_pct: float, signal: float, fee_pct: float, slippage_pct: float,
             multiple: float) -> bool:
    """¿Compensa abrir o ampliar? Movimiento esperado = ATR diario (%) × intensidad de la señal
    (acotada a 1). Debe superar `multiple` × el coste de ida y vuelta, 2·(fee + slippage).
    Con BTC (ATR ~2-4 %) y costes del 0,35 % por lado, hace falta una señal de ~0,4 o más."""
    return atr_pct * min(1.0, abs(signal)) >= multiple * 2 * (fee_pct + slippage_pct)


def vol_scale(target: float, vol: float, vol_target: float) -> float:
    """Volatility targeting: reduce la exposición si la volatilidad supera el objetivo."""
    return target * min(1.0, vol_target / vol) if vol_target > 0 and vol > 0 else target


def order_eur(target: float, equity: float, position_value: float, cash: float,
              min_order: float, min_rebalance_pct: float, fee_pct: float) -> float:
    """Euros a comprar (+) o vender (-). 0 si el ajuste no compensa las comisiones.

    El tamaño sale del capital total actual (efectivo + posición): así se reinvierten beneficios.
    """
    delta = target * equity - position_value
    if delta > 0:
        delta = min(delta, cash / (1 + fee_pct))  # deja sitio para la comisión
    # Una salida total solo está limitada por el mínimo del exchange, no por min_rebalance_pct.
    threshold = min_order if target == 0 and delta < 0 else max(min_order,
                                                                 min_rebalance_pct * equity)
    return delta if abs(delta) >= threshold else 0.0
