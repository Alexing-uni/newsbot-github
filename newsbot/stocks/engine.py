"""Motor de la cartera de bolsa multiposición (general.engine = "stocks").

run_tick (cada 15 min con el mercado abierto): velas y precio de Yahoo, titulares, sentimiento por
valor, eventos y técnicos → fusion.decide por valor → ventas que toquen (stop-loss = urgente) y
como mucho UNA propuesta de compra. Nada se ejecuta sin tu ✅ en Telegram (condiciones de la API de
Trading 212): confirm() comprueba caducidad, horario, deriva del precio y efectivo, y entonces
opera, apunta la orden y, con execution.broker_stop, deja el stop-loss puesto en el broker.
Un RLock por data_dir serializa run_tick y confirm (hilo del bucle e hilo de Telegram).
"""
import html
import json
import logging
import os
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import asdict, replace
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from statistics import median
from zoneinfo import ZoneInfo

from newsbot import config as config_mod
from newsbot import portfolio, sentiment
from newsbot.features import market, session
from newsbot.news import ingest
from newsbot.reporting import telegram
from newsbot.risk import sizing as risk_sizing
from newsbot.stocks import adaptive, insight, kronos_signal, localnews, sizing, technical
from newsbot.stocks.paper import StocksPaperBroker
from newsbot.storage import db
from newsbot.strategy.fusion import Decision, decide, smooth

log = logging.getLogger(__name__)
EXTRA = (*market.SHADOW, "events", "breakout", "macd", "high52", "kronos", "dip", "finbert")  # solo con peso
NAMES = {"news": "noticias", "trend": "tendencia", "intraday": "intradía", "events": "eventos",
         "breakout": "ruptura", "macd": "MACD", "high52": "máx. 52 sem.", "rsi": "RSI",
         "bollinger": "Bollinger", "momentum": "momentum", "volume": "volumen", "fng": "F&G",
         "votes": "votos", "kronos": "Kronos",
         "dip": "caída (rebote)", "finbert": "noticias (local)"}
STATUS = {"accepted": "aceptada (ejecutándose)", "rejected": "descartada", "expired": "caducada",
          "executed": "ejecutada", "failed": "fallida"}
DUST = 1e-9
_LOCKS: dict[str, threading.RLock] = {}
_CLIENTS: dict[tuple, object] = {}  # clientes de Trading 212 por cartera (nunca se loguean)
_GUARD = threading.Lock()


# --- utilidades ---------------------------------------------------------------------------------
def _key(cfg: dict) -> str:
    return str(Path(cfg["general"]["data_dir"]).resolve())


def lock(cfg: dict) -> threading.RLock:
    """Candado de la cartera (uno por data_dir), compartido por run_tick y confirm."""
    with _GUARD:
        return _LOCKS.setdefault(_key(cfg), threading.RLock())


def _now(cfg: dict, now: datetime | None) -> datetime:
    tz = ZoneInfo(cfg["general"]["timezone"])
    return (now or datetime.now(tz)).astimezone(tz)


def _connect(cfg: dict):
    data_dir = Path(cfg["general"]["data_dir"])
    data_dir.mkdir(parents=True, exist_ok=True)
    return closing(db.connect(data_dir / "newsbot.db"))


def e(x) -> str:
    return html.escape("" if x is None else str(x), quote=True)


def _code(symbol: str) -> str:
    return config_mod.asset(symbol)


def _market(cfg: dict, symbol: str) -> dict:
    return (cfg.get("market") or {}).get(_code(symbol)) or {}


def _name(cfg: dict, symbol: str) -> str:
    """"RHM (Rheinmetall)": el código y el nombre corto de [market.CODE].name."""
    short = (_market(cfg, symbol).get("name") or "").split(" (")[0].strip()
    return f"{_code(symbol)} ({short})" if short else _code(symbol)


def _group(cfg: dict, symbol: str) -> str:
    return _market(cfg, symbol).get("group") or _code(symbol)


def _signed(text: str) -> str:
    return text.replace("-", "−")


def _pct(x: float) -> str:
    return _signed(f"{x * 100:+.1f} %")


def _qty(q: float) -> str:
    return f"{q:.4f}" if q >= 0.01 else f"{q:.6f}"


def _hhmm(cfg: dict, iso: str) -> str:
    return datetime.fromisoformat(iso).astimezone(ZoneInfo(cfg["general"]["timezone"])).strftime("%H:%M")


def _is_live(cfg: dict) -> bool:
    return cfg["general"]["mode"] == "live" and cfg["execution"].get("broker", "paper") == "trading212"


def _drivers(contributions: dict[str, float], n: int = 3) -> str:
    """"noticias +0.20, eventos +0.10, tendencia +0.09…": los componentes que más aportan."""
    items = sorted(((k, v) for k, v in contributions.items() if abs(v) >= 0.005), key=lambda kv: -abs(kv[1]))
    text = ", ".join(_signed(f"{NAMES.get(k, k)} {v:+.2f}") for k, v in items[:n])
    return text + ("…" if len(items) > n else "") if text else "sin componentes"


def _close_at(cfg: dict, now: datetime) -> datetime | None:
    """Cierre de la sesión de hoy (None sin [session])."""
    s = cfg.get("session")
    if not s:
        return None
    h, m = (int(v) for v in s["close"].split(":"))
    local = now.astimezone(ZoneInfo(s["tz"]))
    return datetime.combine(local.date(), time(h, m), local.tzinfo).astimezone(now.tzinfo)


def _send(text: str, markup: dict | None = None) -> int | None:
    """Aviso HTML (con la etiqueta de la cartera). Con telegram.send_get_id devuelve el message_id
    para editar el mensaje al responder. Nunca lanza: Telegram caído no para el tick."""
    try:
        get_id = getattr(telegram, "send_get_id", None)
        if get_id is not None:
            return get_id(text, html=True, markup=markup)
        telegram.send(text, html=True, markup=markup)
    except Exception as exc:
        log.warning("Telegram: aviso no enviado (%s)", type(exc).__name__)
    return None


IDLE_NOTE_MINUTES = 60  # el aviso del dinero parado llega una vez al día, en la última hora de sesión


def _idle_note(cfg: dict, kv, lines: list[str], now: datetime) -> None:
    close = _close_at(cfg, now)
    day = now.date().isoformat()
    if close is None or now < close - timedelta(minutes=IDLE_NOTE_MINUTES) or kv.get("idle:sent") == day:
        return
    kv.set("idle:sent", day)
    head, *rest = lines
    _notify(f"<b>{e(head)}</b>\n" + "\n".join(f"• {e(ln)}" for ln in rest))


def _notify(text: str) -> None:
    try:
        telegram.send(text, html=True)
    except Exception as exc:
        log.warning("Telegram: aviso no enviado (%s)", type(exc).__name__)


def _events():  # import perezoso: stocks/events.py es de otro paquete de trabajo
    from newsbot.stocks import events
    return events


def _t212():  # import perezoso: solo hace falta en live
    from newsbot.execution import trading212
    return trading212


def refresh_events(con: sqlite3.Connection, cfg: dict, now: datetime, headlines) -> int:
    """Busca eventos con fecha en los titulares si toca (events.due). Nunca rompe el tick."""
    if not (cfg.get("events") or {}).get("enabled"):
        return 0
    try:
        ev = _events()
        return ev.refresh(con, cfg, now, headlines) if ev.due(con, cfg, now) else 0
    except Exception as exc:
        log.warning("Eventos: no se pudieron actualizar (%s: %s)", type(exc).__name__, exc)
        return 0


def event_score(con: sqlite3.Connection, cfg: dict, code: str, now: datetime) -> float:
    if not (cfg.get("events") or {}).get("enabled"):
        return 0.0
    try:
        return max(-1.0, min(1.0, float(_events().score(con, cfg, code, now))))
    except Exception as exc:
        log.debug("Eventos: sin puntuación para %s (%s)", code, type(exc).__name__)
        return 0.0


def make_broker(cfg: dict, con: sqlite3.Connection, env=None):
    """Papel salvo mode = "live" con execution.broker = "trading212", que exige además
    CONFIRM_LIVE=yes y TRADING212_API_KEY/SECRET (si falta algo: SystemExit)."""
    if not _is_live(cfg):
        return StocksPaperBroker(cfg, con)
    env = os.environ if env is None else env
    missing = [name for name, ok in [
        ("CONFIRM_LIVE=yes", env.get("CONFIRM_LIVE") == "yes"),
        ("TRADING212_API_KEY y TRADING212_API_SECRET",
         bool(env.get("TRADING212_API_KEY") and env.get("TRADING212_API_SECRET"))),
    ] if not ok]
    if missing:
        raise SystemExit("Modo live de bolsa (Trading 212) bloqueado. Falta: " + ", ".join(missing))
    t = _t212()
    key, secret = t.credentials(env)
    where = cfg["execution"].get("t212_env", "live")
    with _GUARD:  # un cliente por cartera: tick y Telegram comparten la separación entre llamadas
        client = _CLIENTS.get(k := (_key(cfg), where, key))
        if client is None:
            client = _CLIENTS[k] = t.T212Client(key, secret, env=where)
    fx = None
    if any(m.get("currency", "EUR") != "EUR" for m in (cfg.get("market") or {}).values()):
        fx = market.make_exchange(cfg["general"]["exchange"], markets=cfg.get("market")).fx_rate
    return t.T212Broker(cfg, client, fx=fx)


def _effective(cfg: dict, con: sqlite3.Connection) -> dict:
    """cfg con los parámetros aprobados (learn/reoptimize) encima, como run_once en cripto."""
    pid, override = db.active_params(con, cfg["general"]["mode"])
    try:
        return config_mod.apply(cfg, override) | {"_params_id": pid}
    except ValueError as exc:
        log.error("Parámetros aplicados #%s inválidos (%s); uso el config", pid, exc)
        return cfg | {"_params_id": None}


# --- datos --------------------------------------------------------------------------------------
def _market_data(cfg: dict, ex, now: datetime) -> dict:
    """Velas diarias cerradas, 15 min, precio y si cotiza hoy, por valor. Un valor sin datos se
    salta (avisa en el log); sin datos de ninguno el tick falla."""
    s, now_ms, out, series = cfg["strategy"], int(now.timestamp() * 1000), {}, {}
    kron = cfg.get("kronos") or {}  # Kronos (componente sombra): necesita ~256 velas de 15 min

    def fetch(x):
        try:
            candles = market.closed_candles(ex, x, now_ms, limit=260)  # 52 semanas para high52
            short = market.closed_candles(ex, x, now_ms, limit=300 if kron.get("enabled") else 100,
                                          timeframe=s["intraday_timeframe"])
            price = market.last_price(ex, x)
            today = (market.with_retry(ex.trading_today, x, now_ms)
                     if cfg.get("session") and hasattr(ex, "trading_today") else True)
            return candles, short, price, today
        except Exception as exc:
            return exc

    symbols = cfg["general"]["symbols"]
    with ThreadPoolExecutor(max_workers=min(8, len(symbols))) as pool:  # los valores en paralelo
        got = dict(zip(symbols, pool.map(fetch, symbols), strict=True))
    for x in symbols:
        if isinstance(got[x], Exception):
            log.warning("Sin datos de %s (%s: %s)", x, type(got[x]).__name__, got[x])
            continue
        candles, short, price, today = got[x]
        feats = market.features(candles, cfg) | technical.features(candles, price) | {
            "intraday": market.intraday_score([c[4] for c in short], s["ema_fast"], s["ema_slow"])}
        out[x], series[x] = {"price": price, "today": bool(today), "feats": feats,
                             "prev_close": candles[-1][4] if candles else None}, short
    if not out:
        raise RuntimeError("sin datos de mercado de ningún valor")
    ks = (kronos_signal.scores(series, kron.get("pred_len", 8), kron.get("size", "mini"))
          if kron.get("enabled") else {})
    for x, m in out.items():
        m["feats"]["kronos"] = ks.get(x, 0.0)
    return out


FROZEN_MINUTES = 14  # todos los precios idénticos durante este tiempo = fuente congelada


def _mark_frozen(con: sqlite3.Connection, mk: dict, now: datetime, kv, dry_run: bool) -> bool:
    """Fuente de precios congelada (el 5-nov Yahoo dio el mismo precio a TODOS los valores de EE. UU.
    durante 1 h 40 min): si cada valor (al menos 3) tiene exactamente el precio de un ciclo de hace
    ≥ FROZEN_MINUTES, se marca m["frozen"] y no se compra; se avisa una vez al empezar y al acabar."""
    cut = now.timestamp() - FROZEN_MINUTES * 60
    same = 0
    for x, m in mk.items():
        row = con.execute("SELECT price FROM ticks WHERE symbol = ? AND ts <= ? AND ts >= ? "
                          "AND action != 'BACKFILL' ORDER BY ts DESC LIMIT 1",
                          (x, cut, cut - 1200)).fetchone()
        if row is None or row[0] is None:
            break
        same += abs(row[0] - m["price"]) <= 1e-9 * max(1.0, abs(m["price"]))
    frozen = len(mk) >= 3 and same == len(mk)
    for m in mk.values():
        m["frozen"] = frozen
    if dry_run:
        return frozen
    if frozen and not kv.get("prices:frozen"):
        kv.set("prices:frozen", now.isoformat())
        log.warning("Precios congelados: ningún valor se mueve desde hace %s min", FROZEN_MINUTES)
        _notify(f"⚠️ Ningún precio se ha movido en {FROZEN_MINUTES} min: la fuente de datos (Yahoo) parece "
                "congelada. No compro nada hasta que vuelva; los stops siguen en Trading 212.")
    elif not frozen and (since := kv.get("prices:frozen")):
        kv.delete("prices:frozen")
        log.info("Precios de nuevo en movimiento (congelados desde %s)", since[11:16])
        _notify("✅ Los precios vuelven a moverse: sigo analizando con normalidad.")
    return frozen


# --- días de pánico: aviso por Telegram (nunca compra por ello) y seguimiento del rebote ----------
PANIC_DROP = -0.02     # un valor "cae" si pierde un 2 % o más frente al cierre de ayer
PANIC_SHARE = 0.6      # ... y caen así al menos el 60 % de los valores
PANIC_MEDIAN = -0.025  # ... con una caída mediana de al menos el 2,5 %
PANIC_WORDS = ("sell-off", "selloff", "sell off", "crash", "plunge", "rout", "panic", "capitulation",
               "bloodbath", "worst day", "since covid", "dump", "tumble", "meltdown", "desplome", "pánico")
PANIC_KEEP = 20        # días de pánico guardados en el KV "panic:days"


def _panic(mk: dict, headlines, now: datetime, kv, dry_run: bool, con=None) -> float | None:
    """Día de pánico: casi todo cae a la vez frente al cierre de ayer. Una vez al día avisa por
    Telegram con los peores valores y los titulares de pánico, y guarda el día y sus precios para medir
    después si comprar habría compensado. No cambia ninguna decisión: el pánico no es señal de compra.
    Devuelve la caída mediana (o None si no es día de pánico)."""
    ch = {x: m["price"] / m["prev_close"] - 1 for x, m in mk.items() if m.get("prev_close")}
    if len(ch) < 3:
        return None
    med = median(ch.values())
    falling = sum(v <= PANIC_DROP for v in ch.values())
    if falling / len(ch) < PANIC_SHARE or med > PANIC_MEDIAN:
        return None
    day = now.date().isoformat()
    if dry_run or kv.get("panic:sent") == day:
        return med
    kv.set("panic:sent", day)
    try:
        days = json.loads(kv.get("panic:days") or "[]")
    except ValueError:
        days = []
    low = sorted(ch, key=ch.get)[:3]
    days.append({"day": day, "ts": now.isoformat(), "median": round(med, 5), "worst": low,
                 "prices": {x: m["price"] for x, m in mk.items()}})
    kv.set("panic:days", json.dumps(days[-PANIC_KEEP:]))
    worst = ", ".join(f"{e(_code(x))} {_pct(ch[x])}" for x in low)
    hist = ""
    try:
        st = insight.panic_stats(con, PANIC_DROP, PANIC_SHARE, PANIC_MEDIAN) if con is not None else None
    except Exception as exc:  # noqa: BLE001 - el aviso no depende de la estadística
        log.debug("panic_stats: %s", exc)
        st = None
    if st:
        hist = (f"\n\n📊 En los {st['n']} días de pánico anteriores que tengo, los 3 que más cayeron se "
                f"movieron {_pct(st['worst_mean'])} de media al día siguiente (mediana "
                f"{_pct(st['worst_median'])}; subieron {st['worst_up']:.0%} de las veces) y el conjunto "
                f"{_pct(st['all_mean'])}. Con tan pocos casos no es una regla: lo sigo midiendo.")
    news = [h for h in headlines if any(w in (h.title or "").lower() for w in PANIC_WORDS)][:3]
    log.warning("Día de pánico: %s de %s valores caen ≥2 %% (mediana %.1f %%)", falling, len(ch), med * 100)
    _notify(f"🚨 <b>Día de pánico</b>: {falling} de {len(ch)} valores caen más de un 2 % (mediana "
            f"{_pct(med)}). Los peores: {worst}."
            + ("\n<b>Titulares</b>\n" + "\n".join(f"• {e(h.title[:140])}" for h in news) if news else "")
            + hist
            + "\n\nNo compro por esto: un día de pánico puede ser el final de la caída o solo el principio "
              "(en el COVID, tras el primer gran día el mercado cayó otro ~25 %). Si te tienta comprar, "
              "compáralo antes con los datos (abajo). Guardo este día para medir qué pasa después.")
    return med


def _panic_followup(kv, mk: dict) -> list[str]:
    """Para /pensamiento: cuánto se han movido desde cada día de pánico guardado (mediana de los valores),
    o sea, qué habría pasado comprando aquel día."""
    try:
        days = json.loads(kv.get("panic:days") or "[]")
    except ValueError:
        return []
    out = []
    for d in days[-3:]:
        px = d.get("prices") or {}
        moves = {x: mk[x]["price"] / p - 1 for x, p in px.items() if x in mk and p}
        if moves:
            when = f"{d['day'][8:10]}/{d['day'][5:7]}"
            low = [moves[x] for x in d.get("worst") or [] if x in moves]
            tail = f"; los 3 que más cayeron {insight._pct(sum(low) / len(low))}" if low else ""
            out.append(f"📉 Desde el pánico del {when} ({insight._pct(d['median'])} ese día): mediana "
                       f"{insight._pct(median(moves.values()))} si hubiera comprado entonces{tail}")
    return out


_NEWS: dict[str, tuple[datetime, list]] = {}  # cartera -> (cuándo, titulares): news.refresh_minutes


def _headlines(cfg: dict, now: datetime) -> tuple[list, list[float]]:
    """Titulares de los feeds. Con news.refresh_minutes se reutilizan durante ese tiempo: los ciclos
    rápidos (1-2 min) siguen el precio sin volver a descargar todas las noticias cada vez."""
    n, key = cfg["news"], _key(cfg)
    hit = _NEWS.get(key)
    if hit and timedelta(0) <= now - hit[0] < timedelta(minutes=n.get("refresh_minutes", 0)):
        hs = hit[1]
    else:
        try:
            hs = ingest.collect(cfg, now)
            _NEWS[key] = (now, hs)
        except Exception as exc:  # sin titulares las noticias cuentan 0, no se para el tick
            log.warning("Titulares no disponibles (%s)", type(exc).__name__)
            hs = []
    return hs, [ingest.weight(h, now, n["source_weights"], n["half_life_hours"]) for h in hs]


class _ReadOnlyKV:
    """KV para el dry-run: lee la base de datos y no escribe nada."""

    def __init__(self, kv: db.KV):
        self.kv, self.mem = kv, {}

    def get(self, key, default=None):
        return self.mem.get(key, self.kv.get(key, default))

    def set(self, key, value):
        self.mem[key] = value


def _sentiments(cfg: dict, kv: db.KV, headlines, weights, symbols, day: str,
                dry_run: bool = False) -> dict:
    """Sentimiento de cada valor con los titulares que lo mencionan y su EMA (como cripto). Con
    [local_news], FinBERT puntúa cada titular en local y una noticia nueva y fuerte adelanta Claude."""
    n, s = cfg["news"], cfg["strategy"]
    prev_ema = json.loads(kv.get("news_ema", "{}"))  # {símbolo: [día, ema, ema anterior]}
    local_kv = _ReadOnlyKV(kv) if dry_run else kv
    matches = {x: [ingest.mentions(h, n["assets"][_code(x)]) for h in headlines] for x in symbols}
    hits = [sum(m[i] for m in matches.values()) for i in range(len(headlines))]  # valores por titular
    ln = cfg.get("local_news") or {}
    out, alerts = {}, []
    for x in symbols:
        code = _code(x)
        pairs = [(h, w) for h, w, m in zip(headlines, weights, matches[x], strict=True) if m]
        hs, ws = [h for h, _ in pairs], [w for _, w in pairs]
        local, breaking = localnews.analyze(local_kv, cfg, code, pairs)
        # una noticia fuerte es de UNA empresa (o dos): un titular general de mercado no adelanta nada
        specific = {h.title for h, k in zip(headlines, hits, strict=True) if k <= ln.get("max_symbols", 2)}
        breaking = [t for t in breaking if t in specific]
        if breaking and not dry_run and kv.get(f"llm:last:{code}") and len(alerts) < ln.get("max_alerts", 3):
            kv.delete(f"llm:last:{code}")  # Claude vuelve a leer este valor ya, no dentro de 3 h
            log.info("Noticia fuerte de %s: adelanto Claude (%s)", code, breaking[0][:80])
            alerts.append(f"• {e(_code(x))}: «{e(breaking[0][:160])}»")
        sent = sentiment.analyze(hs, cfg, ws, cache=kv, asset=code)
        p = prev_ema.get(x)
        prev = (p[1] if p[0] != day else p[2]) if p else None
        out[x] = {"pairs": pairs, "sent": sent, "prev_ema": prev, "votes": ingest.votes_score(hs),
                  "local": local,
                  "news_ema": smooth(prev, sent.score * sent.confidence, s["news_ema_days"])}
    if alerts:  # un solo aviso por ciclo
        _notify("⚡ <b>Noticias fuertes</b> (las reviso con Claude ahora mismo):\n" + "\n".join(alerts))
    return out


# --- estado -------------------------------------------------------------------------------------
def _external_sell(broker, sym: str, opened: str | None, qty: float | None = None):
    """(Fill, "stop"|"manual") de la venta real en el historial del broker, o None (nunca rompe)."""
    if broker is None or not hasattr(broker, "closed_sell"):
        return None
    try:
        since = datetime.fromisoformat(opened).timestamp() if opened else 0.0
        return broker.closed_sell(sym, since, qty)
    except Exception as exc:
        log.warning("Historial de %s no disponible (%s): precio estimado", sym, type(exc).__name__)
        return None


def _sync(con: sqlite3.Connection, cfg: dict, held: dict, mk: dict, now: datetime,
          dry_run: bool, broker=None) -> dict[str, dict]:
    """La tabla positions sigue al broker: si una posición desapareció (stop ejecutado en Trading
    212 o venta a mano) se borra y se avisa, con el precio real de la venta si el historial del broker
    la muestra (queda en orders y en /operaciones); si apareció una, se añade con su stop."""
    rows = {r["symbol"]: dict(r) for r in db.open_positions(con)}
    out: dict[str, dict] = {}
    kv = db.KV(con)
    for sym, row in rows.items():
        b = held.get(sym)
        if b and b["qty"] > DUST:
            out[sym] = row | {"qty": float(b["qty"]),
                              "avg_entry": float(b.get("avg") or row["avg_entry"] or 0)}
            if not dry_run and kv.get(f"missing:{sym}"):
                kv.delete(f"missing:{sym}")
            continue
        if dry_run:
            continue
        if not kv.get(f"missing:{sym}"):  # una lectura vacía puede ser un fallo puntual de la API
            kv.set(f"missing:{sym}", now.isoformat())
            out[sym] = row
            log.warning("Posición %s no aparece en el broker; la doy por cerrada si sigue igual", sym)
            continue
        kv.delete(f"missing:{sym}")
        with con:
            con.execute("DELETE FROM positions WHERE symbol = ?", (sym,))
            con.execute("UPDATE proposals SET status = 'failed', decided = ?, result = 'posición ya cerrada' "
                        "WHERE symbol = ? AND side = 'sell' AND status = 'proposed'", (now.isoformat(), sym))
        name, fee_pct = e(_name(cfg, sym)), cfg["costs"]["fee_pct"]
        real = _external_sell(broker, sym, row["opened"], row["qty"])
        avg, adopted = row["avg_entry"] or 0.0, bool(kv.get(f"adopted:{sym}"))
        if real:
            fill, kind = real
            qty, px = fill.qty, fill.price
            fees = max(fill.fee, fill.eur * fee_pct) + qty * avg * fee_pct
            eur = fill.eur
            why = "saltó el stop en el broker" if kind == "stop" else "vendida a mano en el broker"
        else:  # el historial no la muestra: precio estimado (el stop, o el precio de ahora)
            qty = row["qty"]
            px = row["stop_price"] if row["stop_order_id"] and row["stop_price"] else \
                (mk[sym]["price"] if sym in mk else avg)
            fees, eur = qty * (px + avg) * fee_pct, qty * px * (1 - fee_pct)
            kind = "stop" if row["stop_order_id"] else "manual"
            why = ("saltó el stop en el broker (precio estimado)" if kind == "stop"
                   else "vendida fuera del bot (precio estimado)")
        with con:
            con.execute("INSERT OR IGNORE INTO orders(day, client_order_id, mode, side, created, status, "
                        "symbol, qty, price, fee, eur) VALUES (?,?,?,'sell',?,?,?,?,?,?,?)",
                        (now.date().isoformat(), f"ext:{sym}:{row['opened']}", cfg["general"]["mode"],
                         now.isoformat(), "filled" if real else "estimated", sym, qty, px, fees, eur))
        _budget_move(cfg, con, eur)
        kv.set(f"sold:{sym}", now.isoformat())  # no se recompra enseguida (risk.cooldown_minutes)
        pnl = (px - avg) * qty - fees if avg else 0.0
        if avg and qty and not adopted:  # lo comprado a mano no cuenta para el umbral adaptativo
            _closed_trade(con, cfg, sym, pnl / (qty * avg), now)
        review = ""
        if avg and pnl < 0 and not adopted:  # toda pérdida se estudia, aunque sea de céntimos
            review = _loss_text(con, cfg, sym, row["opened"], avg, px, qty, fees, why, now)
            _postmortem_async(cfg, sym, row["opened"], avg, px, qty, fees, why, now)
        result = ((f" a {px:.2f} € · resultado " + _signed(f"{pnl:+.2f} €") + " neto"
                   + ("" if real else ", estimado: no la veo en el historial")) if avg else "")
        if kind == "stop":
            _notify(f"🛑 Stop ejecutado en Trading 212: {name} vendida ({_qty(qty)} acciones{result})."
                    + review)
        else:
            _notify(f"🖐 {name} vendida fuera del bot ({_qty(qty)} acciones{result}): la quito de la "
                    "cartera." + review)
        log.info("Posición %s cerrada fuera del bot (%s, %s); borrada", sym,
                 "precio real" if real else "precio estimado", kind)
    for sym, b in held.items():
        if sym in out or sym not in cfg["general"]["symbols"] or b["qty"] <= DUST:
            continue
        price = mk[sym]["price"] if sym in mk else float(b.get("price") or b.get("avg") or 0)
        avg = float(b.get("avg") or price)
        atr = mk[sym]["feats"]["atr_pct"] if sym in mk else 0.0
        out[sym] = {"symbol": sym, "qty": float(b["qty"]), "avg_entry": avg, "high": max(avg, price),
                    "opened": now.isoformat(), "stop_price": avg * (1 - sizing.stop_pct(atr, cfg)),
                    "stop_order_id": None}
        log.info("Posición %s encontrada en el broker; añadida", sym)
        if not dry_run and not db.KV(con).get(f"adopted:{sym}"):
            db.KV(con).set(f"adopted:{sym}", now.isoformat())
            _notify(f"ℹ️ He encontrado {e(_name(cfg, sym))} en tu cuenta (comprada fuera del bot): la "
                    "sigo y te propondré venderla si toca, pero no le pongo ningún stop en el broker sin tu "
                    "permiso.")
    for sym, pos in out.items():  # máximo desde la entrada (trailing stop)
        if sym in mk:
            pos["high"] = max(pos.get("high") or 0.0, mk[sym]["price"])
    if not dry_run and out:
        with con:
            con.executemany(
                "INSERT OR REPLACE INTO positions(symbol, qty, avg_entry, high, opened, stop_price, "
                "stop_order_id) VALUES (?,?,?,?,?,?,?)",
                [(s, p["qty"], p["avg_entry"], p["high"], p["opened"], p["stop_price"], p["stop_order_id"])
                 for s, p in out.items()])
    return out


def _decide(cfg: dict, m: dict, c: dict, pos: dict | None, cash: float, kill: bool) -> Decision:
    """fusion.decide para un valor. El stop-loss es el de la posición (stop_price, por ATR)."""
    f, cfg_x = m["feats"], cfg
    qty, avg = (pos["qty"], pos["avg_entry"] or 0.0) if pos else (0.0, 0.0)
    if pos and pos.get("stop_price") and avg > 0:
        cfg_x = cfg | {"risk": cfg["risk"] | {"stop_loss_pct": 1 - pos["stop_price"] / avg}}
    return decide(news=c["news_ema"], trend=f["trend"], price=m["price"], cash=cash, qty=qty,
                  avg_entry=avg, min_order_eur=cfg["risk"]["min_order_eur"], cfg=cfg_x, fng=0.0,
                  votes=c["votes"], regime=f["regime"], atr_pct=f["atr_pct"], vol=f["vol"],
                  high_since_entry=(pos.get("high") or 0.0) if pos else 0.0, peak=0.0,
                  buy_blocked=kill, intraday=f["intraday"], extra={k: f[k] for k in EXTRA})


def _expire(con: sqlite3.Connection, now: datetime) -> None:
    with con:
        con.execute("UPDATE proposals SET status = 'expired', decided = ? WHERE status = 'proposed' "
                    "AND expires <= ?", (now.isoformat(), now.isoformat()))


def _sold_until(kv: db.KV, cfg: dict, symbol: str) -> datetime | None:
    """Tras vender (o saltar el stop) un valor, no se recompra durante risk.cooldown_minutes."""
    raw = kv.get(f"sold:{symbol}")
    if not raw:
        return None
    return datetime.fromisoformat(raw) + timedelta(minutes=cfg["risk"].get("cooldown_minutes", 0))


def _minutes_since_open(cfg: dict, now: datetime) -> float | None:
    """Minutos desde la apertura de hoy ([session].open, en su zona horaria); None sin [session]."""
    s = cfg.get("session")
    if not s or not s.get("open"):
        return None
    local = now.astimezone(ZoneInfo(s["tz"]))
    h, m = (int(v) for v in s["open"].split(":"))
    return (local - local.replace(hour=h, minute=m, second=0, microsecond=0)).total_seconds() / 60


def _cooldown_until(kv: db.KV, cfg: dict, symbol: str) -> datetime | None:
    """Hasta cuándo no se vuelve a proponer un valor que rechazaste (None si ya puede)."""
    raw = kv.get(f"reject:{symbol}")
    if not raw:
        return None
    return datetime.fromisoformat(raw) + timedelta(hours=cfg["execution"].get("reject_cooldown_hours", 4))


# --- tick ---------------------------------------------------------------------------------------
def _next_minutes(sp: dict, mk: dict, positions: dict, base: float, tp: float = 0.0) -> float:
    """Ciclo variable: más rápido si el mercado se mueve (impulso intradía fuerte) o si una posición está
    cerca de una salida (stop, objetivo `tp` o trailing armado); más lento si está plano y no hay nada
    en cartera. Entre speed.min_minutes y speed.max_minutes."""
    moves = sorted(abs(m["feats"].get("intraday", 0.0)) for m in mk.values()) or [0.0]
    mid = moves[len(moves) // 2]
    near = sp["near_stop_pct"]
    near_exit = any(s in mk and (
        (p.get("stop_price") and mk[s]["price"] <= p["stop_price"] * (1 + near))
        or (tp > 0 and (p.get("avg_entry") or 0) > 0
            and mk[s]["price"] >= p["avg_entry"] * (1 + tp - near / 3))
        or ((p.get("high") or 0) > (p.get("avg_entry") or 0) * (1 + near) > 0))  # en ganancia: vigilar
        for s, p in positions.items())
    if near_exit or mid >= sp["fast_above"]:
        return sp["min_minutes"]
    if mid <= sp["slow_below"] and not positions:
        return sp["max_minutes"]
    return base


def _orphans(con: sqlite3.Connection, cfg: dict, now: datetime) -> None:
    """Órdenes 'pending' de hace más de 5 min: el proceso murió mientras se ejecutaban (Trading 212 no
    tiene id de cliente para buscarlas con seguridad). La posición real la recoge el sync; se avisa."""
    cut = (now - timedelta(minutes=5)).isoformat()
    rows = con.execute("SELECT * FROM orders WHERE status = 'pending' AND created < ?", (cut,)).fetchall()
    for r in rows:
        with con:
            con.execute("UPDATE orders SET status = 'unknown' WHERE id = ?", (r["id"],))
            con.execute("UPDATE proposals SET status = 'failed', result = 'resultado desconocido (reinicio)' "
                        "WHERE status = 'accepted' AND symbol = ?", (r["symbol"],))
        verb = "compra" if r["side"] == "buy" else "venta"
        _notify(f"⚠️ Una orden de {verb} de {e(_name(cfg, r['symbol']))} ({r['created'][11:16]}) quedó sin "
                "confirmar porque el bot se reinició. Revisa Trading 212: la posición real la recojo yo en "
                "este ciclo.")


WIND_DOWN_REASON = "cierre de la cartera: vendo para pasar el dinero a EE. UU."


def _wind_down_due(cfg: dict, now: datetime) -> bool:
    """Cartera en cierre (general.wind_down): propone vender sus posiciones a partir de la hora
    general.wind_down_after ("HH:MM", hora local; por defecto en cuanto abre). Las líneas de Xetra de
    valores de EE. UU. apenas tienen liquidez hasta que abre Nueva York: mejor vender después."""
    after = str(cfg["general"].get("wind_down_after") or "00:00")
    hh, mm = (int(x) for x in after.split(":"))
    return (now.hour, now.minute) >= (hh, mm)


def budget_cash(cfg: dict, con: sqlite3.Connection, save: bool = True) -> float | None:
    """Efectivo propio de la cartera cuando varias comparten la cuenta de Trading 212
    (portfolio.budget_eur): un libro en el KV "budget:cash:<modo>" (papel y live por separado) que
    empieza en el presupuesto menos lo que ya tiene invertido (a precio de entrada) y luego suma
    ventas y resta compras, comisiones
    incluidas; así las ganancias se reinvierten en la misma cartera. Si cambias el presupuesto en el
    config, la diferencia se suma o se resta. None sin presupuesto."""
    budget = (cfg.get("portfolio") or {}).get("budget_eur")
    if not budget:
        return None
    kv, mode = db.KV(con), cfg["general"]["mode"]
    raw, base = kv.get(f"budget:cash:{mode}"), kv.get(f"budget:eur:{mode}")
    if raw is None:
        invested = sum(r["qty"] * (r["avg_entry"] or 0.0) for r in db.open_positions(con))
        cash = budget - invested
    else:
        cash = float(raw) + budget - float(base or budget)
    if save and (raw is None or float(base or 0) != budget):
        kv.set(f"budget:cash:{mode}", repr(cash))
        kv.set(f"budget:eur:{mode}", repr(float(budget)))
    return cash


def _budget_move(cfg: dict, con: sqlite3.Connection, delta: float) -> None:
    """Apunta en el libro del presupuesto una compra (delta < 0) o una venta (delta > 0)."""
    if (cash := budget_cash(cfg, con)) is not None:
        db.KV(con).set(f"budget:cash:{cfg['general']['mode']}", repr(cash + delta))


def _own_cash(cfg: dict, con: sqlite3.Connection, broker_cash: float, save: bool = True) -> float:
    """El efectivo que puede usar la cartera: el de su presupuesto, nunca más que el de la cuenta."""
    own = budget_cash(cfg, con, save)
    return broker_cash if own is None else max(0.0, min(broker_cash, own))


def broker_balance(cfg: dict, con: sqlite3.Connection, now: datetime, max_age: float = 60,
                   broker=None) -> dict:
    """Saldo del broker (efectivo, nº de posiciones y hora de lectura), con caché en KV de `max_age`
    segundos. Nunca lanza: si falla, {"error": ...}. Solo lee; no toca equity_daily ni el kill-switch."""
    kv = db.KV(con)
    ts = float(kv.get("broker:ts") or 0)
    if now.timestamp() - ts > max_age:
        try:
            cash, held = (broker or make_broker(cfg, con)).snapshot()
            value = sum(float(b.get("qty") or 0) * float(b.get("price") or b.get("avg") or 0)
                        for b in held.values())
            kv.set("broker:balance", json.dumps({"cash": cash, "positions": len(held), "value": value}))
            kv.delete("broker:error")
        except BaseException as exc:  # incluye SystemExit (sin claves): /estado nunca debe romperse
            kv.set("broker:error", f"{type(exc).__name__}: {exc}"[:200])
        kv.set("broker:ts", str(now.timestamp()))
        ts = now.timestamp()
    if err := kv.get("broker:error"):
        return {"error": err, "ts": ts}
    return json.loads(kv.get("broker:balance") or "{}") | {"ts": ts}


def run_tick(cfg: dict, now: datetime | None = None, exchange=None, broker=None,
             dry_run: bool = False) -> list[int]:
    """Un tick de la cartera de bolsa. Devuelve los ids de las propuestas creadas.
    `dry_run`: calcula e imprime el ranking y lo que propondría, sin escribir nada ni operar."""
    now = _now(cfg, now)
    is_open = session.is_open(cfg, now)
    if not is_open and not dry_run:
        log.info("Mercado cerrado: sin tick")
        return []
    with portfolio.active(cfg), _connect(cfg) as con:
        kv = db.KV(con)
        if kv.get("paused") == "1" and not dry_run:
            log.info("Bot en pausa (/reanudar CONFIRMAR para reanudar)")
            return []
        eff, day = _effective(cfg, con), now.date().isoformat()
        if not dry_run:
            _orphans(con, cfg, now)
        ex = exchange or market.make_exchange(cfg["general"]["exchange"], markets=cfg.get("market"))
        mk = _market_data(eff, ex, now)
        _mark_frozen(con, mk, now, kv, dry_run)
        headlines, weights = _headlines(eff, now)
        if not any(m.get("frozen") for m in mk.values()):
            _panic(mk, headlines, now, kv, dry_run, con)
        if not dry_run:
            db.save_headlines(con, day, now.isoformat(), headlines, weights)
            refresh_events(con, eff, now, headlines)
        coins = _sentiments(eff, kv, headlines, weights, list(mk), day, dry_run)
        for x in mk:
            mk[x]["feats"]["events"] = event_score(con, eff, _code(x), now)
            mk[x]["feats"]["finbert"] = coins[x]["local"]
        eff = adaptive.apply(eff, kv)  # umbrales de compra ajustados por los últimos resultados
        with lock(cfg):
            return _tick(eff, con, kv, ex, broker, mk, coins, now, dry_run, is_open)


def _tick(cfg: dict, con, kv: db.KV, ex, broker, mk: dict, coins: dict, now: datetime, dry_run: bool,
          is_open: bool) -> list[int]:
    x_cfg, r, st, costs = cfg["execution"], cfg["risk"], cfg["strategy"], cfg["costs"]
    day, now_iso = now.date().isoformat(), now.isoformat()
    if not dry_run:
        _expire(con, now)
        if kv.get("paused") == "1":  # /pausa llegó mientras el tick calculaba
            log.info("Pausa recibida durante el tick: no se registra ni se propone nada")
            return []
    note = ""
    if broker is None:
        try:
            broker = make_broker(cfg, con)
            if not dry_run and kv.get("live:blocked"):
                kv.delete("live:blocked")
        except SystemExit as exc:
            if not dry_run:  # live sin CONFIRM_LIVE o sin claves: se avisa una vez, no en cada tick
                if kv.get("live:blocked") != str(exc):
                    kv.set("live:blocked", str(exc))
                    _notify(f"⚠️ {e(exc)}. No analizo ni propongo nada hasta que esté resuelto.")
                raise
            broker, note = StocksPaperBroker(cfg, con), " (sin claves del broker: efectivo de papel)"
    cash, held = broker.snapshot()
    cash = _own_cash(cfg, con, cash, not dry_run)  # varias carteras en una cuenta: solo su presupuesto
    positions = _sync(con, cfg, held, mk, now, dry_run, broker)
    if not dry_run and cfg["execution"].get("broker_stop"):
        _ensure_stops(con, cfg, kv, broker, positions, now)

    def price_of(sym: str) -> float:
        if sym in mk:
            return mk[sym]["price"]
        b = held.get(sym) or {}
        return float(b.get("price") or positions[sym]["avg_entry"] or 0.0)

    equity = cash + sum(pos["qty"] * price_of(s) for s, pos in positions.items())
    cfg = sizing.with_tier(cfg, equity)  # límites según el capital (si el config tiene niveles)
    r = cfg["risk"]
    if not dry_run and cfg.get("_tier") and kv.get("tier") != cfg["_tier"]:
        if kv.get("tier"):
            _notify(f"📊 Cambio de nivel por capital ({equity:.2f} €): {e(cfg['_tier'])}. Ajusto posiciones, "
                    "tamaños y objetivos.")
        kv.set("tier", cfg["_tier"])
    peak = max(float(kv.get("peak") or equity), equity)
    drawdown = 1 - equity / peak if peak > 0 else 0.0
    kill_now = drawdown >= r["max_drawdown_pct"]
    kill = kv.get("kill_switch") == "1" or kill_now
    winding = bool(cfg["general"].get("wind_down"))
    if winding and not positions and not dry_run and kv.get("wind:done") != "1":
        kv.set("wind:done", "1")
        _notify(f"📦 Esta cartera ya está vacía ({cash:.2f} € en efectivo). Todo su dinero queda libre en la "
                "cuenta para EE. UU.; ya se puede apagar (general.enabled = false).")

    ds = {x: _decide(cfg, mk[x], coins[x], positions.get(x), cash, kill) for x in mk}
    actions, reasons = {x: "HOLD" for x in mk}, {x: ds[x].reason for x in mk}
    pending = db.pending_proposals(con, now_iso)
    pend = {(q["symbol"], q["side"]) for q in pending}
    plans: list[dict] = []

    # --- salidas: stop-loss (urgente), trailing o señal de venta -------------------------------
    for sym, pos in positions.items():
        if sym not in mk:
            continue
        d, m = ds[sym], mk[sym]
        if not m["today"]:
            reasons[sym] = "mercado cerrado hoy para este valor"
            continue
        tp, avg = r.get("take_profit_pct", 0.0), pos.get("avg_entry") or 0.0
        selling = d.action == "SELL" and d.target_exposure == 0
        if winding and not selling and _wind_down_due(cfg, now):
            d = replace(d, action="SELL", target_exposure=0.0, reason=WIND_DOWN_REASON)
            selling = True
        if tp > 0 and avg > 0 and m["price"] >= avg * (1 + tp) and not selling:
            gain = pos["qty"] * (m["price"] - avg)
            d = replace(d, action="SELL", target_exposure=0.0,
                        reason=f"objetivo de beneficio: +{m['price'] / avg - 1:.1%} (≈ {gain:+.2f} €)")
        if not (d.action == "SELL" and d.target_exposure == 0):
            reasons[sym] = f"en cartera: {d.reason}"
            continue
        urgent = "stop" in d.reason
        until = None if urgent else _cooldown_until(kv, cfg, sym)
        if (sym, "sell") in pend:
            reasons[sym] = f"{d.reason}; venta ya propuesta, esperando tu respuesta"
        elif until and now < until:
            reasons[sym] = f"{d.reason}; rechazaste venderla (no insisto hasta las {until:%H:%M})"
        elif not is_open:
            reasons[sym] = f"{d.reason}; mercado cerrado"
        else:
            actions[sym] = "SELL"
            plans.append({"symbol": sym, "side": "sell", "qty": pos["qty"], "eur": pos["qty"] * m["price"],
                          "price": m["price"], "stop": pos.get("stop_price"), "urgent": urgent,
                          "reason": f"{d.reason} | {_drivers(d.contributions)}", "signal": d.signal,
                          "thought": insight.sell_thought(cfg, pos, m["price"], d.reason)})

    # --- entradas: el mejor candidato que pase todos los filtros --------------------------------
    pend_buys = [q for q in pending if q["side"] == "buy"]
    live_pos = [s for s, pos in positions.items() if pos["qty"] * price_of(s) >= r["min_order_eur"]]
    held_groups = [_group(cfg, s) for s in live_pos] + [_group(cfg, q["symbol"]) for q in pend_buys]
    free_cash = cash - sum(q["eur"] or 0.0 for q in pend_buys)
    today_n = con.execute("SELECT count(*) FROM proposals WHERE substr(created, 1, 10) = ? AND side = 'buy'",
                          (day,)).fetchone()[0]  # el cupo es de compras: las ventas nunca se bloquean
    opened_min = _minutes_since_open(cfg, now)
    calm = st.get("no_buy_first_minutes", 0)
    floor = st.get("min_market_intraday")  # filtro de mercado (lo propone la autopsia de pérdidas)
    market_now = median(m["feats"]["intraday"] for m in mk.values()) if mk else 0.0
    candidates = []
    for sym in mk:
        if sym in positions:
            continue
        d, f = ds[sym], mk[sym]["feats"]
        buy_th = st["buy_threshold_bear"] if f["regime"] == "bear" else st["buy_threshold"]
        until = _cooldown_until(kv, cfg, sym)
        sold = _sold_until(kv, cfg, sym)
        if not mk[sym]["today"]:
            reasons[sym] = "mercado cerrado hoy para este valor"
        elif mk[sym].get("frozen") and d.signal >= buy_th:
            reasons[sym] = "precios congelados (la fuente no se mueve): no compro hasta que vuelva"
        elif d.signal < buy_th:
            continue  # el motivo de decide (señal neutra / de venta, umbral bajista)
        elif winding:
            reasons[sym] = "cartera en cierre (el dinero pasa a otra cartera): no compro"
        elif kill:
            reasons[sym] = f"kill-switch (drawdown {drawdown:.0%}): solo vender/mantener"
        elif sold and now < sold:
            reasons[sym] = f"la vendí hace poco (no la recompro hasta las {sold:%H:%M})"
        elif opened_min is not None and opened_min < calm:
            reasons[sym] = f"primeros {calm} min tras la apertura: precio errático, espero"
        elif floor is not None and market_now < floor:
            reasons[sym] = f"el mercado cae (intradía mediano {market_now:+.2f} < {floor:+.2f}): no compro"
        elif not risk_sizing.has_edge(f["atr_pct"], d.signal, costs["fee_pct"], costs["slippage_pct"],
                                      r["edge_cost_multiple"]):
            reasons[sym] = (f"sin ventaja: movimiento esperado {f['atr_pct'] * min(1, abs(d.signal)):.2%}"
                            " < coste")
        elif (sym, "buy") in pend:
            reasons[sym] = "compra ya propuesta, esperando tu respuesta"
        elif until and now < until:
            reasons[sym] = f"la rechazaste hace poco (no la propongo hasta las {until:%H:%M})"
        elif not is_open:
            reasons[sym] = "mercado cerrado"
        elif today_n >= x_cfg["max_proposals_day"]:
            reasons[sym] = f"máximo de propuestas hoy ({x_cfg['max_proposals_day']})"
        else:
            group = _group(cfg, sym)
            group_eur = (sum(positions[s]["qty"] * price_of(s) for s in live_pos if _group(cfg, s) == group)
                         + sum(q["eur"] or 0.0 for q in pend_buys if _group(cfg, q["symbol"]) == group))
            eur, why = sizing.order_eur(equity, free_cash, d.signal, buy_th, f["atr_pct"], held_groups, group,
                                        group_eur, cfg)
            reasons[sym] = f"candidata: {eur:.2f} € ({why})" if eur > 0 else f"sin tamaño: {why}"
            if eur > 0:
                candidates.append((d.signal, sym, eur, buy_th))
    if candidates:
        signal, sym, eur, buy_th = max(candidates)
        price, d = mk[sym]["price"], ds[sym]
        motive = "señal de compra" + (f" (régimen bajista: umbral {buy_th})"
                                      if mk[sym]["feats"]["regime"] == "bear" else "")
        actions[sym] = "BUY"
        plan = {"symbol": sym, "side": "buy", "qty": eur / price, "eur": eur, "price": price,
                "stop": price * (1 - sizing.stop_pct(mk[sym]["feats"]["atr_pct"], cfg)),
                "urgent": False, "reason": f"{motive} | {_drivers(d.contributions)}", "signal": signal}
        plans.append(plan | {"thought": insight.buy_thought(cfg, plan, _drivers(d.contributions, 2),
                                                             mk[sym]["feats"])})

    ranking = sorted(mk, key=lambda s: ds[s].signal, reverse=True)
    thought = insight.thinking(cfg, mk, ds, reasons, positions, plans, ranking, cash, equity, is_open,
                               st["buy_threshold"], NAMES)
    if not plans:  # sin compras: qué hacer con el efectivo parado (en /pensamiento y un aviso al día)
        try:
            idle = insight.idle_cash(con, cfg, mk, ds, ranking, positions, cash, equity,
                                     st["buy_threshold"], now)
        except Exception as exc:  # el razonamiento no debe romper el tick
            log.warning("Razonamiento del efectivo no disponible (%s)", type(exc).__name__)
            idle = []
        thought += idle
        if idle and not dry_run and is_open:
            _idle_note(cfg, kv, idle, now)
    thought += _panic_followup(kv, mk)
    if not dry_run and (sp := cfg.get("speed")):
        kv.set("tick:next_minutes", str(_next_minutes(sp, mk, positions, cfg["general"]["tick_minutes"],
                                                      r.get("take_profit_pct", 0.0))))
    if dry_run:
        _print_dry(cfg, mk, ds, actions, reasons, ranking, plans, cash, equity, positions, is_open, note)
        print("Pensamiento:\n  " + "\n  ".join(thought))
        return []
    kv.set("thinking", json.dumps({"ts": now_iso, "lines": thought}, ensure_ascii=False))

    # --- registro: ticks (aprendizaje), capital, kill-switch, EMA de noticias -------------------
    db.save_ticks(con, now, [{
        "symbol": s, "price": mk[s]["price"], "signal": ds[s].signal, "action": actions[s],
        "reason": reasons[s], "held": s in positions, "contributions": ds[s].contributions,
        "raw": {"news": coins[s]["news_ema"], "trend": mk[s]["feats"]["trend"],
                "intraday": mk[s]["feats"]["intraday"], "fng": 0.0, "votes": coins[s]["votes"],
                **{k: mk[s]["feats"][k] for k in EXTRA},
                "_regime": mk[s]["feats"]["regime"], "_atr": mk[s]["feats"]["atr_pct"]},
    } for s in mk])
    kv.set("peak", repr(peak))
    ema = json.loads(kv.get("news_ema", "{}")) | {
        s: [day, coins[s]["news_ema"], coins[s]["prev_ema"]] for s in coins}
    kv.set("news_ema", json.dumps(ema))
    if kill_now and kv.get("kill_switch") != "1":
        kv.set("kill_switch", "1")
        _notify(f"🛑 Kill-switch: drawdown {drawdown:.0%} ≥ {r['max_drawdown_pct']:.0%}. No propongo compras "
                f"(solo ventas) hasta {portfolio.cmd('reanudar', 'CONFIRMAR')}.")
    with con:
        con.execute("INSERT OR REPLACE INTO equity_daily(day, cash, qty, avg_entry, price, equity, peak, "
                    "high_since_entry, symbol) VALUES (?,?,?,?,?,?,?,?,?)",
                    (day, cash, 0.0, 0.0, 0.0, equity, peak, 0.0, None))

    # --- propuestas -----------------------------------------------------------------------------
    ids = [_propose(con, cfg, now, plan) for plan in plans]
    first_today = not db.has_decision(con, day)
    if ids or first_today:
        last = plans[-1] if plans else None
        sym = last["symbol"] if last else ranking[0]
        _record(con, cfg, now, sym, ("BUY" if last["side"] == "buy" else "SELL") if last else "HOLD",
                ds[sym].signal, (last["eur"] if last["side"] == "buy" else -last["eur"]) if last else 0.0,
                mk[sym]["price"], (f"propuesta #{ids[-1]}: " + last["reason"]) if last else reasons[sym],
                equity, {"proposals": ids, "cash": cash, "equity": equity, "peak": peak,
                         "positions": {s: {k: pos[k] for k in ("qty", "avg_entry", "stop_price")}
                                       for s, pos in positions.items()},
                         "coins": {s: {"price": mk[s]["price"], "signal": ds[s].signal, "action": actions[s],
                                       "reason": reasons[s], "contributions": ds[s].contributions,
                                       "features": mk[s]["feats"], "news_ema": coins[s]["news_ema"],
                                       "sentiment": asdict(coins[s]["sent"]),
                                       "headlines_hash": sentiment.headlines_hash(
                                           [h for h, _ in coins[s]["pairs"]])} for s in mk}},
                coins[sym]["news_ema"])
    log.info("Tick %s | capital %.2f € (efectivo %.2f €) | %s | %s", now.strftime("%H:%M"), equity, cash,
             ", ".join(f"{_code(s)} {ds[s].signal:+.2f}" for s in ranking[:5]),
             "; ".join(f"{q['side']} {_code(q['symbol'])} #{i}" for q, i in zip(plans, ids, strict=True))
             or "sin propuestas")
    if ids and not x_cfg.get("confirm", True) and not _is_live(cfg):  # solo pruebas en papel
        for i in ids:
            _send(confirm(cfg, i, True, now, exchange=ex, broker=broker))
    return ids


def _propose(con: sqlite3.Connection, cfg: dict, now: datetime, plan: dict) -> int:
    """Guarda la propuesta (caduca a los proposal_minutes o al cierre) y la envía con sus botones."""
    expires = now + timedelta(minutes=cfg["execution"]["proposal_minutes"])
    close = _close_at(cfg, now)
    if close and now < close < expires:
        expires = close
    with con:
        pid = con.execute(
            "INSERT INTO proposals(created, expires, symbol, side, eur, qty, ref_price, stop_price, reason, "
            "signal, urgent, thought) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (now.isoformat(), expires.isoformat(), plan["symbol"], plan["side"], plan["eur"], plan["qty"],
             plan["price"], plan["stop"], plan["reason"], plan["signal"], int(plan["urgent"]),
             plan.get("thought"))).lastrowid
    row = con.execute("SELECT * FROM proposals WHERE id = ?", (pid,)).fetchone()
    if not (cfg["execution"].get("confirm", True) or _is_live(cfg)):
        return pid  # ejecución directa (pruebas en papel): no hace falta preguntar
    msg_id = _send(proposal_text(row, cfg), proposal_markup(row, cfg))
    if msg_id is not None:  # sent_at: un "sí" escrito antes de este momento nunca la confirma
        with con:
            con.execute("UPDATE proposals SET msg_id = ?, sent_at = ? WHERE id = ?",
                        (msg_id, datetime.now(UTC).isoformat(), pid))
    return pid


def _record(con: sqlite3.Connection, cfg: dict, now: datetime, sym: str, action: str, signal: float,
            order_eur: float, price: float, reason: str, equity: float, inputs: dict,
            news_ema: float | None) -> None:
    """Fila del día en decisions (la última propuesta u operación); la anterior va al archivo."""
    day = now.date().isoformat()
    if db.has_decision(con, day):
        db.archive_decision(con, day, now.isoformat())
    with con:
        con.execute(
            "INSERT OR REPLACE INTO decisions(day, created, mode, action, signal, target_exposure, "
            "order_eur, price, reason, inputs_json, config_hash, news_ema, symbol) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (day, now.isoformat(), cfg["general"]["mode"], action, signal,
             abs(order_eur) / equity if equity > 0 else 0.0, order_eur, price, reason,
             json.dumps(inputs | {"params_id": cfg.get("_params_id")}, ensure_ascii=False, default=str),
             cfg.get("_hash"), news_ema, sym))


def _print_dry(cfg, mk, ds, actions, reasons, ranking, plans, cash, equity, positions, is_open, note) -> None:
    lines = [f"[DRY-RUN] {portfolio.label(cfg) or portfolio.word(cfg)} · capital {equity:.2f} € · efectivo "
             f"{cash:.2f} €{note} · {len(positions)} posiciones"
             + ("" if is_open else " · mercado cerrado: no propondría nada")]
    for s, pos in positions.items():
        lines.append(f"  posición {_code(s)} {_qty(pos['qty'])} · entrada {pos['avg_entry']:.2f} € · stop "
                     f"{pos['stop_price'] or 0:.2f} €")
    for i, s in enumerate(ranking, 1):
        f = mk[s]["feats"]
        lines.append(f"{i:2d}. {_code(s):6s} {ds[s].signal:+.3f} {actions[s]:4s} {mk[s]['price']:.2f} € · "
                     f"{f['regime']} · ATR {f['atr_pct']:.2%} · {_drivers(ds[s].contributions)} "
                     f"— {reasons[s]}")
    for q in plans:
        verb = "compra" if q["side"] == "buy" else "venta" + (" URGENTE (stop-loss)" if q["urgent"] else "")
        lines.append(f"Propondría: {verb} {_code(q['symbol'])} {q['eur']:.2f} € (≈ {_qty(q['qty'])} a "
                     f"{q['price']:.2f} €)" + (f", stop {q['stop']:.2f} €" if q["stop"] else "")
                     + f" — {q['reason']}")
    if not plans:
        lines.append("Nada que proponer.")
    print("\n".join(lines))


# --- confirmación -------------------------------------------------------------------------------
def _close(con: sqlite3.Connection, pid: int, status: str, now: datetime, result: str) -> None:
    with con:
        con.execute("UPDATE proposals SET status = ?, decided = ?, result = ? WHERE id = ?",
                    (status, now.isoformat(), result, pid))


def confirm(cfg: dict, proposal_id: int, accept: bool, now: datetime | None = None, exchange=None,
            broker=None) -> str:
    """Tu respuesta a una propuesta (botón o "sí"/"no"). Devuelve el resultado en HTML."""
    now = _now(cfg, now)
    with lock(cfg), _connect(cfg) as con:
        row = con.execute("SELECT * FROM proposals WHERE id = ?", (proposal_id,)).fetchone()
        if row is None:
            return f"No existe la propuesta #{int(proposal_id)}."
        pid, name, buy = row["id"], e(_name(cfg, row["symbol"])), row["side"] == "buy"
        if row["status"] != "proposed":
            status = STATUS.get(row["status"], row["status"])
            return f"Ya respondida: la propuesta #{pid} ({name}) está {status}."
        if datetime.fromisoformat(row["expires"]) <= now:
            _close(con, pid, "expired", now, "caducada")
            return (f"⌛ La propuesta #{pid} ({name}) caducó a las {_hhmm(cfg, row['expires'])}: no hago "
                    "nada. Si sigue teniendo sentido, llegará otra.")
        if not accept:
            _close(con, pid, "rejected", now, "rechazada")
            db.KV(con).set(f"reject:{row['symbol']}", now.isoformat())
            hours = cfg["execution"].get("reject_cooldown_hours", 4)
            return (f"❌ Descartada: no {'compro' if buy else 'vendo'} {name}."
                    + ("" if row["urgent"] else f" No lo vuelvo a proponer en {hours:g} h."))
        kv = db.KV(con)
        if buy and (kv.get("paused") == "1" or kv.get("kill_switch") == "1"):
            why = "bot en pausa" if kv.get("paused") == "1" else "kill-switch activo"
            _close(con, pid, "failed", now, why)
            return f"⛔ No compro {name}: {why} desde que te lo propuse (vender sí se puede)."
        return _execute(cfg, con, row, now, exchange, broker)


def _execute(cfg: dict, con: sqlite3.Connection, row: sqlite3.Row, now: datetime, exchange, broker) -> str:
    x, g = cfg["execution"], cfg["general"]
    pid, sym, buy = row["id"], row["symbol"], row["side"] == "buy"
    name, verb = e(_name(cfg, sym)), "compra" if buy else "venta"
    if not session.is_open(cfg, now):
        _close(con, pid, "failed", now, "mercado cerrado")
        return f"🔒 Mercado cerrado: no ejecuto la {verb} de {name} (propuesta #{pid})."
    ex = exchange or market.make_exchange(g["exchange"], markets=cfg.get("market"))
    try:
        price = market.last_price(ex, sym)
    except Exception as exc:
        _close(con, pid, "failed", now, f"sin precio ({type(exc).__name__})")
        return f"⚠️ No pude leer el precio de {name} ({e(type(exc).__name__)}): no ejecuto la {verb}."
    ref = row["ref_price"] or price
    drift = price / ref - 1
    if buy and abs(drift) > x["price_drift_max"]:
        _close(con, pid, "failed", now, f"deriva del precio {drift:+.2%}")
        return (f"⚠️ El precio de {name} se ha movido {_pct(drift)} ({ref:.2f} → {price:.2f} €), más del "
                f"{x['price_drift_max'] * 100:.1f} % permitido: no ejecuto la {verb}. Llegará otra propuesta "
                "si sigue teniendo sentido.")
    if broker is None:
        try:
            broker = make_broker(cfg, con)
        except SystemExit as exc:  # nunca SystemExit en el hilo de Telegram: lo pararía
            _close(con, pid, "failed", now, str(exc))
            return f"⚠️ {e(exc)}. No ejecuto la {verb} de {name}."
    pos = con.execute("SELECT * FROM positions WHERE symbol = ?", (sym,)).fetchone()
    if buy:
        cash, _ = broker.snapshot()
        cash = _own_cash(cfg, con, cash)
        if cash + 1e-9 < row["eur"]:
            _close(con, pid, "failed", now, f"efectivo insuficiente ({cash:.2f} €)")
            return (f"⚠️ Efectivo insuficiente para comprar {name}: hay {cash:.2f} € y hacen falta "
                    f"{row['eur']:.2f} €. No ejecuto.")
    elif not pos or pos["qty"] <= DUST:
        _close(con, pid, "failed", now, "sin posición")
        return f"⚠️ Ya no tienes {name}: nada que vender."
    cancelled = False
    if not buy and pos["stop_order_id"] and x.get("broker_stop"):
        if not _cancel_stop(broker, pos["stop_order_id"]):
            _close(con, pid, "failed", now, "no pude quitar el stop del broker")
            where = "Trading 212" if _is_live(cfg) else "el broker"
            return (f"⚠️ No pude confirmar que el stop de {name} esté cancelado en {where}: no vendo para "
                    "no duplicar órdenes. El stop sigue protegiéndote; reviso en el próximo tick.")
        cancelled = True
    # 1) propuesta 'accepted' y orden 'pending' ANTES de tocar el broker: nunca se repite
    coid = f"{'t212' if _is_live(cfg) else 'paper'}:{pid}"
    day, now_iso = now.date().isoformat(), now.isoformat()
    with con:
        con.execute("UPDATE proposals SET status = 'accepted', decided = ? WHERE id = ?", (now_iso, pid))
        con.execute("INSERT INTO orders(day, client_order_id, mode, side, created, status, symbol) "
                    "VALUES (?,?,?,?,?, 'pending', ?)", (day, coid, g["mode"], row["side"], now_iso, sym))
    # 2) ejecución (al vender, primero se quita el stop del broker para liberar las acciones)
    error = ""
    try:
        if buy:
            fill = broker.buy(sym, row["eur"], price)
        else:
            fill = broker.sell(sym, pos["qty"], price)
    except Exception as exc:
        log.exception("La orden de %s %s falló", verb, sym)
        fill, error = None, f"{type(exc).__name__}: {exc}"
    with con:
        con.execute("UPDATE orders SET qty=?, price=?, fee=?, eur=?, status=? WHERE client_order_id = ?",
                    (fill.qty, fill.price, fill.fee, fill.eur, "filled", coid) if fill else
                    (0, 0, 0, 0, "nofill", coid))
    if not fill:
        if cancelled:  # la venta no se hizo: el stop vuelve a su sitio
            sid = _place_stop(broker, sym, pos["qty"], pos["stop_price"])
            with con:
                con.execute("UPDATE positions SET stop_order_id = ? WHERE symbol = ?", (sid, sym))
        _close(con, pid, "failed", now, error or "sin llenar")
        where = "Trading 212" if _is_live(cfg) else "el broker"
        return (f"⚠️ La orden de {verb} de {name} no se llenó" + (f" ({e(error)[:200]})" if error else "")
                + f"; revisa {where}.")
    text = (_after_buy(cfg, con, row, pos, fill, broker, now) if buy
            else _after_sell(cfg, con, pos, fill, broker, now, (row["reason"] or "").partition(" | ")[0]))
    _close(con, pid, "executed", now, text)
    _budget_move(cfg, con, -(fill.eur + fill.fee) if buy else fill.eur - fill.fee)
    st = db.last_state(con)  # capital del último tick: se le aplica la orden (sin otra llamada al broker)
    if st:
        cash = st["cash"] - (fill.eur + fill.fee if buy else -(fill.eur - fill.fee))
        equity = st["equity"] - fill.fee  # efectivo <-> acciones al precio de ejecución: solo la comisión
        with con:
            con.execute("INSERT OR REPLACE INTO equity_daily(day, cash, qty, avg_entry, price, equity, peak, "
                        "high_since_entry, symbol) VALUES (?,?,?,?,?,?,?,?,?)",
                        (day, cash, 0.0, 0.0, 0.0, equity, max(st["peak"] or 0.0, equity), 0.0, None))
        db.KV(con).delete("broker:ts")  # el saldo cacheado de /estado ya no vale
        st = db.last_state(con)
    _record(con, cfg, now, sym, "BUY" if buy else "SELL", row["signal"] or 0.0,
            fill.eur if buy else -fill.eur, fill.price, f"propuesta #{pid} ejecutada: {text}",
            st["equity"] if st else 0.0, {"proposal": pid, "fill": asdict(fill)}, None)
    log.info("Propuesta #%s ejecutada: %s %s %.6f a %.4f (%.2f €)", pid, verb, sym, fill.qty, fill.price,
             fill.eur)
    return text


def _ensure_stops(con, cfg: dict, kv: db.KV, broker, positions: dict, now: datetime) -> None:
    """Toda posición lleva su stop en el broker (adoptada, compra sin stop, stop rechazado...)."""
    for sym, pos in positions.items():
        if pos.get("stop_order_id") or not pos.get("stop_price") or pos["qty"] <= DUST \
                or kv.get(f"adopted:{sym}"):  # comprada a mano: ninguna orden sin tu permiso
            continue
        last = kv.get(f"stopretry:{sym}")
        if last and now - datetime.fromisoformat(last) < timedelta(hours=1):
            continue
        kv.set(f"stopretry:{sym}", now.isoformat())
        sid = _place_stop(broker, sym, pos["qty"], pos["stop_price"])
        if sid:
            pos["stop_order_id"] = sid
            with con:
                con.execute("UPDATE positions SET stop_order_id = ? WHERE symbol = ?", (sid, sym))
            kv.delete(f"stopretry:{sym}")
        else:
            _notify(f"⚠️ No pude poner el stop de {e(_name(cfg, sym))} en el broker "
                    f"({pos['stop_price']:.2f} €): lo vigilo yo en cada tick y lo reintento en 1 h.")


def _place_stop(broker, sym: str, qty: float, stop: float | None) -> str | None:
    if not stop or qty <= DUST:
        return None
    try:
        return broker.place_stop(sym, qty, stop)
    except Exception as exc:
        log.warning("No se pudo poner el stop de %s (%s)", sym, type(exc).__name__)
        return None


def _cancel_stop(broker, order_id: str) -> bool:
    try:
        broker.cancel_stop(order_id)
        return True
    except Exception as exc:
        log.warning("No se pudo cancelar el stop %s (%s)", order_id, type(exc).__name__)
        return False


def _after_buy(cfg, con, row, pos, fill, broker, now) -> str:
    """Actualiza la posición (precio medio, máximo, stop) y pone el stop en el broker."""
    q0, a0 = (pos["qty"], pos["avg_entry"] or 0.0) if pos else (0.0, 0.0)
    q1 = q0 + fill.qty
    avg = (q0 * a0 + fill.qty * fill.price) / q1
    sp = (1 - row["stop_price"] / row["ref_price"] if row["stop_price"] and row["ref_price"]
          else cfg["risk"]["stop_loss_pct"])
    stop, sid, warn = avg * (1 - sp), None, ""
    if cfg["execution"].get("broker_stop"):
        if pos and pos["stop_order_id"]:
            _cancel_stop(broker, pos["stop_order_id"])
        sid = _place_stop(broker, row["symbol"], q1, stop)
        if sid is None:
            warn = "\n⚠️ No pude dejar el stop puesto en el broker: lo vigilo yo en cada tick."
    db.KV(con).delete(f"adopted:{row['symbol']}")
    with con:
        con.execute("INSERT OR REPLACE INTO positions(symbol, qty, avg_entry, high, opened, stop_price, "
                    "stop_order_id) VALUES (?,?,?,?,?,?,?)",
                    (row["symbol"], q1, avg, max(pos["high"] or 0.0, fill.price) if pos else fill.price,
                     pos["opened"] if pos else now.isoformat(), stop, sid))
    return (f"✅ Comprado {_qty(fill.qty)} {e(_code(row['symbol']))} a {fill.price:.2f} € ({fill.eur:.2f} €)"
            f" · stop en {stop:.2f} €" + warn)


def _closed_trade(con, cfg: dict, sym: str, ret: float, now: datetime) -> None:
    """Apunta la rentabilidad de una operación cerrada; si el umbral adaptativo cambia, avisa."""
    try:
        if msg := adaptive.record(con, cfg, sym, ret, now):
            _notify(msg)
    except Exception as exc:
        log.warning("Umbral adaptativo no actualizado (%s)", type(exc).__name__)


def _postmortem_async(cfg: dict, sym: str, opened, avg: float, price: float, qty: float, fees: float,
                      why: str, now: datetime) -> None:
    """Autopsia de la pérdida en un hilo aparte (Claude tarda unos segundos: no frena la venta ni el
    tick). Envía el diagnóstico y la mejora con ✅ Aplicar / ❌ No. Nunca lanza."""
    def work():
        try:
            from newsbot.stocks import postmortem
            with portfolio.active(cfg), _connect(cfg) as con:
                out = postmortem.run(con, cfg, sym, opened, avg, price, qty, fees, why, now)
            _send(postmortem_text(out), postmortem_markup(cfg, out["pid"]))
        except Exception as exc:
            log.warning("Autopsia de %s no disponible (%s)", sym, type(exc).__name__)
    if os.environ.get("NEWSBOT_SYNC_POSTMORTEM"):  # pruebas: en el mismo hilo
        work()
    else:
        threading.Thread(target=work, name=f"autopsia-{_code(sym)}", daemon=True).start()


def postmortem_text(out: dict) -> str:
    f = out["facts"]
    pnl, ret = _signed(f"{f['pnl_eur']:+.2f} €"), _signed(f"{f['ret_pct']:+.1f} %")
    head = f"🔬 <b>Autopsia de la pérdida</b> · {e(f['symbol'])} {pnl} ({ret}) en {f['hours_held']:g} h"
    lines = [head, f"<b>Por qué compré:</b> {e(f['buy_thought'] or f['buy_reason'] or 'sin registro')}",
             f"<b>Qué pasó:</b> {e(out['diagnosis'])}"]
    if out.get("lesson"):
        lines.append(f"<b>Lección:</b> {e(out['lesson'])}")
    if out["pid"]:
        lines.append(f"<b>Mejora propuesta</b> ({e(out['by'])}): {e(out['change'])}. ¿La aplico?")
    else:
        lines.append(f"<b>Mejora:</b> {e(out['change'])}.")
    return "\n".join(lines)


def postmortem_markup(cfg: dict, pid: int | None) -> dict | None:
    """✅ Aplicar / ❌ No de una propuesta de parámetros (callback "m:<palabra>:<id>:y|n")."""
    if not pid:
        return None
    w = "" if portfolio.word(cfg) == portfolio.MAIN else portfolio.word(cfg)
    return {"inline_keyboard": [[{"text": "✅ Aplicar mejora", "callback_data": f"m:{w}:{pid}:y"},
                                 {"text": "❌ No", "callback_data": f"m:{w}:{pid}:n"}]]}


def _loss_text(con, cfg, sym, opened, avg, price, qty, fees, why, now) -> str:
    """"🧐 Por qué perdí" (nunca rompe la venta: si falla, nada)."""
    try:
        lines = insight.loss_review(con, cfg, sym, opened, avg, price, qty, fees, why, now, NAMES)
    except Exception as exc:
        log.warning("Repaso de la pérdida de %s no disponible (%s)", sym, type(exc).__name__)
        return ""
    return "\n\n🧐 <b>Por qué perdí</b>\n" + "\n".join(f"• {e(ln)}" for ln in lines)


def _after_sell(cfg, con, pos, fill, broker, now, why: str = "") -> str:
    """Reduce o cierra la posición; si queda algo, vuelve a poner su stop. Con pérdida, la repasa."""
    sym, avg = pos["symbol"], pos["avg_entry"] or 0.0
    left = pos["qty"] - fill.qty
    fee_pct = cfg["costs"]["fee_pct"]
    # neto de verdad: la comisión de esta venta (la del broker o, si no viene, la del config) y la de
    # la compra (el precio medio no la incluye). Así una pérdida de céntimos también se ve y se estudia.
    fees = max(fill.fee, fill.eur * fee_pct) + fill.qty * avg * fee_pct
    pnl = (fill.price - avg) * fill.qty - fees
    with con:
        if left <= DUST:
            con.execute("DELETE FROM positions WHERE symbol = ?", (sym,))
        else:
            sid = (_place_stop(broker, sym, left, pos["stop_price"]) if cfg["execution"].get("broker_stop")
                   else pos["stop_order_id"])
            con.execute("UPDATE positions SET qty = ?, stop_order_id = ? WHERE symbol = ?", (left, sid, sym))
    ret = f" ({_pct(fill.price / avg - 1)})" if avg > 0 else ""
    if left <= DUST:
        db.KV(con).set(f"sold:{sym}", now.isoformat())  # no se recompra enseguida (risk.cooldown_minutes)
        if avg > 0:
            _closed_trade(con, cfg, sym, pnl / (fill.qty * avg), now)
    review = ""
    if pnl < 0:
        review = _loss_text(con, cfg, sym, pos["opened"], avg, fill.price, fill.qty, fees, why or "venta",
                            now)
        _postmortem_async(cfg, sym, pos["opened"], avg, fill.price, fill.qty, fees, why or "venta", now)
    return (f"✅ Vendido {_qty(fill.qty)} {e(_code(sym))} a {fill.price:.2f} € ({fill.eur:.2f} €) · "
            "resultado " + _signed(f"{pnl:+.2f} €") + " neto" + ret + review)


# --- textos para Telegram -----------------------------------------------------------------------
def proposal_text(row, cfg: dict) -> str:
    """Mensaje de la propuesta (HTML, todo lo externo escapado)."""
    sym, buy = row["symbol"], row["side"] == "buy"
    price, eur, qty = row["ref_price"] or 0.0, row["eur"] or 0.0, row["qty"] or 0.0
    motive, sep, comps = (row["reason"] or "").partition(" | ")
    motive, comps = (motive, comps) if sep else ("", motive)
    signal = _signed(f"{row['signal'] or 0.0:+.2f}")
    caduca = f"caduca {_hhmm(cfg, row['expires'])}"
    if buy:
        stop = row["stop_price"]
        lines = [f"🟢 <b>Propuesta de compra</b> · {e(_name(cfg, sym))}",
                 f"{eur:.2f} € ≈ {_qty(qty)} acciones a {price:.2f} €",
                 f"Señal {signal} ({e(comps)})",
                 (f"Stop-loss {stop:.2f} € ({_pct(stop / price - 1)}) · " if stop and price else "") + caduca]
        ask = "¿La compro?"
    else:
        head = ("⚠️ <b>STOP-LOSS</b> · " if row["urgent"] else "") + "🔴 <b>Propuesta de venta</b>"
        lines = [f"{head} · {e(_name(cfg, sym))}",
                 f"{_qty(qty)} acciones a {price:.2f} € ≈ {eur:.2f} €",
                 *([f"Motivo: {e(motive)}"] if motive else []),
                 f"Señal {signal} ({e(comps)})", caduca]
        ask = "¿La vendo?"
    if thought := (row["thought"] if "thought" in row.keys() else None):
        lines.append(f"💭 <i>{e(thought)}</i>")
    return "\n".join([*lines, ask])


def proposal_markup(row, cfg: dict) -> dict:
    """Botones ✅/❌ (callback "o:<palabra>:<id>:y|n"; palabra vacía = cartera principal) y, debajo,
    📈 gráfico en el chat (callback "g:<palabra>:<CÓDIGO>") y el enlace a TradingView."""
    from newsbot.reporting import chart  # diferido: matplotlib solo al dibujar
    w = "" if portfolio.word(cfg) == portfolio.MAIN else portfolio.word(cfg)
    yes = "✅ Comprar" if row["side"] == "buy" else "✅ Vender"
    graph = f"g:{w}:{_code(row['symbol'])}"[:64]
    return {"inline_keyboard": [[{"text": yes, "callback_data": f"o:{w}:{row['id']}:y"},
                                 {"text": "❌ No", "callback_data": f"o:{w}:{row['id']}:n"}],
                                [{"text": "📈 Gráfico", "callback_data": graph},
                                 {"text": "TradingView", "url": chart.tradingview_url(cfg, row["symbol"])}]]}


def positions_lines(con: sqlite3.Connection, cfg: dict, now: datetime) -> list[str]:
    """Líneas HTML para /estado de la bolsa: posiciones, caja y propuestas pendientes."""
    now = _now(cfg, now)
    p, rows = cfg["portfolio"], db.open_positions(con)
    prices = {r["symbol"]: r["price"] for r in con.execute(
        "SELECT symbol, price FROM ticks WHERE id IN (SELECT max(id) FROM ticks WHERE action != 'BACKFILL' "
        "GROUP BY symbol)")}
    lines = [f"<b>Posiciones</b> {len(rows)}/{p['max_positions']}" if rows else "Posiciones: ninguna"]
    for r in rows:
        avg = r["avg_entry"] or 0.0
        px = prices.get(r["symbol"]) or avg
        change = f" ({_pct(px / avg - 1)})" if avg > 0 else ""
        stop = f" · stop {r['stop_price']:.2f} €" if r["stop_price"] else ""
        lines.append(f"• {e(_code(r['symbol']))} {_qty(r['qty'])} · entrada {avg:.2f} € · ahora {px:.2f} €"
                     f"{change} · {r['qty'] * px:.2f} €{stop}")
    if st := db.last_state(con):
        share = f" ({st['cash'] / st['equity']:.0%} del capital)" if st["equity"] else ""
        lines.append(f"Efectivo {st['cash']:.2f} €{share} · mínimo {p['min_cash_pct']:.0%}")
    pending = db.pending_proposals(con, now.isoformat())
    for q in pending:
        verb = "compra" if q["side"] == "buy" else "venta"
        lines.append(f"⏳ Propuesta #{q['id']}: {verb} {e(_code(q['symbol']))} {q['eur'] or 0:.2f} € · "
                     f"caduca {_hhmm(cfg, q['expires'])} · "
                     + ("responde sí/no" if len(pending) == 1 else "pulsa ✅/❌ en su mensaje"))
    return lines
