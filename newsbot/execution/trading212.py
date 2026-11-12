"""Broker de bolsa en Trading 212 (API pública v0, cuenta Invest en EUR). Solo urllib.

- Auth Basic base64(key:secret). Hosts live/demo. La clave nunca se loguea ni sale en los errores.
- Límites por cuenta y por endpoint: separación mínima entre llamadas de cada cubo (periodo × 1,1),
  más x-ratelimit-remaining/x-ratelimit-reset. Un 429 en GET espera al reset y reintenta; en POST no.
- Órdenes por CANTIDAD (venta = cantidad negativa), decimales según el precio, siempre hacia abajo.
- Los POST no son idempotentes y no hay id de cliente: NUNCA se reenvía una orden cuyo resultado es
  incierto (red, timeout, 408, 5xx). Se concilia con las órdenes pendientes, el historial y las
  posiciones antes de darla por no ejecutada.
- GET /orders/{id} solo ve órdenes pendientes (404 al llenarse): el fill sale del historial.
- 401/403 (clave revocada, sin permiso, IP no permitida) → ccxt.AuthenticationError: el bot deja de
  operar y avisa por Telegram.
"""
import base64
import functools
import http.client
import json
import logging
import math
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from datetime import UTC, datetime

import ccxt

from newsbot.execution.paper import Fill

log = logging.getLogger(__name__)

HOSTS = {"live": "https://live.trading212.com", "demo": "https://demo.trading212.com"}
API = "/api/v0/equity"
UA = "newsbot/0.1"
TIMEOUT = 15.0
# separación mínima entre llamadas de cada cubo = periodo del límite × 1,1 (límites por cuenta)
GAP = {"summary": 5.5, "positions": 1.1, "pending": 5.5, "order": 1.1, "market": 1.32, "stop": 2.2,
       "cancel": 1.32, "history": 3.3}
GET_TRIES = 3              # un GET se reintenta tras 429 (al reset) o error de red (1 s, 2 s)
MAX_WAIT = 65.0            # más espera que esto por un 429 → se rinde
BUY_BUFFER = 0.99          # margen para que el precio real (con retraso) no deje la compra sin fondos
SHRINK = 0.03              # fondos insuficientes → una vez con un 3 % menos
MIN_LEFT_EUR = 1.0         # T212 rechaza dejar restos < 1: si quedaría menos, se cierra entera
FILL_TIMEOUT = 15.0        # sondeo de la orden: cada 1,2 s hasta 15 s
POLL_S = 1.2
CANCEL_WAIT = 6.0
HISTORY_TRIES = 3          # el historial tarda un poco en mostrar el fill
RECONCILE_ROUNDS, RECONCILE_WAIT = 2, 3.0
FINAL = ("FILLED", "CANCELLED", "REJECTED")


class T212Error(Exception):
    """Error de la API. kind ∈ auth, insufficient, precision, not_owned, market_closed, not_found, rate,
    invalid, network (resultado incierto: red, timeout, 408, 5xx), unknown."""

    def __init__(self, status: int, code: str, detail: str, kind: str):
        super().__init__(f"Trading 212 {status} {code or kind}: {detail}")
        self.status, self.code, self.detail, self.kind = status, code, detail, kind


def _kind(status: int, text: str) -> str:
    b = re.sub(r"[^a-z]", "", text.lower())
    if status in (401, 403):  # clave mala/revocada, sin permiso o not-available-in-real-money-account
        return "auth"
    if status == 429:
        return "rate"
    if status == 408 or status >= 500:
        return "network"
    if "precision" in b:
        return "precision"
    if "notowned" in b or ("insufficient" in b and ("quantity" in b or "shares" in b)):
        return "not_owned"
    if "insufficient" in b:
        return "insufficient"
    if "marketclosed" in b or "marketisclosed" in b:
        return "market_closed"
    if status == 404 or "notfound" in b or "doesnotexist" in b:
        return "not_found"
    return "invalid" if status in (400, 422) else "unknown"


def _error(status: int, raw: bytes | None) -> T212Error:
    """Tres formas de cuerpo: {"code","clarification"}, problem-JSON {"type","title","detail"} (a veces
    dentro de {"error": ...}) y texto plano (el 401 de Cloudflare es "\\n")."""
    text = (raw or b"").decode("utf-8", "replace").strip()
    try:
        j = json.loads(text) if text else {}
    except ValueError:
        j = {}
    if isinstance(j, dict) and isinstance(j.get("error"), dict):
        j = j["error"]
    if not isinstance(j, dict):
        j = {}
    code = str(j.get("code") or j.get("type") or "")
    detail = str(j.get("clarification") or j.get("detail") or j.get("message") or j.get("title")
                 or text)[:300]
    return T212Error(status, code, detail, _kind(status, f"{code} {detail} {j.get('title') or ''}"))


def _ts(v) -> float | None:
    """x-ratelimit-reset: timestamp Unix en segundos (o en ms)."""
    try:
        t = float(v)
    except (TypeError, ValueError):
        return None
    return t / 1000 if t > 1e12 else t


def _iso_ts(v) -> float | None:
    if not v:
        return None
    try:
        dt = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None
    return (dt if dt.tzinfo else dt.replace(tzinfo=UTC)).timestamp()


def urllib_http(method: str, url: str, headers: dict, body: bytes | None,
                timeout: float) -> tuple[int, dict, bytes]:
    """Transporte por defecto: (status, cabeceras, cuerpo). Los errores HTTP vuelven como status; los de
    red (URLError, timeout, conexión cortada) se propagan."""
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, dict(r.headers.items()), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers.items()) if e.headers else {}, e.read() or b""


class T212Client:
    def __init__(self, key: str, secret: str, env: str = "live", http=None, clock=time.time,
                 sleep=time.sleep):
        if env not in HOSTS:
            raise ValueError(f"execution.t212_env debe ser 'live' o 'demo', no {env!r}")
        self.env, self.host = env, HOSTS[env]
        self._auth = "Basic " + base64.b64encode(f"{key}:{secret}".encode()).decode()
        self._http = http or urllib_http
        self.clock, self.sleep = clock, sleep
        self._next: dict[str, float] = {}
        self._lock = threading.Lock()

    def __repr__(self) -> str:  # sin la clave
        return f"T212Client({self.env})"

    # --- endpoints -------------------------------------------------------------------------------
    def summary(self) -> dict:
        return self._call("GET", "/account/summary", "summary") or {}

    def positions(self, ticker: str | None = None) -> list[dict]:
        query = {"ticker": ticker} if ticker else None
        return self._call("GET", "/positions", "positions", query=query) or []

    def pending(self) -> list[dict]:
        """Órdenes activas (no llenadas, canceladas ni caducadas)."""
        return self._call("GET", "/orders", "pending") or []

    def market_order(self, ticker: str, qty: float) -> dict:
        """qty > 0 compra, < 0 venta. NUNCA reintenta (no es idempotente)."""
        return self._call("POST", "/orders/market", "market",
                          body={"ticker": ticker, "quantity": qty, "extendedHours": False}) or {}

    def order(self, order_id) -> dict | None:
        """La orden si sigue pendiente; None (404) si ya se llenó, se canceló o no existe."""
        try:
            return self._call("GET", f"/orders/{int(order_id)}", "order")
        except T212Error as e:
            if e.kind == "not_found":
                return None
            raise

    def history(self, ticker: str, limit: int = 10) -> list[dict]:
        """Órdenes históricas del valor, más recientes primero: [{"order": {...}, "fill": {...}}]."""
        r = self._call("GET", "/history/orders", "history", query={"ticker": ticker, "limit": min(limit, 50)})
        return (r or {}).get("items") or []

    def stop_order(self, ticker: str, qty_negative: float, stop_price: float) -> dict:
        if qty_negative >= 0:
            raise ValueError("el stop-loss de una posición larga lleva cantidad negativa")
        return self._call("POST", "/orders/stop", "stop", body={
            "ticker": ticker, "quantity": qty_negative, "stopPrice": stop_price,
            "timeValidity": "GOOD_TILL_CANCEL"}) or {}

    def cancel(self, order_id) -> None:
        """200 = petición de cancelación aceptada (no garantiza que no se llene antes)."""
        self._call("DELETE", f"/orders/{int(order_id)}", "cancel")

    # --- transporte ------------------------------------------------------------------------------
    def _wait(self, bucket: str) -> None:
        """Reserva el siguiente hueco del cubo (seguro entre hilos) y espera a que llegue."""
        with self._lock:
            now = self.clock()
            start = max(now, self._next.get(bucket, 0.0))
            if start - now > MAX_WAIT:  # reset lejano (o reloj desfasado): no se bloquea el bot
                raise T212Error(429, "rate_wait", f"límite de '{bucket}': faltan {start - now:.0f} s", "rate")
            self._next[bucket] = start + GAP[bucket]
        if start > now:
            self.sleep(start - now)

    def _limits(self, bucket: str, status: int, h: dict) -> None:
        reset, until = _ts(h.get("x-ratelimit-reset")), None
        if status == 429:
            if reset is None:
                after = _ts(h.get("retry-after"))  # solo en demo
                reset = self.clock() + (after if after is not None else 60.0)
            until = reset
        elif str(h.get("x-ratelimit-remaining", "")).strip() == "0" and reset:
            until = reset
        if until:
            with self._lock:
                self._next[bucket] = max(self._next.get(bucket, 0.0), until)

    def _call(self, method: str, path: str, bucket: str, body: dict | None = None,
              query: dict | None = None):
        url = self.host + API + path + ("?" + urllib.parse.urlencode(query) if query else "")
        data = None if body is None else json.dumps(body).encode()
        headers = {"Authorization": self._auth, "Accept": "application/json", "User-Agent": UA}
        if data is not None:
            headers["Content-Type"] = "application/json"
        tries = GET_TRIES if method == "GET" else 1
        for attempt in range(tries):
            self._wait(bucket)
            log.debug("Trading 212 %s %s", method, path)
            try:
                status, hdrs, raw = self._http(method, url, headers, data, TIMEOUT)
            except (OSError, http.client.HTTPException) as exc:  # URLError, timeout, conexión cortada
                err = T212Error(0, "network", f"{type(exc).__name__}: {exc}", "network")
            else:
                hdrs = {str(k).lower(): v for k, v in (hdrs or {}).items()}
                self._limits(bucket, status, hdrs)
                if 200 <= status < 300:
                    try:
                        return json.loads(raw) if raw and raw.strip() else None
                    except ValueError:  # respuesta cortada: en un POST el resultado es incierto
                        err = T212Error(status, "bad_json", "respuesta no JSON", "network")
                else:
                    err = _error(status, raw)
            if err.kind not in ("rate", "network") or attempt == tries - 1:
                raise err
            if err.kind == "rate" and self._next.get(bucket, 0.0) - self.clock() > MAX_WAIT:
                raise err
            if err.kind == "network":
                self.sleep(2.0 ** attempt)
            log.info("Trading 212 %s %s: %s; reintento", method, path, err)
        raise AssertionError("inalcanzable")


def quantity_decimals(price: float) -> int:
    """Decimales máximos de la cantidad según el precio (regla de Trading 212)."""
    return sum(price >= t for t in (0.1, 1, 10, 100, 1e3, 1e4, 1e5))


def _floor(qty: float, decimals: int) -> float:
    if qty <= 0 or decimals < 0:
        return 0.0
    f = 10 ** decimals
    return round(math.floor(round(qty * f, 6)) / f, decimals)  # round(…, 6): sin ruido de coma flotante


def _decimals(qty: float) -> int:
    s = f"{qty:.10f}".rstrip("0")
    return len(s.split(".")[1]) if "." in s else 0


def round_qty(qty: float, price: float) -> float:
    """Cantidad válida para Trading 212, SIEMPRE hacia abajo."""
    return _floor(qty, quantity_decimals(price))


def credentials(env: Mapping[str, str]) -> tuple[str, str]:
    key = (env.get("TRADING212_API_KEY") or "").strip()
    secret = (env.get("TRADING212_API_SECRET") or "").strip()
    if not (key and secret):
        raise SystemExit("Trading 212: faltan TRADING212_API_KEY y TRADING212_API_SECRET en el entorno "
                         "(.env)")
    return key, secret


def _ticker_of(o: dict) -> str | None:
    return o.get("ticker") or (o.get("instrument") or {}).get("ticker")


def _qty(p: dict | None) -> float:
    return float((p or {}).get("quantity") or 0.0)


def _available(p: dict | None) -> float:
    if not p:
        return 0.0
    q = p.get("quantityAvailableForTrading")
    return float(p.get("quantity") or 0.0) if q is None else float(q)


def _stop_px(p: float) -> float:
    return round(p, 2 if p >= 1 else 4)


def _make_fill(side: str, fills: list[dict], rate: float = 1.0) -> Fill:
    """Uno o varios fills de la misma orden. Comisión = suma de taxes (vienen con signo negativo).
    `rate`: unidades de la divisa del valor por 1 EUR (los fills vienen en esa divisa; el bot va en EUR)."""
    qty = value = eur = fee = 0.0
    for f in fills:
        q, px = abs(float(f.get("quantity") or 0.0)), float(f.get("price") or 0.0)
        wi = f.get("walletImpact") or {}
        qty, value = qty + q, value + q * px
        eur += abs(float(wi.get("netValue") or 0.0)) or q * px / rate
        fee += sum(abs(float(t.get("quantity") or 0.0)) for t in wi.get("taxes") or [])
    return Fill(side, round(qty, 10), value / qty / rate, round(fee, 6), round(eur, 6))


def _stop_on_auth(fn):
    """401/403 → ccxt.AuthenticationError: bot.main deja de operar y avisa por Telegram."""
    @functools.wraps(fn)
    def wrapper(self, *args, **kwargs):
        try:
            return fn(self, *args, **kwargs)
        except T212Error as e:
            if e.kind != "auth":
                raise
            log.error("Trading 212 rechazó la API key: %s", e)
            raise ccxt.AuthenticationError(f"Trading 212 rechazó la API key o le falta un permiso "
                                           f"({e.status} {e.code}: {e.detail})") from e
    return wrapper


class T212Broker:
    """Interfaz de broker de bolsa (la misma que StocksPaperBroker): snapshot, buy, sell, place_stop,
    cancel_stop. Órdenes a mercado por cantidad; nunca reenvía un POST de resultado incierto."""

    def __init__(self, cfg: dict, client: T212Client, fx=None):
        self.client = client
        named = {s.split("/")[0]: s for s in (cfg.get("general") or {}).get("symbols") or []}
        markets = cfg.get("market") or {}
        self.tickers = {named.get(code, f"{code}/EUR"): m["t212"]
                        for code, m in markets.items() if m.get("t212")}
        self.symbols = {t: s for s, t in self.tickers.items()}
        # divisa de cada valor (EUR salvo [market.CODE].currency) y cambio: unidades por 1 EUR
        self.currency = {s: markets.get(s.split("/")[0], {}).get("currency", "EUR") for s in self.tickers}
        self.fx = fx or (lambda currency: 1.0)

    def _rate(self, symbol: str) -> float:
        """Unidades de la divisa del valor por 1 EUR (1.0 si cotiza en euros)."""
        cur = self.currency.get(symbol, "EUR")
        return 1.0 if cur == "EUR" else float(self.fx(cur))

    def _ticker(self, symbol: str) -> str:
        try:
            return self.tickers[symbol]
        except KeyError:
            raise ValueError(f"{symbol}: falta market.{symbol.split('/')[0]}.t212 en el config") from None

    def _position(self, ticker: str) -> dict | None:
        return next((p for p in self.client.positions(ticker) if _ticker_of(p) == ticker), None)

    def _safe(self, fn) -> list:
        """Lecturas de conciliación: un fallo (salvo auth) cuenta como "no encontrado"."""
        try:
            return fn() or []
        except T212Error as e:
            if e.kind == "auth":
                raise
            log.warning("Trading 212: %s", e)
            return []

    # --- lectura -------------------------------------------------------------------------------
    @_stop_on_auth
    def snapshot(self) -> tuple[float, dict[str, dict]]:
        """(efectivo EUR disponible, {"RHM/EUR": {"qty","avg","price","available"}}). Las posiciones fuera del
        universo (compradas a mano) se ignoran."""
        s = self.client.summary()
        if s.get("currency") not in (None, "EUR"):
            log.warning("Trading 212: la cuenta está en %s, no en EUR", s.get("currency"))
        cash = float((s.get("cash") or {}).get("availableToTrade") or 0.0)
        out: dict[str, dict] = {}
        for p in self.client.positions():
            sym = self.symbols.get(_ticker_of(p) or "")
            if sym is None or _qty(p) <= 0:
                log.debug("Trading 212: posición fuera del universo: %s", _ticker_of(p))
                continue
            q, wi, rate = _qty(p), p.get("walletImpact") or {}, self._rate(sym)
            if rate != 1.0 and wi.get("totalCost") and wi.get("currentValue"):  # importes ya en EUR
                avg, price = float(wi["totalCost"]) / q, float(wi["currentValue"]) / q
            else:
                avg = float(p.get("averagePricePaid") or 0.0) / rate
                price = float(p.get("currentPrice") or 0.0) / rate
            out[sym] = {"qty": q, "avg": avg, "price": price, "available": _available(p)}
        return cash, out

    @_stop_on_auth
    def closed_sell(self, symbol: str, since: float, qty: float | None = None) -> tuple[Fill, str] | None:
        """Venta real de una posición que desapareció (stop del broker o venta a mano en la app):
        los fills de venta del valor desde `since` (apertura de la posición), agregados, y si fue
        "stop" o "manual". Con `qty`, solo las ventas más recientes hasta cubrirla (una venta parcial
        anterior del propio bot ya está contada). None si el historial no la muestra."""
        ticker = self._ticker(symbol)
        sells, got = [], 0.0
        for it in self._safe(lambda: self.client.history(ticker, limit=20)):  # más recientes primero
            if qty and got >= qty * (1 - 1e-6):
                break
            o, f = it.get("order") or {}, it.get("fill")
            q = float(o.get("quantity") or 0.0)
            side = str(o.get("side") or ("BUY" if q > 0 else "SELL")).upper()
            if side != "SELL" or not f or not float(f.get("quantity") or 0.0):
                continue
            t = _iso_ts(f.get("filledAt") or o.get("createdAt") or o.get("creationTime"))
            if t is not None and t < since - 5:
                continue
            sells.append((o, f))
            got += abs(float(f.get("quantity") or 0.0))
        if not sells:
            return None
        kind = "stop" if any("STOP" in str(o.get("type") or "").upper() for o, _ in sells) else "manual"
        return _make_fill("sell", [f for _, f in sells], self._rate(symbol)), kind

    # --- órdenes -------------------------------------------------------------------------------
    @_stop_on_auth
    def buy(self, symbol: str, eur: float, price: float) -> Fill | None:
        ticker = self._ticker(symbol)
        native = price * self._rate(symbol)  # los decimales permitidos dependen del precio en su divisa
        qty = round_qty(eur * BUY_BUFFER / price, native) if price > 0 else 0.0
        if qty <= 0:
            log.warning("Trading 212: %.2f € no llegan a la cantidad mínima de %s a %.4f", eur, symbol, price)
            return None
        try:
            before: float | None = _qty(self._position(ticker))
        except T212Error as e:
            if e.kind == "auth":
                raise
            log.warning("Trading 212: no pude leer la posición de %s antes de comprar (%s)", symbol, e)
            before = None
        return self._trade(symbol, ticker, "buy", qty, price, before)

    @_stop_on_auth
    def sell(self, symbol: str, qty: float, price: float) -> Fill | None:
        """Vende min(qty, disponible); si quedaría un resto < 1 €, la posición entera."""
        ticker = self._ticker(symbol)
        if price <= 0 or qty <= 0:
            log.error("Trading 212: venta de %s con cantidad %s o precio %s no válidos", symbol, qty, price)
            return None
        try:
            pos = self._position(ticker)
        except T212Error as e:
            if e.kind == "auth":
                raise
            log.error("Trading 212: no pude leer la posición de %s; no vendo (%s)", symbol, e)
            return None
        avail = _available(pos)
        if avail <= 0:
            log.warning("Trading 212: no hay %s disponible para vender", symbol)
            return None
        q = min(qty, avail)
        q = avail if (avail - q) * price < MIN_LEFT_EUR else round_qty(q, price * self._rate(symbol))
        if q <= 0:
            return None
        return self._trade(symbol, ticker, "sell", q, price, _qty(pos))

    def _retry_qty(self, kind: str, ticker: str, side: str, qty: float, price: float) -> float:
        """Cantidad para el único reintento permitido por tipo de rechazo (0 = no reintentar)."""
        price *= self._rate(self.symbols.get(ticker, ""))  # decimales según el precio en su divisa
        if kind == "precision":
            return _floor(qty, min(quantity_decimals(price), _decimals(qty) - 1))
        if kind == "insufficient" and side == "buy":
            return round_qty(qty * (1 - SHRINK), price)
        if kind == "not_owned" and side == "sell":
            pos = self._safe(lambda: [self._position(ticker)])
            return min(qty, _available(pos[0] if pos else None))
        return 0.0

    def _trade(self, symbol: str, ticker: str, side: str, qty: float, price: float,
               before: float | None) -> Fill | None:
        sign, retried = (1 if side == "buy" else -1), set()
        while True:
            t_sent = self.client.clock()
            try:
                order = self.client.market_order(ticker, sign * qty)
            except T212Error as e:
                if e.kind == "auth":
                    raise
                if e.kind == "network":
                    log.warning("Trading 212: resultado incierto al enviar %s %.7g %s (%s). No se reenvía: "
                                "concilio", side, qty, symbol, e)
                    return self._reconcile(symbol, ticker, side, qty, price, t_sent, before)
                if e.kind == "market_closed":
                    log.warning("Trading 212: mercado cerrado para %s (%s); no se opera", symbol, ticker)
                    return None
                new = 0.0 if e.kind in retried else self._retry_qty(e.kind, ticker, side, qty, price)
                if new <= 0 or new == qty:
                    log.error("Trading 212 rechazó %s %.7g %s: %s", side, qty, symbol, e)
                    return None
                retried.add(e.kind)
                log.warning("Trading 212 rechazó %s %.7g %s (%s): reintento una vez con %.7g",
                            side, qty, symbol, e.kind, new)
                qty = new
                continue
            if order.get("id") is None:
                log.warning("Trading 212: respuesta sin id al enviar %s %s; concilio", side, symbol)
                return self._reconcile(symbol, ticker, side, qty, price, t_sent, before)
            return self._settle(symbol, ticker, side, qty, price, order, before)

    def _poll(self, oid, last: dict | None) -> dict | None:
        try:
            return self.client.order(oid)
        except T212Error as e:
            if e.kind == "auth":
                raise
            log.debug("Trading 212: sondeo de la orden %s: %s", oid, e)
            return last

    def _cancel(self, oid, last: dict | None = None) -> dict | None:
        """Cancela y espera a que la orden deje de estar pendiente. Devuelve su último estado (None = ya
        no está pendiente)."""
        try:
            self.client.cancel(oid)
        except T212Error as e:
            if e.kind == "auth":
                raise
            if e.kind == "not_found":
                return None
            log.warning("Trading 212: no pude cancelar la orden %s: %s", oid, e)
            return last
        deadline = self.client.clock() + CANCEL_WAIT
        while self.client.clock() < deadline:
            last = self._poll(oid, last)
            if last is None or last.get("status") in FINAL:
                return last
            self.client.sleep(POLL_S)
        log.warning("Trading 212: la orden %s sigue pendiente tras cancelarla", oid)
        return last

    def _history_fill(self, ticker: str, oid, side: str) -> tuple[bool, Fill | None]:
        """(encontrada, fill). Agrega los fills de la orden; cancelada/rechazada sin nada llenado → None."""
        for _ in range(HISTORY_TRIES):
            items = self._safe(lambda: self.client.history(ticker, limit=10))
            mine = [it for it in items if str((it.get("order") or {}).get("id")) == str(oid)]
            fills = [f for it in mine if (f := it.get("fill")) and float(f.get("quantity") or 0.0)]
            if fills:
                return True, _make_fill(side, fills, self._rate(self.symbols.get(ticker, "")))
            o = (mine[0].get("order") or {}) if mine else {}
            if o.get("status") in ("CANCELLED", "REJECTED") and not float(o.get("filledQuantity") or 0.0):
                return True, None
        return False, None

    def _positions_fill(self, ticker: str, side: str, qty: float, price: float,
                        before: float | None) -> Fill | None:
        """Último recurso: cambio de la posición frente a antes de enviar (precio = el de referencia)."""
        if before is None:
            return None
        pos = self._safe(lambda: [self._position(ticker)])  # [None] = sin posición; [] = no se pudo leer
        if not pos:
            return None
        after = _qty(pos[0])
        delta = after - before if side == "buy" else before - after
        if delta <= 1e-9:
            return None
        q = min(delta, qty)
        return Fill(side, q, price, 0.0, q * price)

    def _settle(self, symbol: str, ticker: str, side: str, qty: float, price: float, order: dict,
                before: float | None) -> Fill | None:
        oid, last = order["id"], order
        deadline = self.client.clock() + FILL_TIMEOUT
        while last is not None and last.get("status") not in FINAL and self.client.clock() < deadline:
            self.client.sleep(POLL_S)
            last = self._poll(oid, last)
        if last is not None and last.get("status") not in FINAL:
            # sigue en cola (mercado cerrado, subasta…): no se deja una orden viva sin control
            log.warning("Trading 212: la orden %s de %s sigue %s tras %.0f s; la cancelo",
                        oid, symbol, last.get("status"), FILL_TIMEOUT)
            last = self._cancel(oid, last)
        found, fill = self._history_fill(ticker, oid, side)
        if not found:
            ref = last or order
            filled = abs(float(ref.get("filledQuantity") or 0.0))
            if filled > 0:
                fv = abs(float(ref.get("filledValue") or 0.0))  # en la divisa del valor (USD en _US_EQ)
                px = fv / filled / self._rate(symbol) if fv else price
                fill = Fill(side, filled, px, 0.0, filled * px)
            else:
                fill = self._positions_fill(ticker, side, qty, price, before)
            log.warning("Trading 212: la orden %s de %s no aparece en el historial; %s", oid, symbol,
                        "uso lo que dicen la orden/las posiciones" if fill else "la doy por no ejecutada")
        if fill:
            log.info("Trading 212: %s %.7g %s a %.4f (%.2f €, comisión %.2f €)", side, fill.qty, symbol,
                     fill.price, fill.eur, fill.fee)
        else:
            log.warning("Trading 212: la orden %s de %s (%s %.7g) no se ejecutó", oid, symbol, side, qty)
        return fill

    @staticmethod
    def _match(orders: list, ticker: str, side: str, qty: float, t_sent: float, kind: str = "MARKET",
               stop: float | None = None) -> dict | None:
        """Nuestra orden entre otras: mismo valor, lado, cantidad y tipo, enviada por la API y creada
        después de t_sent − 5 s."""
        for o in orders:
            if not isinstance(o, dict) or _ticker_of(o) != ticker:
                continue
            q = float(o.get("quantity") or 0.0)
            o_side = str(o.get("side") or ("BUY" if q > 0 else "SELL")).upper()
            created = _iso_ts(o.get("createdAt") or o.get("creationTime"))
            if (o_side == side.upper() and math.isclose(abs(q), qty, rel_tol=1e-9, abs_tol=1e-12)
                    and str(o.get("type") or kind).upper() == kind
                    and o.get("initiatedFrom") in (None, "API")
                    and (created is None or created >= t_sent - 5)
                    and (stop is None or math.isclose(float(o.get("stopPrice") or 0.0), stop, rel_tol=1e-6))):
                return o
        return None

    def _reconcile(self, symbol: str, ticker: str, side: str, qty: float, price: float, t_sent: float,
                   before: float | None) -> Fill | None:
        """Tras un POST de resultado incierto: pendientes → historial → posiciones. Nunca reenvía."""
        for _ in range(RECONCILE_ROUNDS):
            self.client.sleep(RECONCILE_WAIT)  # deja que la orden aparezca
            o = self._match(self._safe(self.client.pending), ticker, side, qty, t_sent)
            if o is None:
                items = self._safe(lambda: self.client.history(ticker, limit=10))
                o = self._match([it.get("order") for it in items], ticker, side, qty, t_sent)
            if o is not None and o.get("id") is not None:
                log.warning("Trading 212: la orden %s de %s sí llegó; sigo su ejecución", o["id"], symbol)
                return self._settle(symbol, ticker, side, qty, price, o, before)
            fill = self._positions_fill(ticker, side, qty, price, before)
            if fill:
                log.warning("Trading 212: %s %.7g %s conciliada por posiciones (precio aproximado %.4f)",
                            side, fill.qty, symbol, price)
                return fill
        log.error("Trading 212: la orden %s %.7g %s no aparece en pendientes, historial ni posiciones. "
                  "NO se reenvía: revísalo en la app", side, qty, symbol)
        return None

    # --- stop-loss en el broker ----------------------------------------------------------------
    @_stop_on_auth
    def place_stop(self, symbol: str, qty: float, stop_price: float) -> str | None:
        """Stop de venta GOOD_TILL_CANCEL (salta con el último precio negociado). Id o None si falla."""
        ticker = self._ticker(symbol)  # el stop se envía en la divisa del valor (USD en las líneas _US_EQ)
        q, sp, retried = abs(qty), _stop_px(stop_price * self._rate(symbol)), set()
        while q > 0:
            t_sent = self.client.clock()
            try:
                o = self.client.stop_order(ticker, -q, sp)
            except T212Error as e:
                if e.kind == "auth":
                    raise
                if e.kind == "network":  # resultado incierto: se busca entre las pendientes, nunca se reenvía
                    log.warning("Trading 212: resultado incierto al poner el stop de %s (%s); concilio",
                                symbol, e)
                    self.client.sleep(RECONCILE_WAIT)
                    pending = self._safe(self.client.pending)
                    o = self._match(pending, ticker, "sell", q, t_sent, "STOP", sp) or {}
                else:
                    # _retry_qty pasa el precio a la divisa del valor: se le da en EUR (sp ya está en USD)
                    new = 0.0 if e.kind in retried else self._retry_qty(e.kind, ticker, "sell", q, stop_price)
                    if new <= 0 or new == q:
                        log.error("Trading 212 rechazó el stop de %s a %.4f: %s", symbol, sp, e)
                        return None
                    retried.add(e.kind)
                    log.warning("Trading 212 rechazó el stop de %s (%s): reintento una vez con %.7g",
                                symbol, e.kind, new)
                    q = new
                    continue
            if o.get("id") is None:
                log.error("Trading 212: el stop de %s no aparece; no se reenvía", symbol)
                return None
            log.info("Trading 212: stop de %s (%.7g) a %.4f, orden %s", symbol, q, sp, o["id"])
            return str(o["id"])
        return None

    @_stop_on_auth
    def cancel_stop(self, order_id) -> None:
        """Cancela el stop (antes de vender). Si ya no estaba pendiente (saltó o se canceló), no hace nada."""
        if not order_id:
            return
        self._cancel(order_id)
        try:  # sin confirmación positiva no se vende: el stop podría seguir reservando las acciones
            still = any(str(o.get("id")) == str(order_id) for o in self.client.pending())
        except T212Error as exc:
            if exc.kind == "auth":
                raise
            raise T212Error(exc.status, exc.code, f"no pude comprobar el stop {order_id}", "unknown") from exc
        if still:
            raise T212Error(0, "", f"el stop {order_id} sigue activo", "unknown")
