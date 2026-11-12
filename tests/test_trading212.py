"""Broker de Trading 212 sin red: transporte HTTP simulado y reloj falso (los sleeps adelantan el reloj)."""
import base64
import json
import logging
import tomllib
import urllib.error
import urllib.parse
from datetime import UTC, datetime
from pathlib import Path

import ccxt
import pytest

from newsbot.execution.paper import Fill
from newsbot.execution.trading212 import (
    API,
    T212Broker,
    T212Client,
    T212Error,
    credentials,
    quantity_decimals,
    round_qty,
)

ROOT = Path(__file__).resolve().parents[1]
T0 = 1_800_000_000.0
QTY = 0.01483  # round_qty(15 € × 0.99 / 1001, 1001)
CFG = {"general": {"symbols": ["RHM/EUR", "GOLD/EUR"]},
       "market": {"RHM": {"ticker": "RHM.DE", "t212": "RHMd_EQ"},
                  "GOLD": {"ticker": "4GLD.DE", "t212": "4GLDd_EQ"}}}
PRECISION = {"type": "/api-errors/quantity-precision-mismatch", "title": "Error while placing the order",
             "status": 400, "detail": "invalid quantity precision 4"}
NOT_OWNED = {"code": "SellingEquityNotOwned", "clarification": "SellingEquityNotOwned"}
INSUFFICIENT = {"code": "UNDEFINED", "clarification": "InsufficientFreeForStocksException"}
INVALID = {"type": "/api-errors/invalid-request", "title": "The request is invalid",
           "detail": "Invalid payload"}
GONE = (404, {"type": "/api-errors/entity-not-found", "title": "Requested entity not found"})


class Clock:
    def __init__(self):
        self.t, self.slept = T0, []

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.slept.append(s)
        self.t += s


class Server:
    """Transporte simulado. Respuestas por (método, ruta) en orden y la última se repite. Cada una es
    (status, cuerpo[, cabeceras]), una excepción (error de red) o un callable que devuelve una de ellas."""

    def __init__(self, clock):
        self.clock, self.routes, self.calls = clock, {}, []

    def on(self, method, path, *responses):
        self.routes[(method, path)] = list(responses)
        return self

    def __call__(self, method, url, headers, body, timeout):
        u = urllib.parse.urlsplit(url)
        path = u.path.removeprefix(API)
        self.calls.append({"method": method, "path": path, "host": f"{u.scheme}://{u.netloc}",
                           "query": dict(urllib.parse.parse_qsl(u.query)), "headers": headers,
                           "body": json.loads(body) if body else None, "t": self.clock()})
        queue = self.routes.get((method, path))
        assert queue, f"llamada inesperada: {method} {path}"
        r = queue.pop(0) if len(queue) > 1 else queue[0]
        r = r() if callable(r) else r
        if isinstance(r, BaseException):
            raise r
        status, payload, *rest = r
        raw = (payload.encode() if isinstance(payload, str) else b"" if payload is None
               else json.dumps(payload).encode())
        return status, (rest[0] if rest else {}), raw

    def made(self, method, path=None):
        return [c for c in self.calls if c["method"] == method and (path is None or c["path"] == path)]


def make(env="live"):
    clock = Clock()
    srv = Server(clock)
    return T212Client("KEY123", "SECRET456", env=env, http=srv, clock=clock, sleep=clock.sleep), srv, clock


def broker(cfg=CFG):
    client, srv, clock = make()
    return T212Broker(cfg, client), srv, clock


def iso(t):
    return datetime.fromtimestamp(t, UTC).isoformat().replace("+00:00", "Z")


def order(oid, qty, status="NEW", filled=0.0, ticker="RHMd_EQ", t=T0, kind="MARKET", **kw):
    return {"id": oid, "ticker": ticker, "type": kind, "quantity": qty, "filledQuantity": filled,
            "status": status, "side": "BUY" if qty > 0 else "SELL", "initiatedFrom": "API",
            "createdAt": iso(t), "strategy": "QUANTITY", **kw}


def hist(o, qty=None, price=None, net=None, taxes=()):
    item = {"order": o}
    if qty:
        item["fill"] = {"id": o["id"] * 10, "quantity": qty, "price": price, "type": "TRADE",
                        "walletImpact": {"currency": "EUR", "netValue": net,
                                         "taxes": [{"name": n, "quantity": q, "currency": "EUR"}
                                                   for n, q in taxes]}}
    return item


def page(*items):
    return (200, {"items": list(items), "nextPagePath": None})


def pos(qty, avail=None, ticker="RHMd_EQ", avg=1000.0, price=1010.0):
    return {"instrument": {"ticker": ticker, "currency": "EUR"}, "quantity": qty,
            "quantityAvailableForTrading": qty if avail is None else avail,
            "averagePricePaid": avg, "currentPrice": price}


def qtys(srv, path="/orders/market"):
    return [c["body"]["quantity"] for c in srv.made("POST", path)]


# --- cliente -------------------------------------------------------------------------------------
def test_basic_auth_hosts_and_key_never_shown():
    client, srv, _ = make()
    srv.on("GET", "/account/summary", (200, {"cash": {"availableToTrade": 50.0}}))
    assert client.summary()["cash"]["availableToTrade"] == 50.0
    c = srv.calls[0]
    assert c["host"] == "https://live.trading212.com" and c["path"] == "/account/summary"
    assert c["headers"]["Authorization"] == "Basic " + base64.b64encode(b"KEY123:SECRET456").decode()
    assert "KEY123" not in repr(client) and "SECRET456" not in repr(client)
    demo, srv2, _ = make("demo")
    srv2.on("GET", "/account/summary", (200, {}))
    demo.summary()
    assert srv2.calls[0]["host"] == "https://demo.trading212.com"
    with pytest.raises(ValueError):
        T212Client("k", "s", env="paper")


def test_minimum_spacing_per_endpoint():
    client, srv, _ = make()
    srv.on("GET", "/account/summary", (200, {})).on("GET", "/positions", (200, []))
    srv.on("GET", "/history/orders", page())
    client.summary()
    client.positions()  # otro cubo: no espera al de summary
    client.summary()    # 1 cada 5 s → 5,5 s
    client.positions()
    client.history("RHMd_EQ")
    client.history("RHMd_EQ")  # 20 por minuto → 3,3 s
    assert [c["t"] - T0 for c in srv.calls] == pytest.approx([0, 0, 5.5, 5.5, 5.5, 8.8])


@pytest.mark.parametrize("scale", [1, 1000])  # el reset puede venir en segundos o en milisegundos
def test_ratelimit_remaining_zero_waits_until_reset(scale):
    client, srv, _ = make()
    headers = {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": str(int((T0 + 30) * scale))}
    srv.on("GET", "/positions", (200, [], headers), (200, []))
    client.positions()
    client.positions()
    assert srv.calls[1]["t"] == pytest.approx(T0 + 30)


def test_429_get_waits_for_reset_and_retries_but_post_never_retries():
    client, srv, _ = make()
    srv.on("GET", "/account/summary", (429, "", {"x-ratelimit-reset": str(T0 + 20)}), (200, {"cash": {}}))
    assert client.summary() == {"cash": {}}
    assert [c["t"] for c in srv.calls] == pytest.approx([T0, T0 + 20])
    srv.on("POST", "/orders/market", (429, "", {"x-ratelimit-reset": str(T0 + 50)}), (200, {"id": 1}))
    with pytest.raises(T212Error) as e:
        client.market_order("RHMd_EQ", 0.01)
    assert e.value.kind == "rate" and len(srv.made("POST")) == 1
    client.market_order("RHMd_EQ", 0.01)  # el siguiente POST respeta el reset
    assert srv.made("POST")[1]["t"] == pytest.approx(T0 + 50)


def test_429_with_reset_too_far_gives_up():
    client, srv, clock = make()
    srv.on("GET", "/positions", (429, "", {"x-ratelimit-reset": str(T0 + 600)}))
    with pytest.raises(T212Error) as e:
        client.positions()
    assert e.value.kind == "rate" and len(srv.calls) == 1 and clock.t < T0 + 600
    with pytest.raises(T212Error) as e:  # ni siquiera se envía: no bloquea el hilo 10 minutos
        client.positions()
    assert e.value.kind == "rate" and len(srv.calls) == 1 and clock.t < T0 + 600


def test_get_network_errors_retry_with_backoff():
    client, srv, clock = make()
    srv.on("GET", "/positions", urllib.error.URLError("down"), TimeoutError("slow"), (200, [pos(1)]))
    assert client.positions() == [pos(1)]
    assert len(srv.calls) == 3 and 1.0 in clock.slept and 2.0 in clock.slept
    srv.on("GET", "/positions", ConnectionResetError("reset"))
    with pytest.raises(T212Error) as e:
        client.positions()
    assert e.value.kind == "network" and e.value.status == 0


@pytest.mark.parametrize("status, body, kind, code", [
    (401, "\n", "auth", ""),
    (403, {"type": "/api-errors/not-available-in-real-money-account",
           "title": "The action is not allowed in real money account", "status": 403},
     "auth", "/api-errors/not-available-in-real-money-account"),
    (400, INSUFFICIENT, "insufficient", "UNDEFINED"),
    (400, {"code": "InsufficientFreeForStocksBuy"}, "insufficient", "InsufficientFreeForStocksBuy"),
    (400, PRECISION, "precision", "/api-errors/quantity-precision-mismatch"),
    (400, {"success": False, "error": PRECISION}, "precision", "/api-errors/quantity-precision-mismatch"),
    (400, NOT_OWNED, "not_owned", "SellingEquityNotOwned"),
    (400, {"code": "MarketClosed"}, "market_closed", "MarketClosed"),
    (404, {"type": "/api-errors/entity-not-found", "detail": "Ticker does not exist"}, "not_found",
     "/api-errors/entity-not-found"),
    (400, {"type": "/api-errors/entity-not-found", "detail": "Ticker does not exist"}, "not_found",
     "/api-errors/entity-not-found"),
    (429, "", "rate", ""),
    (400, INVALID, "invalid", "/api-errors/invalid-request"),
    (408, "", "network", ""),
    (502, "<html>bad gateway</html>", "network", ""),
    (200, "<html>cortada", "network", "bad_json"),
    (418, "teapot", "unknown", ""),
])
def test_error_kinds(status, body, kind, code):
    client, srv, _ = make()
    srv.on("POST", "/orders/market", (status, body))
    with pytest.raises(T212Error) as e:
        client.market_order("RHMd_EQ", 0.5)
    assert (e.value.status, e.value.kind, e.value.code) == (status, kind, code)
    assert len(srv.calls) == 1  # un POST nunca se reintenta
    if kind == "precision":
        assert e.value.detail == "invalid quantity precision 4"


def test_post_network_error_is_not_retried():
    client, srv, _ = make()
    srv.on("POST", "/orders/market", urllib.error.URLError("timed out"))
    with pytest.raises(T212Error) as e:
        client.market_order("RHMd_EQ", 0.5)
    assert e.value.kind == "network" and len(srv.calls) == 1


def test_order_is_none_once_not_pending_and_history_query():
    client, srv, _ = make()
    srv.on("GET", "/orders/5", GONE)
    assert client.order(5) is None
    srv.on("GET", "/history/orders", page(hist(order(5, 1.0, "FILLED", 1.0), 1.0, 10.0, 10.0)))
    assert client.history("RHMd_EQ", limit=500)[0]["order"]["id"] == 5
    assert srv.calls[-1]["query"] == {"ticker": "RHMd_EQ", "limit": "50"}


@pytest.mark.parametrize("price, d", [(0.05, 0), (0.1, 1), (0.5, 1), (1, 2), (9.99, 2), (10, 3), (99.9, 3),
                                      (100, 4), (999, 4), (1000, 5), (9999, 5), (10_000, 6), (99_999, 6),
                                      (100_000, 7), (1e6, 7)])
def test_quantity_decimals(price, d):
    assert quantity_decimals(price) == d


@pytest.mark.parametrize("qty, price, out", [
    (15 * 0.99 / 1001, 1001, QTY), (0.015, 1001, 0.015), (0.1 + 0.2, 50, 0.3), (0.0149999, 500, 0.0149),
    (2.999, 0.05, 2.0), (5.5559, 50, 5.555), (0.0, 100, 0.0), (-1.0, 100, 0.0)])
def test_round_qty_always_rounds_down(qty, price, out):
    assert round_qty(qty, price) == out
    assert round_qty(qty, price) <= max(qty, 0.0)


def test_credentials():
    assert credentials({"TRADING212_API_KEY": " k ", "TRADING212_API_SECRET": "s\n"}) == ("k", "s")
    with pytest.raises(SystemExit, match="TRADING212_API_KEY"):
        credentials({})
    with pytest.raises(SystemExit):
        credentials({"TRADING212_API_KEY": "k", "TRADING212_API_SECRET": "  "})


# --- broker --------------------------------------------------------------------------------------
def test_mapping_covers_config_bolsa():
    cfg = tomllib.loads((ROOT / "config_bolsa.toml").read_text(encoding="utf-8"))
    cfg["general"]["symbols"] = [f"{c}/EUR" for c in cfg["market"]]  # todo el universo (el real: solo WMT)
    b = T212Broker(cfg, make()[0])
    assert set(b.tickers) == set(cfg["general"]["symbols"])
    assert b.tickers["TTWO/EUR"] == "TKEd_EQ" and b.symbols["SMHm_EQ"] == "SEMI/EUR"
    with pytest.raises(ValueError, match="t212"):
        T212Broker(CFG, make()[0]).buy("XYZ/EUR", 10, 10)


def test_snapshot_maps_tickers_back_to_symbols():
    b, srv, _ = broker()
    srv.on("GET", "/account/summary", (200, {"currency": "EUR", "cash": {"availableToTrade": 35.5,
                                                                         "reservedForOrders": 0.0}}))
    srv.on("GET", "/positions", (200, [pos(QTY, avg=1001.0, price=1010.0),
                                       pos(0.1, avail=0.05, ticker="4GLDd_EQ", avg=120.0, price=121.0),
                                       pos(2.0, ticker="TSLA_US_EQ")]))  # comprada a mano: fuera
    cash, positions = b.snapshot()
    assert cash == 35.5
    assert positions == {
        "RHM/EUR": {"qty": QTY, "avg": 1001.0, "price": 1010.0, "available": QTY},
        "GOLD/EUR": {"qty": 0.1, "avg": 120.0, "price": 121.0, "available": 0.05}}


def test_buy_waits_for_fill_and_reads_it_from_history():
    b, srv, _ = broker()
    srv.on("GET", "/positions", (200, []))
    srv.on("POST", "/orders/market", (200, order(77, QTY)))
    srv.on("GET", "/orders/77", (200, order(77, QTY)), GONE)  # pendiente y luego ya no (llenada)
    srv.on("GET", "/history/orders", page(
        hist(order(5, 1.0, "FILLED", 1.0), 1.0, 900.0, 900.0),
        hist(order(77, QTY, "FILLED", QTY), QTY, 1001.5, 14.85, [("FRENCH_TRANSACTION_TAX", -0.06)])))
    fill = b.buy("RHM/EUR", 15.0, 1001.0)
    assert (fill.side, fill.qty, fill.fee, fill.eur) == ("buy", QTY, 0.06, 14.85)
    assert fill.price == pytest.approx(1001.5)
    post = srv.made("POST")
    assert len(post) == 1
    assert post[0]["body"] == {"ticker": "RHMd_EQ", "quantity": QTY, "extendedHours": False}
    assert post[0]["headers"]["Content-Type"] == "application/json"
    polls = [c["t"] for c in srv.made("GET", "/orders/77")]
    assert len(polls) == 2 and polls[1] - polls[0] >= 1.2
    assert srv.made("GET", "/history/orders")[0]["query"] == {"ticker": "RHMd_EQ", "limit": "10"}


def test_partial_fill_cancels_the_rest_and_books_filled_quantity():
    b, srv, clock = broker()
    srv.on("GET", "/positions", (200, []))
    srv.on("POST", "/orders/market", (200, order(78, QTY)))
    srv.on("GET", "/orders/78", lambda: GONE if srv.made("DELETE") else
           (200, order(78, QTY, "PARTIALLY_FILLED", 0.01)))
    srv.on("DELETE", "/orders/78", (200, None))
    srv.on("GET", "/history/orders", page(hist(order(78, QTY, "CANCELLED", 0.01), 0.01, 1000.0, 10.0)))
    fill = b.buy("RHM/EUR", 15.0, 1001.0)
    assert fill == Fill("buy", 0.01, 1000.0, 0.0, 10.0)
    assert len(srv.made("POST")) == 1 and len(srv.made("DELETE")) == 1
    assert srv.made("DELETE")[0]["t"] >= T0 + 15


def test_market_closed_returns_none_with_clear_log(caplog):
    b, srv, _ = broker()
    srv.on("GET", "/positions", (200, []))
    srv.on("POST", "/orders/market", (400, {"code": "MarketClosed", "clarification": "MarketClosed"}))
    with caplog.at_level(logging.WARNING):
        assert b.buy("RHM/EUR", 15.0, 1001.0) is None
    assert "mercado cerrado" in caplog.text and len(srv.made("POST")) == 1


def test_queued_order_is_cancelled_and_not_booked():
    b, srv, _ = broker()
    srv.on("GET", "/positions", (200, []))
    srv.on("POST", "/orders/market", (200, order(79, QTY)))  # en cola hasta la apertura
    srv.on("GET", "/orders/79", lambda: GONE if srv.made("DELETE") else (200, order(79, QTY)))
    srv.on("DELETE", "/orders/79", (200, None))
    srv.on("GET", "/history/orders", page(hist(order(79, QTY, "CANCELLED", 0.0))))
    assert b.buy("RHM/EUR", 15.0, 1001.0) is None
    assert len(srv.made("POST")) == 1 and len(srv.made("DELETE")) == 1


def test_precision_error_retries_once_with_one_decimal_less():
    b, srv, _ = broker()
    srv.on("GET", "/positions", (200, []))
    srv.on("POST", "/orders/market", (400, PRECISION), (200, order(80, 0.0148)))
    srv.on("GET", "/orders/80", GONE)
    srv.on("GET", "/history/orders", page(hist(order(80, 0.0148, "FILLED", 0.0148), 0.0148, 1001.0, 14.81)))
    fill = b.buy("RHM/EUR", 15.0, 1001.0)
    assert qtys(srv) == [QTY, 0.0148] and fill.qty == 0.0148
    b, srv, _ = broker()
    srv.on("GET", "/positions", (200, []))
    srv.on("POST", "/orders/market", (400, PRECISION))
    assert b.buy("RHM/EUR", 15.0, 1001.0) is None
    assert qtys(srv) == [QTY, 0.0148]  # solo una vez


def test_insufficient_funds_shrinks_three_percent_once():
    b, srv, _ = broker()
    srv.on("GET", "/positions", (200, []))
    srv.on("POST", "/orders/market", (400, INSUFFICIENT))
    assert b.buy("RHM/EUR", 15.0, 1001.0) is None
    assert qtys(srv) == [QTY, round_qty(QTY * 0.97, 1001.0)] == [QTY, 0.01438]


def test_sell_not_owned_rereads_positions_and_sells_available_once():
    b, srv, _ = broker()
    srv.on("GET", "/positions", (200, [pos(0.02)]), (200, [pos(0.02, avail=0.015)]))
    srv.on("POST", "/orders/market", (400, NOT_OWNED), (200, order(81, -0.015)))
    srv.on("GET", "/orders/81", GONE)
    srv.on("GET", "/history/orders", page(hist(order(81, -0.015, "FILLED", 0.015), -0.015, 1010.0, 15.15)))
    fill = b.sell("RHM/EUR", 0.02, 1010.0)
    assert qtys(srv) == [-0.02, -0.015]
    assert (fill.side, fill.qty, fill.eur) == ("sell", 0.015, 15.15) and fill.price == pytest.approx(1010.0)
    b, srv, _ = broker()
    srv.on("GET", "/positions", (200, [pos(0.02)]))
    srv.on("POST", "/orders/market", (400, NOT_OWNED))
    assert b.sell("RHM/EUR", 0.02, 1010.0) is None and qtys(srv) == [-0.02]  # disponible igual: no insiste


def test_sell_caps_to_available_and_closes_small_remainders():
    b, srv, _ = broker()
    srv.on("GET", "/positions", (200, [pos(0.02)]))
    srv.on("POST", "/orders/market", (400, {"code": "Whatever"}))
    b.sell("RHM/EUR", 1.0, 1010.0)       # más de lo que hay → lo disponible
    b.sell("RHM/EUR", 0.0195, 1010.0)    # dejaría 0,50 € → la posición entera
    b.sell("RHM/EUR", 0.01, 1010.0)      # deja 10 €: parcial
    assert qtys(srv) == [-0.02, -0.02, -0.01]
    srv.on("GET", "/positions", (200, []))
    assert b.sell("RHM/EUR", 0.01, 1010.0) is None and len(srv.made("POST")) == 3


def test_network_error_after_post_reconciled_via_history_without_resending():
    b, srv, _ = broker()
    srv.on("GET", "/positions", (200, []))
    srv.on("POST", "/orders/market", TimeoutError("timed out"))
    srv.on("GET", "/orders", (200, []))  # ya no está pendiente: se llenó
    old = order(60, QTY, "FILLED", QTY, t=T0 - 3600)                 # misma cantidad, de hace una hora
    app = order(61, QTY, "FILLED", QTY, t=T0 + 1, initiatedFrom="WEB")  # puesta a mano en la web
    mine = order(62, QTY, "FILLED", QTY, t=T0 + 1)
    srv.on("GET", "/history/orders", page(hist(old, QTY, 990.0, 14.68), hist(app, QTY, 995.0, 14.76),
                                          hist(mine, QTY, 1002.0, 14.86)))
    fill = b.buy("RHM/EUR", 15.0, 1001.0)
    assert len(srv.made("POST")) == 1
    assert (fill.qty, fill.eur) == (QTY, 14.86) and fill.price == pytest.approx(1002.0)


def test_network_error_after_post_found_pending_then_followed():
    b, srv, _ = broker()
    srv.on("GET", "/positions", (200, []))
    srv.on("POST", "/orders/market", urllib.error.URLError("reset"))
    srv.on("GET", "/orders", (200, [order(63, QTY, t=T0 + 0.5)]))
    srv.on("GET", "/orders/63", GONE)
    srv.on("GET", "/history/orders", page(hist(order(63, QTY, "FILLED", QTY), QTY, 1001.0, 14.85)))
    assert b.buy("RHM/EUR", 15.0, 1001.0).qty == QTY
    assert len(srv.made("POST")) == 1


def test_network_error_after_post_without_trace_is_nofill(caplog):
    b, srv, _ = broker()
    srv.on("GET", "/positions", (200, []))
    srv.on("POST", "/orders/market", (503, "upstream"))
    srv.on("GET", "/orders", (200, []))
    srv.on("GET", "/history/orders", page())
    with caplog.at_level(logging.WARNING):
        assert b.buy("RHM/EUR", 15.0, 1001.0) is None
    assert len(srv.made("POST")) == 1 and "NO se reenvía" in caplog.text


def test_network_error_after_post_reconciled_by_position_change():
    b, srv, _ = broker()
    srv.on("GET", "/positions", (200, []), (200, [pos(QTY)]))  # antes nada; después la posición
    srv.on("POST", "/orders/market", TimeoutError("timed out"))
    srv.on("GET", "/orders", (200, []))
    srv.on("GET", "/history/orders", page())
    assert b.buy("RHM/EUR", 15.0, 1001.0) == Fill("buy", QTY, 1001.0, 0.0, QTY * 1001.0)
    assert len(srv.made("POST")) == 1


def test_auth_errors_raise_authentication_error_without_leaking_the_key(caplog):
    b, srv, _ = broker()
    srv.on("GET", "/positions", (200, []))
    srv.on("POST", "/orders/market", (401, "\n"))
    with caplog.at_level(logging.DEBUG), pytest.raises(ccxt.AuthenticationError) as e:
        b.buy("RHM/EUR", 15.0, 1001.0)
    assert len(srv.made("POST")) == 1
    srv.on("GET", "/account/summary", (403, {"type": "/api-errors/not-available-in-real-money-account",
                                             "status": 403}))
    with pytest.raises(ccxt.AuthenticationError):
        b.snapshot()
    srv.on("POST", "/orders/stop", (403, ""))
    with pytest.raises(ccxt.AuthenticationError):
        b.place_stop("RHM/EUR", QTY, 920.0)
    for text in (caplog.text, str(e.value)):
        assert "SECRET456" not in text and "KEY123" not in text


def test_place_and_cancel_stop():
    b, srv, _ = broker()
    srv.on("POST", "/orders/stop", (200, order(90, -QTY, kind="STOP", stopPrice=921.1)))
    assert b.place_stop("RHM/EUR", QTY, 921.1049) == "90"
    assert srv.made("POST")[0]["body"] == {"ticker": "RHMd_EQ", "quantity": -QTY, "stopPrice": 921.1,
                                           "timeValidity": "GOOD_TILL_CANCEL"}
    srv.on("DELETE", "/orders/90", (200, None))
    srv.on("GET", "/orders/90", GONE)
    srv.on("GET", "/orders", (200, []), (200, []))  # comprobación: ya no está entre las pendientes
    assert b.cancel_stop("90") is None and len(srv.made("DELETE", "/orders/90")) == 1
    srv.on("DELETE", "/orders/91", GONE)  # ya saltó o se canceló: nada que hacer
    b.cancel_stop("91")
    n = len(srv.calls)
    b.cancel_stop(None)
    assert len(srv.calls) == n


def test_place_stop_failures():
    b, srv, _ = broker()
    srv.on("POST", "/orders/stop", (400, INVALID))
    assert b.place_stop("RHM/EUR", QTY, 921.1) is None and len(srv.made("POST")) == 1
    b, srv, _ = broker()
    srv.on("POST", "/orders/stop", (400, PRECISION), (200, {"id": 92}))
    assert b.place_stop("RHM/EUR", QTY, 921.1) == "92" and qtys(srv, "/orders/stop") == [-QTY, -0.0148]


def test_place_stop_network_error_is_reconciled_never_resent():
    b, srv, _ = broker()
    srv.on("POST", "/orders/stop", TimeoutError("timed out"))
    srv.on("GET", "/orders", (200, [order(93, -QTY, kind="MARKET", t=T0 + 1),
                                    order(94, -QTY, kind="STOP", stopPrice=921.1, t=T0 + 1)]))
    assert b.place_stop("RHM/EUR", QTY, 921.1) == "94"
    srv.on("GET", "/orders", (200, []))
    assert b.place_stop("RHM/EUR", QTY, 921.1) is None
    assert len(srv.made("POST")) == 2  # uno por llamada


def test_buy_below_minimum_quantity_sends_nothing():
    b, srv, _ = broker()
    assert b.buy("RHM/EUR", 0.005, 1001.0) is None and srv.calls == []


def test_cancel_stop_raises_when_not_confirmed():
    """Sin confirmar que el stop se canceló no se vende: podría seguir reservando las acciones."""
    b, srv, _ = broker()
    srv.on("DELETE", "/orders/93", (200, None))
    srv.on("GET", "/orders/93", GONE)
    srv.on("GET", "/orders", (200, [order(93, -QTY, kind="STOP", stopPrice=900.0)]))
    with pytest.raises(T212Error):
        b.cancel_stop("93")


def test_usd_instrument_converts_prices_and_stop():
    """Línea en dólares (NVDA_US_EQ) desde cuenta en EUR: el bot en euros, Trading 212 en dólares."""
    from newsbot.execution.trading212 import _make_fill
    cfg = {"general": {"symbols": ["NVDA/EUR"]},
           "market": {"NVDA": {"t212": "NVDA_US_EQ", "currency": "USD"}}}
    c, srv, _ = make()
    b = T212Broker(cfg, c, fx=lambda cur: 1.10)
    srv.on("GET", "/positions", (200, [{"instrument": {"ticker": "NVDA_US_EQ"}, "quantity": 0.1,
                                        "quantityAvailableForTrading": 0.1, "averagePricePaid": 198.0,
                                        "currentPrice": 220.0, "walletImpact": {"totalCost": 18.0,
                                                                                "currentValue": 20.0}}]))
    srv.on("GET", "/account/summary", (200, {"currency": "EUR", "cash": {"availableToTrade": 30.0}}))
    cash, held = b.snapshot()
    pos = held["NVDA/EUR"]
    assert pos["avg"] == pytest.approx(180.0) and pos["price"] == pytest.approx(200.0)  # en EUR
    srv.on("POST", "/orders/stop", (200, order(5, -0.1, kind="STOP", stopPrice=198.0)))
    b.place_stop("NVDA/EUR", 0.1, 180.0)  # 180 € → 198 $
    assert srv.made("POST")[0]["body"]["stopPrice"] == 198.0
    wi = {"netValue": -20.03, "taxes": [{"quantity": -0.03}]}
    f = _make_fill("buy", [{"quantity": 0.1, "price": 220.0, "walletImpact": wi}], 1.10)
    assert f.price == pytest.approx(200.0) and f.eur == pytest.approx(20.03) and f.fee == pytest.approx(0.03)


def test_closed_sell_reads_the_real_sale_from_history():
    b, srv, clock = broker()
    stop = order(91, -0.01, "FILLED", 0.01, kind="STOP", t=T0 + 100)
    old = order(90, -0.02, "FILLED", 0.02, t=T0 - 1000)  # de antes de abrir la posición
    srv.on("GET", "/history/orders", page(hist(stop, -0.01, 950.0, 9.5), hist(old, -0.02, 900.0, 18.0)))
    fill, kind = b.closed_sell("RHM/EUR", since=T0)
    assert kind == "stop" and fill.qty == 0.01 and fill.price == 950.0 and fill.eur == 9.5
    manual = order(92, -0.005, "FILLED", 0.005, t=T0 + 200, initiatedFrom="ANDROID")
    part = order(93, -0.005, "FILLED", 0.005, t=T0 + 50)  # venta parcial del bot, ya contada
    srv.on("GET", "/history/orders", page(hist(manual, -0.005, 1000.0, 5.0), hist(part, -0.005, 990.0, 4.95)))
    fill, kind = b.closed_sell("RHM/EUR", since=T0, qty=0.005)
    assert kind == "manual" and fill.qty == 0.005 and fill.price == 1000.0
    srv.on("GET", "/history/orders", page(hist(order(94, 0.01, "FILLED", 0.01, t=T0 + 10), 0.01, 1.0, 1.0)))
    assert b.closed_sell("RHM/EUR", since=T0) is None  # solo compras: no hay venta
