"""Broker simulado de la cartera de bolsa: la misma interfaz que execution.trading212.T212Broker.

El efectivo vive en el KV "paper:cash" (al empezar, general.initial_capital); las posiciones son las
de la tabla positions, que escribe el motor tras cada ejecución (con cualquiera de los dos brokers).
"""
import itertools
import sqlite3

from newsbot.execution.paper import Fill
from newsbot.storage import db

_STOP_IDS = itertools.count(1)


class StocksPaperBroker:
    def __init__(self, cfg: dict, con: sqlite3.Connection):
        self.cfg, self.con, self.kv = cfg, con, db.KV(con)
        self.fee_pct, self.slippage_pct = cfg["costs"]["fee_pct"], cfg["costs"]["slippage_pct"]

    @property
    def cash(self) -> float:
        return float(self.kv.get("paper:cash") or self.cfg["general"]["initial_capital"])

    def _set_cash(self, value: float) -> None:
        self.kv.set("paper:cash", repr(max(0.0, value)))

    def _held(self, symbol: str) -> float:
        row = self.con.execute("SELECT qty FROM positions WHERE symbol = ?", (symbol,)).fetchone()
        return float(row["qty"]) if row else 0.0

    def snapshot(self) -> tuple[float, dict[str, dict]]:
        """(efectivo, {símbolo: {"qty", "avg", "price"}}); en papel no hay precio propio (None)."""
        return self.cash, {r["symbol"]: {"qty": r["qty"], "avg": r["avg_entry"] or 0.0, "price": None}
                           for r in db.open_positions(self.con)}

    def buy(self, symbol: str, eur: float, price: float) -> Fill | None:
        eur = min(eur, self.cash / (1 + self.fee_pct))
        if eur <= 0 or price <= 0:
            return None
        px = price * (1 + self.slippage_pct)
        fee = eur * self.fee_pct
        self._set_cash(self.cash - eur - fee)
        return Fill("buy", eur / px, px, fee, eur)

    def sell(self, symbol: str, qty: float, price: float) -> Fill | None:
        q = min(qty, self._held(symbol))
        if q <= 0 or price <= 0:
            return None
        px = price * (1 - self.slippage_pct)
        gross = q * px
        fee = gross * self.fee_pct
        self._set_cash(self.cash + gross - fee)
        return Fill("sell", q, px, fee, gross)

    def place_stop(self, symbol: str, qty: float, stop_price: float) -> str | None:
        """En papel el stop lo vigila el propio motor (propuesta urgente): solo devuelve un id."""
        return f"paper-stop-{next(_STOP_IDS)}" if qty > 0 and stop_price > 0 else None

    def cancel_stop(self, order_id: str) -> None:
        return None
