from dataclasses import dataclass

from newsbot.strategy.fusion import Decision


@dataclass(frozen=True)
class Fill:
    side: str
    qty: float
    price: float
    fee: float
    eur: float  # importe bruto de la operación


class PaperBroker:
    """Simula comisión y slippage con los mismos números que se usarán en live."""

    def __init__(self, fee_pct: float, slippage_pct: float, cash: float, qty: float = 0.0,
                 avg_entry: float = 0.0):
        self.fee_pct, self.slippage_pct = fee_pct, slippage_pct
        self.cash, self.qty, self.avg_entry = cash, qty, avg_entry

    def buy(self, eur: float, price: float) -> Fill:
        eur = min(eur, self.cash / (1 + self.fee_pct))
        px = price * (1 + self.slippage_pct)
        q, fee = eur / px, eur * self.fee_pct
        self.avg_entry = (self.avg_entry * self.qty + eur) / (self.qty + q)
        self.qty += q
        self.cash -= eur + fee
        return Fill("buy", q, px, fee, eur)

    def sell(self, qty: float, price: float) -> Fill:
        q = min(qty, self.qty)
        px = price * (1 - self.slippage_pct)
        gross = q * px
        fee = gross * self.fee_pct
        self.qty -= q
        self.cash += gross - fee
        if self.qty <= 1e-12:
            self.qty, self.avg_entry = 0.0, 0.0
        return Fill("sell", q, px, fee, gross)


def execute(broker, d: Decision, price: float) -> Fill | None:
    """broker: PaperBroker o LiveBroker (misma interfaz)."""
    if d.action == "BUY":
        return broker.buy(d.order_eur, price)
    if d.action == "SELL":
        return broker.sell(broker.qty if d.target_exposure == 0 else -d.order_eur / price, price)
    return None
