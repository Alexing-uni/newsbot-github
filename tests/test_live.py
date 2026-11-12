"""Modo live: la triple llave, LiveBroker contra un exchange simulado y run_once en live."""
import math
import sqlite3
import uuid
from contextlib import closing
from pathlib import Path

import pytest
from conftest import NOW, FakeExchange, uptrend

from newsbot import bot
from newsbot.bot import run_once
from newsbot.execution.live import LiveBroker, assert_live_allowed, client_order_id

LIVE_ENV = {"CONFIRM_LIVE": "yes", "BITVAVO_API_KEY": "k", "BITVAVO_API_SECRET": "s"}


class FakeLive(FakeExchange):
    """Exchange simulado con balance, órdenes y libro. Precio de mercado 70 000 €."""

    def __init__(self, maker=0.0015, taker=0.0025, fill_limit=True, eur=10.0, btc=0.0,
                 fee_currency="EUR", closes=None):
        super().__init__(closes or uptrend())
        self.has = {"createMarketOrderWithCost": True}
        self.fees = {"maker": maker, "taker": taker}
        self.fill_limit, self.fee_currency = fill_limit, fee_currency
        self.bal = {"EUR": eur, "BTC": btc}
        self.orders: dict[str, dict] = {}
        self.calls: list[tuple] = []

    def fetch_trading_fees(self):
        return {"BTC/EUR": self.fees}

    def fetch_balance(self):
        return {k: {"free": v, "total": v} for k, v in self.bal.items()}

    def amount_to_precision(self, symbol, amount):
        return f"{math.floor(amount * 1e8) / 1e8:.8f}"

    def fetch_order_book(self, symbol, limit=None):
        return {"bids": [[69_990.0, 1.0]], "asks": [[70_010.0, 1.0]]}

    def _new(self, type_, side, amount, price, params, filled):
        oid = str(len(self.orders) + 1)
        cost = filled * price
        fee = cost * self.fees["taker" if type_ == "market" else "maker"]
        if self.fee_currency == "BTC":
            fee_obj = {"cost": fee / price, "currency": "BTC"}
        else:
            fee_obj = {"cost": fee, "currency": "EUR"}
        sign = 1 if side == "buy" else -1
        self.bal["BTC"] += sign * filled
        self.bal["EUR"] -= sign * cost + (fee if self.fee_currency == "EUR" else 0)
        self.orders[oid] = {"id": oid, "side": side, "type": type_, "amount": amount,
                            "filled": filled, "average": price if filled else None,
                            "cost": cost, "fee": fee_obj,
                            "status": "closed" if filled >= amount else "open",
                            "clientOrderId": params.get("clientOrderId")}
        self.calls.append((type_, side, amount, price, dict(params)))
        return {"id": oid}

    def create_order(self, symbol, type_, side, amount, price=None, params=None):
        params = params or {}
        if type_ == "limit":
            return self._new(type_, side, amount, price, params, amount if self.fill_limit else 0.0)
        return self._new(type_, side, amount, 70_000.0, params, amount)

    def create_market_order_with_cost(self, symbol, side, cost, params=None):
        return self._new("market", side, cost / 70_000, 70_000.0, params or {}, cost / 70_000)

    def fetch_order(self, oid, symbol):
        return dict(self.orders[oid])

    def cancel_order(self, oid, symbol):
        self.orders[oid]["status"] = "canceled"

    def fetch_orders(self, symbol, since=None):
        return list(self.orders.values())


def broker(cfg, ex, **kw):
    ticks = iter(range(0, 10_000, 600))  # cada consulta avanza 10 min
    return LiveBroker(ex, "BTC/EUR", cfg, "2026-11-02", sleep=lambda s: None,
                      clock=lambda: next(ticks), **kw)


# --- triple llave -----------------------------------------------------------------------------
@pytest.mark.parametrize("mode,env", [
    ("paper", LIVE_ENV),
    ("live", {"BITVAVO_API_KEY": "k", "BITVAVO_API_SECRET": "s"}),
    ("live", {"CONFIRM_LIVE": "yes", "BITVAVO_API_KEY": "k"}),
])
def test_live_gate_aborts_if_anything_missing(cfg, mode, env):
    cfg["general"]["mode"] = mode
    with pytest.raises(SystemExit, match="bloqueado"):
        assert_live_allowed(cfg, env)


def test_live_gate_passes_with_all_three(cfg):
    cfg["general"]["mode"] = "live"
    assert assert_live_allowed(cfg, LIVE_ENV) == {"apiKey": "k", "secret": "s"}


def test_client_order_id_is_deterministic_uuid():
    a = client_order_id("2026-11-02", "buy", "mkt")
    assert a == client_order_id("2026-11-02", "buy", "mkt") and uuid.UUID(a)
    assert a != client_order_id("2026-11-03", "buy", "mkt")


# --- LiveBroker -------------------------------------------------------------------------------
def test_market_order_when_maker_not_cheaper(cfg):
    ex = FakeLive(maker=0.0025, taker=0.0025)
    b = broker(cfg, ex)
    assert b.order_type == "market"
    fill = b.buy(9.0, 70_000.0)
    assert [c[0] for c in ex.calls] == ["market"]
    assert ex.calls[0][4]["clientOrderId"] == client_order_id("2026-11-02", "buy", "mkt")
    assert fill.qty == pytest.approx(9 / 70_000) and fill.fee == pytest.approx(9 * 0.0025)
    assert b.qty == pytest.approx(9 / 70_000) and b.avg_entry == pytest.approx(70_000)
    assert b.cash == pytest.approx(10 - 9 - 9 * 0.0025)  # balance real tras conciliar


def test_market_type_forced_by_config(cfg):
    cfg["execution"]["order_type"] = "market"
    assert broker(cfg, FakeLive()).order_type == "market"


def test_fee_lookup_failure_falls_back_to_market(cfg):
    ex = FakeLive()

    def boom():
        raise RuntimeError("401")

    ex.fetch_trading_fees = boom
    assert broker(cfg, ex).order_type == "market"


def test_post_only_limit_fills(cfg):
    ex = FakeLive()
    b = broker(cfg, ex)
    assert b.order_type == "limit_then_market"
    fill = b.buy(9.0, 70_000.0)
    assert [c[0] for c in ex.calls] == ["limit"]
    assert ex.calls[0][3] == 69_990.0 and ex.calls[0][4]["postOnly"] is True
    assert fill.price == pytest.approx(69_990.0)


def test_limit_timeout_cancels_then_market(cfg):
    ex = FakeLive(fill_limit=False)
    fill = broker(cfg, ex).buy(9.0, 70_000.0)
    assert [c[0] for c in ex.calls] == ["limit", "market"]
    assert ex.orders["1"]["status"] == "canceled"
    assert fill.qty > 0


def test_sell_respects_market_minimums(cfg):
    ex = FakeLive(eur=0.0, btc=0.00002)  # 1,40 € de BTC: por debajo del mínimo de 5 €
    b = broker(cfg, ex, avg_entry=60_000.0)
    assert b.sell(b.qty, 70_000.0) is None and ex.calls == []
    assert b.avg_entry == 0.0  # solo queda polvo


def test_sell_all_and_fee_in_base_currency(cfg):
    cfg["execution"]["order_type"] = "market"
    ex = FakeLive(eur=0.0, btc=0.0001, fee_currency="BTC")
    b = broker(cfg, ex, avg_entry=60_000.0)
    fill = b.sell(b.qty, 70_000.0)
    assert fill.side == "sell" and fill.eur == pytest.approx(7.0)
    assert fill.fee == pytest.approx(7.0 * 0.0025)  # convertida de BTC a EUR
    assert b.qty == pytest.approx(0.0) and b.avg_entry == 0.0


def test_find_reconciles_pending_by_client_order_id(cfg):
    cfg["execution"]["order_type"] = "market"
    ex = FakeLive()
    broker(cfg, ex).buy(9.0, 70_000.0)
    found = broker(cfg, ex).find(0)
    assert found and found.qty == pytest.approx(9 / 70_000)
    assert LiveBroker(ex, "BTC/EUR", cfg, "otro-dia").find(0) is None


# --- run_once en live -------------------------------------------------------------------------
def test_run_once_live_end_to_end(cfg, monkeypatch):
    for k, v in LIVE_ENV.items():
        monkeypatch.setenv(k, v)
    cfg["general"]["mode"] = "live"
    ex = FakeLive(closes=uptrend())
    d = run_once(cfg, ex, NOW)
    assert d.action == "BUY" and ex.options["operatorId"] == 1001
    assert ex.calls and ex.calls[0][0] == "limit"
    with closing(sqlite3.connect(Path(cfg["general"]["data_dir"]) / "newsbot.db")) as con:
        status, mode = con.execute("SELECT status, mode FROM orders").fetchone()
        qty = con.execute("SELECT qty FROM equity_daily").fetchone()[0]
    assert (status, mode) == ("filled", "live") and qty == pytest.approx(ex.bal["BTC"])


def test_live_blocked_without_confirm(cfg):
    cfg["general"]["mode"] = "live"
    with pytest.raises(SystemExit, match="bloqueado"):
        run_once(cfg, FakeLive(), NOW)


def test_reconcile_pending_orders(cfg, monkeypatch):
    for k, v in LIVE_ENV.items():
        monkeypatch.setenv(k, v)
    cfg["general"]["mode"] = "live"
    cfg["execution"]["order_type"] = "market"
    ex = FakeLive()
    broker(cfg, ex).buy(9.0, 70_000.0)  # el proceso "murió" tras enviar la orden
    data = Path(cfg["general"]["data_dir"])
    data.mkdir(parents=True)
    from newsbot.storage import db
    with closing(db.connect(data / "newsbot.db")) as con, con:
        con.execute("INSERT INTO orders(day, client_order_id, mode, side, created, status) "
                    "VALUES ('2026-11-02', '2026-11-02', 'live', 'buy', ?, 'pending')",
                    (NOW.isoformat(),))
    assert bot.reconcile(cfg, ex) == 1
    with closing(db.connect(data / "newsbot.db")) as con:
        row = con.execute("SELECT status, qty FROM orders").fetchone()
    assert row["status"] == "filled" and row["qty"] == pytest.approx(9 / 70_000)
