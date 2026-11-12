"""Ejecución real vía ccxt. Solo spot, solo largo. Nunca reintenta la creación de una orden
(podría duplicarla); solo reintenta lecturas. Siempre concilia con la orden y el balance reales.
"""
import logging
import time
import uuid
from collections.abc import Mapping

import ccxt

from newsbot.execution.paper import Fill
from newsbot.features.market import with_retry

log = logging.getLogger(__name__)
FINAL = ("closed", "canceled", "rejected", "expired")


def assert_live_allowed(cfg: dict, env: Mapping[str, str]) -> dict[str, str]:
    """Las tres llaves del modo live: mode="live", CONFIRM_LIVE=yes y claves en el entorno.

    Si falta cualquiera, aborta. Devuelve las credenciales para ccxt (nunca se loguean).
    """
    prefix = cfg["general"]["exchange"].upper()
    key, secret = env.get(f"{prefix}_API_KEY"), env.get(f"{prefix}_API_SECRET")
    missing = [name for name, ok in [
        ('mode = "live" en config.toml', cfg["general"]["mode"] == "live"),
        ("CONFIRM_LIVE=yes", env.get("CONFIRM_LIVE") == "yes"),
        (f"{prefix}_API_KEY y {prefix}_API_SECRET", bool(key and secret)),
    ] if not ok]
    if missing:
        raise SystemExit("Modo live bloqueado. Falta: " + ", ".join(missing))
    return {"apiKey": key, "secret": secret}  # type: ignore[dict-item]


def client_order_id(seed: str, side: str, tag: str) -> str:
    """Determinista a partir de la fecha: un reinicio genera el mismo id (Bitvavo exige UUID)."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"newsbot:{seed}:{side}:{tag}"))


class LiveBroker:
    """Misma interfaz que PaperBroker (cash, qty, avg_entry, buy, sell)."""

    def __init__(self, ex, symbol: str, cfg: dict, seed: str, avg_entry: float = 0.0,
                 sleep=time.sleep, clock=time.monotonic):
        self.ex, self.symbol, self.seed = ex, symbol, seed
        self.sleep, self.clock = sleep, clock
        with_retry(ex.load_markets)
        m = ex.market(symbol)
        self.base, self.quote, self.limits = m["base"], m["quote"], m["limits"]
        self.fee_pct = cfg["costs"]["fee_pct"]
        self.timeout_s = cfg["execution"]["limit_timeout_min"] * 60
        self.avg_entry = avg_entry
        # ccxt no tiene un método unificado para leer los permisos de una API key y Bitvavo no los
        # expone en ccxt: no se puede verificar aquí que la key no permita retiradas.
        log.warning("No se puede verificar por API que la key no tenga permiso de retirada: "
                    "compruébalo en el panel del exchange (solo 'view' y 'trade').")
        self.order_type = self._choose_order_type(cfg)
        self.refresh()

    def _choose_order_type(self, cfg: dict) -> str:
        try:
            fees = with_retry(self.ex.fetch_trading_fees).get(self.symbol, {})
        except Exception as exc:
            log.warning("No pude leer las comisiones de la cuenta (%s): uso orden de mercado", exc)
            return "market"
        maker, taker = fees.get("maker"), fees.get("taker")
        if taker is not None and taker > self.fee_pct:
            log.warning("Comisión taker real %.4f > costs.fee_pct %.4f: el paper subestima costes",
                        taker, self.fee_pct)
        if cfg["execution"]["order_type"] == "market":
            return "market"
        if maker is None or taker is None or maker >= taker:
            log.info("Maker (%s) no es más barata que taker (%s): uso orden de mercado", maker, taker)
            return "market"
        return "limit_then_market"

    def refresh(self) -> None:
        bal = with_retry(self.ex.fetch_balance)
        self.cash = float((bal.get(self.quote) or {}).get("free") or 0.0)
        self.qty = float((bal.get(self.base) or {}).get("total") or 0.0)

    # --- órdenes ---------------------------------------------------------------------------
    def _amount(self, amount: float) -> float:
        try:
            return float(self.ex.amount_to_precision(self.symbol, amount))
        except ccxt.InvalidOrder:  # por debajo de la precisión mínima
            return 0.0

    def _tradeable(self, amount: float, price: float) -> bool:
        amin = (self.limits.get("amount") or {}).get("min") or 0.0
        cmin = (self.limits.get("cost") or {}).get("min") or 0.0
        return amount > 0 and amount >= amin and amount * price >= cmin

    def _fetch(self, order: dict) -> dict:
        return with_retry(self.ex.fetch_order, order["id"], self.symbol)

    def _wait(self, order: dict) -> dict:
        deadline = self.clock() + self.timeout_s
        while True:
            o = self._fetch(order)
            if o.get("status") in FINAL:
                return o
            if self.clock() >= deadline:
                try:
                    self.ex.cancel_order(order["id"], self.symbol)
                except ccxt.OrderNotFound:
                    pass  # se llenó justo al cancelar
                return self._fetch(order)
            self.sleep(30)

    def _market(self, side: str, amount: float, price: float) -> dict | None:
        params = {"clientOrderId": client_order_id(self.seed, side, "mkt")}
        if side == "buy":
            cost = amount * price
            if self.ex.has.get("createMarketBuyOrderWithCost"):
                o = self.ex.create_market_buy_order_with_cost(self.symbol, cost, params)
            elif self.ex.has.get("createMarketOrderWithCost"):
                o = self.ex.create_market_order_with_cost(self.symbol, "buy", cost, params)
            else:
                o = self.ex.create_order(self.symbol, "market", "buy", self._amount(amount), None,
                                         params)
        else:
            o = self.ex.create_order(self.symbol, "market", "sell", self._amount(amount), None,
                                     params)
        return self._fetch(o)

    def _execute(self, side: str, amount: float, price: float) -> list[dict]:
        orders: list[dict] = []
        if self.order_type == "limit_then_market":
            # idea: post-only al mejor precio; si no se llena en N minutos, se cancela y va a mercado
            book = with_retry(self.ex.fetch_order_book, self.symbol, 5)
            px = (book["bids"] if side == "buy" else book["asks"])[0][0]
            amt = self._amount(amount * price / px if side == "buy" else amount)
            if self._tradeable(amt, px):
                o = self.ex.create_order(self.symbol, "limit", side, amt, px, {
                    "postOnly": True, "clientOrderId": client_order_id(self.seed, side, "lim")})
                o = self._wait(o)
                orders.append(o)
                amount -= float(o.get("filled") or 0.0)
        if self._tradeable(self._amount(amount), price):
            o = self._market(side, amount, price)
            if o:
                orders.append(o)
        return orders

    def _fill(self, side: str, orders: list[dict]) -> Fill | None:
        qty = sum(float(o.get("filled") or 0.0) for o in orders)
        if qty <= 0:
            return None
        cost = sum(float(o.get("cost") or (o.get("filled") or 0) * (o.get("average") or 0))
                   for o in orders)
        fee = 0.0
        for o in orders:
            for f in o.get("fees") or [o.get("fee")]:
                if f and f.get("cost"):
                    fee += float(f["cost"]) * (float(o.get("average") or 0)
                                               if f.get("currency") == self.base else 1.0)
        return Fill(side, qty, cost / qty, fee, cost)

    def buy(self, eur: float, price: float) -> Fill | None:
        eur = min(eur, self.cash / (1 + self.fee_pct))
        qty_before = self.qty
        fill = self._fill("buy", self._execute("buy", eur / price, price))
        if fill:
            self.avg_entry = (self.avg_entry * qty_before + fill.eur) / (qty_before + fill.qty)
        self.refresh()
        return fill

    def sell(self, qty: float, price: float) -> Fill | None:
        fill = self._fill("sell", self._execute("sell", min(qty, self.qty), price))
        self.refresh()
        if not self._tradeable(self.qty, price):  # solo queda polvo: la posición está cerrada
            self.avg_entry = 0.0
        return fill

    def find(self, since_ms: int) -> Fill | None:
        """Concilia una orden que quedó 'pending' (p. ej. el proceso murió tras enviarla)."""
        ids = {client_order_id(self.seed, side, tag) for side in ("buy", "sell")
               for tag in ("lim", "mkt")}
        orders = [o for o in with_retry(self.ex.fetch_orders, self.symbol, since_ms)
                  if o.get("clientOrderId") in ids]
        return self._fill(orders[0]["side"], orders) if orders else None
