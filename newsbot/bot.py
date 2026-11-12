"""Punto de entrada: python -m newsbot.bot [opciones]. Sin opciones: la decisión del día."""
import argparse
import json
import logging
import os
import statistics
import sys
import threading
import time
from collections import Counter
from contextlib import closing, contextmanager
from dataclasses import asdict, replace
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path
from zoneinfo import ZoneInfo

import ccxt

from newsbot import config as config_mod
from newsbot import portfolio, sentiment
from newsbot.execution.live import LiveBroker, assert_live_allowed
from newsbot.execution.paper import PaperBroker, execute
from newsbot.features import market, session
from newsbot.news import fng, ingest
from newsbot.news import watch as news_watch
from newsbot.reporting import journal, report, telegram
from newsbot.research import learn, reoptimize
from newsbot.stocks import engine as stocks
from newsbot.storage import db
from newsbot.strategy.fusion import Decision, decide, smooth, track_high

log = logging.getLogger("newsbot")


def _now(cfg: dict, now: datetime | None) -> datetime:
    tz = ZoneInfo(cfg["general"]["timezone"])
    return (now or datetime.now(tz)).astimezone(tz)


def _connect(cfg: dict):
    data_dir = Path(cfg["general"]["data_dir"])
    data_dir.mkdir(parents=True, exist_ok=True)
    return closing(db.connect(data_dir / "newsbot.db"))


def run_once(cfg: dict, exchange=None, now: datetime | None = None, dry_run: bool = False,
             force: bool = False, trigger: str | None = None,
             intraday: bool = False) -> Decision | None:
    """`trigger`: motivo de una decisión adelantada por el vigilante (se guarda en los inputs).
    `intraday`: modo bucle (--loop): puede decidir en cada tick, con los límites de [risk]."""
    now = _now(cfg, now)
    with _connect(cfg) as con:
        pid, override = db.active_params(con, cfg["general"]["mode"])
        try:
            eff = config_mod.apply(cfg, override) | {"_params_id": pid}
        except ValueError as exc:  # sigue con config.toml: stops y ventas tienen que funcionar
            log.error("Parámetros aplicados #%s inválidos (%s); uso config.toml", pid, exc)
            if db.KV(con).get("params:invalid") != str(pid):
                db.KV(con).set("params:invalid", str(pid))
                telegram.send(f"⚠️ Los parámetros aplicados #{pid} ya no son válidos ({exc}). "
                              "Sigo con config.toml; revisa /propuestas.")
            eff = cfg | {"_params_id": None}
        return _run(eff, con, exchange, now, dry_run, force, trigger, intraday)


def loop(cfg: dict, iterations: int | None = None, sleep=time.sleep, extras=()) -> None:
    """Bucle continuo (Docker / portátil): cada `general.tick_minutes` lee noticias y precios y
    decide. Un error en un tick no para el bucle; se avisa por Telegram una vez por error distinto.
    `extras`: carteras de general.portfolios (bolsa), cada una en su hilo: si una se cuelga, el
    latido de cripto sigue y el vigilante no mata el proceso a mitad de una orden."""
    period = cfg["general"]["tick_minutes"] * 60
    log.info("Bucle iniciado: un tick cada %s min (modo %s)", period // 60, cfg["general"]["mode"])
    with _connect(cfg):
        pass  # migraciones antes de arrancar el hilo de Telegram, para que no compitan
    telegram.register_commands()  # menú de comandos en tu chat
    stop = threading.Event()
    threading.Thread(target=listen_telegram, args=(cfg, stop), daemon=True).start()
    for cx in extras:
        threading.Thread(target=_extra_ticks, args=(cx, stop), daemon=True,
                         name=f"cartera-{portfolio.word(cx)}").start()
    try:
        _ticks(cfg, iterations, sleep, period)
    finally:
        stop.set()


def _ticks(cfg: dict, iterations: int | None, sleep, period: int) -> None:
    done, last_error = 0, None
    while iterations is None or done < iterations:
        start = time.monotonic()
        try:
            with _connect(cfg) as con:  # latido: el vigilante (newsbot.watchdog) lo comprueba
                db.KV(con).set("loop:heartbeat", str(int(time.time())))
        except Exception as exc:
            log.warning("No se pudo escribir el latido (%s)", type(exc).__name__)
        if not cfg["general"].get("enabled", True):  # cartera apagada: el proceso sigue por las demás
            if done == 0:
                log.info("Cartera principal desactivada (general.enabled = false): no analiza ni opera")
            done += 1
            if iterations is None or done < iterations:
                sleep(max(0.0, period - (time.monotonic() - start)))
            continue
        try:
            with single_instance(cfg):
                reconcile(cfg)
                run_once(cfg, intraday=True)
            learn_step(cfg)
            last_error = None
        except SystemExit as exc:  # candado ocupado o modo live bloqueado
            log.warning("Tick omitido: %s", exc)
            msg = f"⚠️ newsbot no opera: {exc}"[:500]
            if "live bloqueado" in str(exc) and msg != last_error:  # faltan claves: avisar una vez
                telegram.send(msg)
                last_error = msg
        except Exception as exc:
            log.exception("Error en el tick")
            msg = f"❌ newsbot error: {type(exc).__name__}: {exc}"[:500]
            if msg != last_error:
                telegram.send(msg)
            last_error = msg
        done += 1
        if iterations is None or done < iterations:
            sleep(max(0.0, period - (time.monotonic() - start)))


def _extra_ticks(cfg: dict, stop: threading.Event, iterations: int | None = None,
                 wait=None) -> None:
    """Hilo de una cartera extra: cada `tick_minutes` su latido y, con el mercado abierto, el mismo
    tick que cripto (o el motor multiposición de bolsa, general.engine = "stocks"); cerrado, solo
    guarda titulares (y busca eventos si toca; sin ticks, sin F&G). Nunca se cae: avisa una vez por
    error distinto, con la etiqueta de la cartera."""
    with portfolio.active(cfg):
        period, done, last_error = cfg["general"]["tick_minutes"] * 60, 0, None
        log.info("Cartera iniciada: un tick cada %s min (modo %s)", period // 60, cfg["general"]["mode"])
        try:
            with _connect(cfg):
                pass  # migraciones de su base de datos
        except Exception as exc:
            log.warning("Migraciones fallaron (%s)", type(exc).__name__)
        while not stop.is_set() and (iterations is None or done < iterations):
            start = time.monotonic()
            try:
                with _connect(cfg) as con:
                    db.KV(con).set("loop:heartbeat", str(int(time.time())))
            except Exception as exc:
                log.warning("No se pudo escribir el latido (%s)", type(exc).__name__)
            try:
                now = _now(cfg, None)
                if session.is_open(cfg, now):
                    with single_instance(cfg):
                        if cfg["general"].get("engine") == "stocks":
                            stocks.run_tick(cfg, now)
                        else:
                            reconcile(cfg)
                            run_once(cfg, intraday=True)
                    learn_step(cfg)
                else:
                    collect_headlines(cfg, now)
                    if cfg["general"].get("engine") == "stocks" and cfg["general"]["mode"] == "live":
                        with _connect(cfg) as con:  # saldo real visible en /estado fuera de horario
                            stocks.broker_balance(cfg, con, now, max_age=3600)
                last_error = None
            except SystemExit as exc:  # candado ocupado o live bloqueado (faltan claves)
                log.warning("Tick omitido: %s", exc)
                msg = f"⚠️ No opero: {exc}"[:500]
                if msg != last_error and "en curso" not in str(exc):  # el candado ocupado es normal
                    telegram.send(msg)
                last_error = msg
            except Exception as exc:
                log.exception("Error en el tick")
                msg = f"❌ newsbot error: {type(exc).__name__}: {exc}"[:500]
                if msg != last_error:
                    telegram.send(msg)
                last_error = msg
            done += 1
            if iterations is None or done < iterations:
                (wait or stop.wait)(max(0.0, _period(cfg, period) - (time.monotonic() - start)))


def _period(cfg: dict, default: float) -> float:
    """Segundos hasta el próximo ciclo: los que pida el motor de bolsa ([speed]) o el fijo."""
    if not cfg.get("speed"):
        return default
    try:
        with _connect(cfg) as con:
            minutes = db.KV(con).get("tick:next_minutes")
        return float(minutes) * 60 if minutes else default
    except Exception:
        return default


def collect_headlines(cfg: dict, now: datetime) -> int:
    """Mercado cerrado: solo guarda los titulares (para la apertura y para learn) y, en la bolsa,
    busca eventos con fecha en ellos si toca (stocks/events.py, con su propio presupuesto)."""
    n, day = cfg["news"], now.date().isoformat()
    headlines = ingest.collect(cfg, now)
    weights = [ingest.weight(h, now, n["source_weights"], n["half_life_hours"]) for h in headlines]
    with _connect(cfg) as con:
        db.save_headlines(con, day, now.isoformat(), headlines, weights)
        if cfg["general"].get("engine") == "stocks":
            stocks.refresh_events(con, cfg, now, headlines)
    log.info("Mercado cerrado: %s titulares guardados, sin tick", len(headlines))
    return len(headlines)


def learn_step(cfg: dict, now: datetime | None = None) -> None:
    """Retroalimentación tras cada tick: barato si no toca (como mucho una propuesta a la semana,
    y en live siempre espera a /aprobar). Nunca rompe el bucle."""
    try:
        now = _now(cfg, now)
        with _connect(cfg) as con:
            msg = learn.maybe_propose(con, cfg, now)
            kv = db.KV(con)
            if kv.get("ticks:pruned") != now.date().isoformat():
                keep = learn.settings(cfg)["lookback_days"] + 2  # + margen para el horizonte 24h
                db.prune_ticks(con, int((now - timedelta(days=keep)).timestamp()))
                kv.set("ticks:pruned", now.date().isoformat())
        if msg:
            log.info(msg)
    except Exception as exc:
        log.warning("Aprendizaje falló (%s)", type(exc).__name__)


def listen_telegram(cfg: dict, stop: threading.Event, wait: int = 10) -> None:
    """Hilo del bucle: long polling de getUpdates, así /status y demás se contestan en cuanto
    llegan, también en mitad de un tick. `wait` corto: si la conexión se queda colgada, un botón
    pulsado en ese hueco llega tarde y Telegram ya no deja contestarlo ("query is too old")."""
    if not (os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID")):
        return
    while not stop.is_set():
        try:
            with _connect(cfg) as con:
                telegram.poll(con, cfg, wait=wait)
        except Exception as exc:  # sin el mensaje de la excepción: la URL lleva el token
            log.warning("Telegram: no se pudieron leer los comandos (%s)", type(exc).__name__)
            stop.wait(3 if isinstance(exc, TimeoutError) else 30)


@contextmanager
def single_instance(cfg: dict, stale_s: int = 1800):
    """Evita que el vigilante y la ejecución diaria se pisen. Un candado de más de 30 min se
    considera abandonado (proceso muerto) y se reutiliza."""
    data_dir = Path(cfg["general"]["data_dir"])
    data_dir.mkdir(parents=True, exist_ok=True)
    lock = data_dir / ".lock"
    try:
        os.close(os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
    except FileExistsError:
        if time.time() - lock.stat().st_mtime < stale_s:
            raise SystemExit("Otra ejecución de newsbot está en curso; salgo sin hacer nada.") from None
        lock.touch()
    try:
        yield
    finally:
        lock.unlink(missing_ok=True)


def watch(cfg: dict, exchange=None, now: datetime | None = None) -> Decision | None:
    """Vigilante (cada 15 min): guarda los titulares y, ante una noticia fuerte, adelanta la
    decisión del día. Nunca más de una operación al día: si ya hubo una, solo guarda titulares."""
    now = _now(cfg, now)
    day, n = now.date().isoformat(), cfg["news"]
    headlines = ingest.collect(cfg, now)
    weights = [ingest.weight(h, now, n["source_weights"], n["half_life_hours"]) for h in headlines]
    with _connect(cfg) as con:
        before = con.total_changes
        db.save_headlines(con, day, now.isoformat(), headlines, weights)
        sources = ", ".join(f"{s} {c}" for s, c in Counter(h.source for h in headlines).items())
        summary = (f"Vigilante: {len(headlines)} titulares en {n['window_hours']} h "
                   f"({con.total_changes - before} nuevos) | {sources or 'sin fuentes'}")
        kv = db.KV(con)
        skip = ("vigilante desactivado" if not cfg["watch"]["enabled"] else
                "bot en pausa" if kv.get("paused") == "1" else
                "ya hubo una operación hoy" if db.orders_today(con, day) else
                "ya hubo un adelanto hoy" if kv.get("watch:triggered") == day else None)
        if skip:
            log.info("%s | no decide: %s", summary, skip)
            return None
        state = db.last_state(con)
        held = state["symbol"] if state and state["qty"] > 0 else None
        hit = news_watch.shock(cfg, now, headlines, weights, held, cache=kv)
        if not hit:
            log.info("%s | noticia fuerte: ninguna (posición: %s)", summary, held or "ninguna")
            return None
        decided = db.has_decision(con, day)
        if decided:  # la decisión de las 09:05 fue HOLD: se archiva y se vuelve a decidir
            db.archive_decision(con, day, now.isoformat())
        kv.set("watch:triggered", day)
    sym, eff = hit
    log.info("Noticia fuerte sobre %s (%+.2f): adelanto la decisión del día", sym, eff)
    telegram.send(f"📰 Noticia fuerte sobre {sym} ({eff:+.2f}): adelanto la decisión del día.")
    return run_once(cfg, exchange, now, force=decided, trigger=f"noticia fuerte {sym} {eff:+.2f}")


def _run(cfg: dict, con, exchange, now: datetime, dry_run: bool, force: bool,
         trigger: str | None = None, intraday: bool = False) -> Decision | None:
    g, s, n, r = cfg["general"], cfg["strategy"], cfg["news"], cfg["risk"]
    day, data_dir, kv = now.date().isoformat(), Path(g["data_dir"]), db.KV(con)
    if not intraday and db.has_decision(con, day) and not (force or dry_run):
        log.info("Ya hay decisión para %s (máx. 1/día). --force solo para depurar.", day)
        return None
    if kv.get("paused") == "1" and not dry_run:
        log.info("Bot en pausa (/reanudar CONFIRMAR para reanudar)")
        return None
    live = g["mode"] == "live"
    keys = assert_live_allowed(cfg, os.environ) if live else {}
    ex = exchange or market.make_exchange(g["exchange"], markets=cfg.get("market"), **keys)
    if live:
        ex.options["operatorId"] = cfg["execution"]["operator_id"]

    # --- datos: mercado de cada moneda, titulares y Fear & Greed ----------------------------
    now_ms = int(now.timestamp() * 1000)
    mk = {}
    for x in g["symbols"]:
        candles = market.closed_candles(ex, x, now_ms)
        short = market.closed_candles(ex, x, now_ms, limit=100, timeframe=s["intraday_timeframe"])
        mk[x] = {"candles": candles, "price": market.last_price(ex, x),
                 "min_order": market.min_order_eur(ex, x, r["min_order_eur"]),
                 "feats": market.features(candles, cfg) | {"intraday": market.intraday_score(
                     [c[4] for c in short], s["ema_fast"], s["ema_slow"])}}
    headlines = ingest.collect(cfg, now)
    weights = [ingest.weight(h, now, n["source_weights"], n["half_life_hours"]) for h in headlines]
    fng_value: int | None = None
    if n.get("fng", True):  # bolsa: news.fng = false (es un índice de cripto)
        try:
            fng_value = fng.fetch()
        except Exception as exc:  # sin índice el componente vale 0, no se para el bot
            log.warning("Fear & Greed falló (%s)", exc)
    fng_signal = fng.contrarian(fng_value) if fng_value is not None else 0.0

    # --- sentimiento de cada moneda, solo con los titulares que la mencionan -----------------
    prev_ema = json.loads(kv.get("news_ema", "{}"))  # {símbolo: [día, ema, ema anterior]}
    coins = {}
    for x in g["symbols"]:
        code = config_mod.asset(x)
        pairs = [(h, w) for h, w in zip(headlines, weights, strict=True)
                 if ingest.mentions(h, n["assets"][code])]
        hs, ws = [h for h, _ in pairs], [w for _, w in pairs]
        sent = sentiment.analyze(hs, cfg, ws, cache=kv, asset=code)
        p = prev_ema.get(x)
        prev = (p[1] if p[0] != day else p[2]) if p else None  # --force no suaviza dos veces hoy
        coins[x] = {"pairs": pairs, "sent": sent, "prev_ema": prev,
                    "news_ema": smooth(prev, sent.score * sent.confidence, s["news_ema_days"]),
                    "votes": ingest.votes_score(hs)}

    # --- estado: efectivo y la única posición abierta (si la hay) ---------------------------
    state = db.last_state(con)
    if live:
        bal = market.with_retry(ex.fetch_balance)
        cash = float((bal.get(g["symbols"][0].split("/")[1]) or {}).get("free") or 0.0)
        owned = {x: float((bal.get(config_mod.asset(x)) or {}).get("total") or 0.0)
                 for x in g["symbols"]}
        # la posición es la moneda con más valor por encima del mínimo de orden; el polvo no cuenta
        held = max((x for x in g["symbols"] if owned[x] * mk[x]["price"] >= mk[x]["min_order"]),
                   key=lambda x: owned[x] * mk[x]["price"], default=None)
        qty = owned[held] if held else 0.0
    else:
        cash, qty = (state["cash"], state["qty"]) if state else (g["initial_capital"], 0.0)
        held = state["symbol"] if state and qty > 0 else None
    same = state is not None and held is not None and state["symbol"] == held
    avg = state["avg_entry"] if same else 0.0
    high = (state["high_since_entry"] or 0.0) if same else 0.0
    equity_before = cash + (qty * mk[held]["price"] if held else 0.0)
    peak = max(state["peak"] if state else equity_before, equity_before)
    # límites anti-comisiones: máx. operaciones al día y enfriamiento tras cada operación
    trades, last_trade = db.orders_today(con, day), db.last_trade_time(con)
    cooldown = trades >= r["max_trades_per_day"] or (
        last_trade is not None and now - last_trade < timedelta(minutes=r["cooldown_minutes"]))

    # --- una decisión por moneda; se ejecuta como mucho una ---------------------------------
    ds = {}
    for x in g["symbols"]:
        m, c, mine = mk[x], coins[x], x == held
        ds[x] = decide(news=c["news_ema"], trend=m["feats"]["trend"], price=m["price"], cash=cash,
                       qty=qty if mine else 0.0, avg_entry=avg if mine else 0.0,
                       min_order_eur=m["min_order"], cfg=cfg, fng=fng_signal, votes=c["votes"],
                       regime=m["feats"]["regime"], atr_pct=m["feats"]["atr_pct"],
                       vol=m["feats"]["vol"], high_since_entry=high if mine else 0.0, peak=peak,
                       buy_blocked=kv.get("kill_switch") == "1",
                       intraday=m["feats"]["intraday"], cooldown=cooldown,
                       extra={k: m["feats"][k] for k in market.SHADOW})
    if cfg.get("session") and hasattr(ex, "trading_today"):  # festivo o valor sin cotizar hoy
        for x in g["symbols"]:
            if not market.with_retry(ex.trading_today, x, now_ms):
                ds[x] = replace(ds[x], action="HOLD", order_eur=0.0,
                                reason="mercado cerrado hoy para este valor")
    if held:  # con posición abierta solo se gestiona esa moneda: vender o mantener
        sym = held
    else:     # sin posición: la compra con mejor señal, si alguna pasa todos los filtros
        buys = [x for x in g["symbols"] if ds[x].action == "BUY"]
        sym = max(buys or g["symbols"], key=lambda x: ds[x].signal)
    d, price, chosen, mine = ds[sym], mk[sym]["price"], coins[sym], sym == held

    top = sorted(chosen["pairs"], key=lambda p: p[1], reverse=True)[:3]
    summary = report.daily(day, g["mode"], sym, d, price, equity_before, g["initial_capital"],
                           [(w, h) for h, w in top], fng_value, [(x, ds[x]) for x in g["symbols"]],
                           held, show_fng=n.get("fng", True))
    if dry_run:
        f = mk[sym]["feats"]
        print("[DRY-RUN] " + summary + f"\nSentimiento {config_mod.asset(sym)}: "
              f"{chosen['sent'].method} | régimen {f['regime']} | ATR {f['atr_pct']:.2%}"
              f" | vol {f['vol']:.0%}")
        return d

    created = now.isoformat()
    seed = day + (now.strftime("T%H%M%S") if force or intraday else "")
    db.save_headlines(con, day, created, headlines, weights)
    db.save_ticks(con, now, [{
        "symbol": x, "price": mk[x]["price"], "signal": ds[x].signal, "action": ds[x].action,
        "reason": ds[x].reason, "held": x == held, "contributions": ds[x].contributions,
        "raw": {"news": coins[x]["news_ema"], "trend": mk[x]["feats"]["trend"],
                "intraday": mk[x]["feats"]["intraday"], "fng": fng_signal,
                "votes": coins[x]["votes"], **{k: mk[x]["feats"][k] for k in market.SHADOW},
                "_regime": mk[x]["feats"]["regime"], "_atr": mk[x]["feats"]["atr_pct"]},
    } for x in g["symbols"]])
    first_today = not db.has_decision(con, day)
    if intraday and d.action == "HOLD" and not first_today:
        # tick sin operación: solo se actualiza el capital (y el máximo para el trailing stop)
        hp = mk[held]["price"] if held else price
        with con:
            con.execute(
                "INSERT OR REPLACE INTO equity_daily(day, cash, qty, avg_entry, price, equity, "
                "peak, high_since_entry, symbol) VALUES (?,?,?,?,?,?,?,?,?)",
                (day, cash, qty, avg, hp, equity_before, peak, track_high(qty, qty, high, hp), held))
        kv.set("news_ema", json.dumps({x: [day, coins[x]["news_ema"], coins[x]["prev_ema"]]
                                       for x in g["symbols"]}))
        log.info("Tick %s | %s | HOLD %s: %s", now.strftime("%H:%M"),
                 ", ".join(f"{config_mod.asset(x)} {ds[x].signal:+.2f}" for x in g["symbols"]),
                 sym, d.reason)
        return d
    if intraday and not first_today:  # la decisión anterior de hoy se conserva en el archivo
        db.archive_decision(con, day, created)
    inputs = {  # todo lo necesario para reproducir la decisión
        "headlines": [{**asdict(h), "published": h.published.isoformat(), "weight": w}
                      for h, w in zip(headlines, weights, strict=True)],
        "symbol": sym, "held_before": held, "fng_value": fng_value, "fng_signal": fng_signal,
        "coins": {x: {
            "features": mk[x]["feats"], "price": mk[x]["price"], "min_order_eur": mk[x]["min_order"],
            "closes": [c[4] for c in mk[x]["candles"][-s["sma_slow"]:]],
            "sentiment": asdict(coins[x]["sent"]), "news_ema": coins[x]["news_ema"],
            "headlines_hash": sentiment.headlines_hash([h for h, _ in coins[x]["pairs"]]),
            "votes": coins[x]["votes"], "signal": ds[x].signal, "action": ds[x].action,
            "reason": ds[x].reason, "contributions": ds[x].contributions,
        } for x in g["symbols"]},
        "state_before": {"cash": cash, "qty": qty, "avg_entry": avg, "peak": peak, "high": high},
        "params_id": cfg.get("_params_id"), "trigger": trigger,
    }
    if kv.get("paused") == "1":  # /pausa llegó mientras el tick calculaba
        log.info("Pausa recibida durante el tick: no se registra ni se ejecuta nada")
        return None
    # 1) Decisión + orden 'pending' ANTES de tocar el exchange: si el proceso muere a mitad, el
    #    guard diario impide repetir y la orden se concilia en la siguiente ejecución.
    with con:
        con.executemany(
            "INSERT INTO sentiment_runs(day, created, method, score, confidence, reason, "
            "headlines_hash, symbol) VALUES (?,?,?,?,?,?,?,?)",
            [(day, created, coins[x]["sent"].method, coins[x]["sent"].score,
              coins[x]["sent"].confidence, coins[x]["sent"].reason,
              inputs["coins"][x]["headlines_hash"], x) for x in g["symbols"]])
        con.execute(
            "INSERT OR REPLACE INTO decisions(day, created, mode, action, signal, target_exposure, "
            "order_eur, price, reason, inputs_json, config_hash, news_ema, symbol) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (day, created, g["mode"], d.action, d.signal, d.target_exposure, d.order_eur, price,
             d.reason, json.dumps(inputs, ensure_ascii=False), cfg["_hash"], chosen["news_ema"],
             sym))
        if d.action != "HOLD":
            con.execute("INSERT INTO orders(day, client_order_id, mode, side, created, status, "
                        "symbol) VALUES (?,?,?,?,?, 'pending', ?)",
                        (day, seed, g["mode"], d.action.lower(), created, sym))
    # 2) Ejecución y conciliación
    if live:
        broker = LiveBroker(ex, sym, cfg, seed, avg if mine else 0.0)
    else:
        broker = PaperBroker(cfg["costs"]["fee_pct"], cfg["costs"]["slippage_pct"], cash,
                             qty if mine else 0.0, avg if mine else 0.0)
    qty_before = broker.qty
    fill = execute(broker, d, price)
    equity = broker.cash + broker.qty * price
    with con:
        if d.action != "HOLD":
            con.execute("UPDATE orders SET qty=?, price=?, fee=?, eur=?, status=? "
                        "WHERE client_order_id = ?",
                        (fill.qty, fill.price, fill.fee, fill.eur, "filled", seed) if fill else
                        (0, 0, 0, 0, "nofill", seed))
        con.execute(
            "INSERT OR REPLACE INTO equity_daily(day, cash, qty, avg_entry, price, equity, peak, "
            "high_since_entry, symbol) VALUES (?,?,?,?,?,?,?,?,?)",
            (day, broker.cash, broker.qty, broker.avg_entry, price, equity, max(peak, equity),
             track_high(qty_before, broker.qty, high if mine else 0.0, price),
             sym if broker.qty > 0 else None))
    kv.set("news_ema", json.dumps({x: [day, coins[x]["news_ema"], coins[x]["prev_ema"]]
                                   for x in g["symbols"]}))
    journal.append(data_dir / "journal.csv", {
        "day": day, "mode": g["mode"], "symbol": sym, "action": d.action,
        "signal": round(d.signal, 4),
        **{k: round(v, 4) for k, v in d.contributions.items()}, "fng_value": fng_value,
        "price": price, "order_eur": round(d.order_eur, 2), "fee": round(fill.fee, 4) if fill else 0,
        "cash": round(broker.cash, 4), "qty": broker.qty, "equity": round(equity, 4),
        "sentiment_method": chosen["sent"].method, "reason": d.reason,
    })

    # 3) Avisos
    print(summary)
    telegram.send(summary)
    if "stop" in d.reason:
        telegram.send(f"⚠️ Stop activado: {d.reason}")
    if d.kill_switch and kv.get("kill_switch") != "1":
        kv.set("kill_switch", "1")
        telegram.send("🛑 Kill-switch: drawdown máximo superado. Solo vender/mantener hasta "
                      "/reanudar CONFIRMAR o `python -m newsbot.bot --resume`.")
    if d.action != "HOLD" and not fill:
        telegram.send("⚠️ La orden no se llenó; revisa el exchange.")
    if reoptimize.due(con, cfg, now):
        try:
            ex_r = market.make_exchange(g["exchange"], markets=cfg.get("market"))
            log.info(reoptimize.run(con, cfg, ex_r, now))
        except Exception as exc:  # nunca rompe la decisión diaria
            log.warning("Reoptimización falló: %s", exc)
    return d


def reconcile(cfg: dict, exchange=None) -> int:
    """Concilia órdenes 'pending' (live) consultando el exchange por su clientOrderId."""
    with _connect(cfg) as con:
        pending = con.execute("SELECT * FROM orders WHERE status = 'pending'").fetchall()
        if not pending or cfg["general"]["mode"] != "live":
            return 0
        g = cfg["general"]
        ex = exchange or market.make_exchange(g["exchange"], **assert_live_allowed(cfg, os.environ))
        ex.options["operatorId"] = cfg["execution"]["operator_id"]
        for row in pending:
            broker = LiveBroker(ex, row["symbol"] or g["symbols"][0], cfg, row["client_order_id"])
            since = int(datetime.fromisoformat(row["created"]).timestamp() * 1000) - 60_000
            fill = broker.find(since)
            with con:
                con.execute("UPDATE orders SET qty=?, price=?, fee=?, eur=?, status=? WHERE id=?",
                            (fill.qty, fill.price, fill.fee, fill.eur, "filled", row["id"]) if fill
                            else (0, 0, 0, 0, "nofill", row["id"]))
            telegram.send(f"Orden {row['day']} conciliada: {'llenada' if fill else 'sin llenar'}")
        return len(pending)


def compare_sentiment(cfg: dict, now: datetime | None = None) -> dict:
    """Ejecuta todos los métodos sobre los mismos titulares y guarda la correlación histórica."""
    now = _now(cfg, now)
    n, day = cfg["news"], now.date().isoformat()
    headlines = ingest.collect(cfg, now)
    weights = [ingest.weight(h, now, n["source_weights"], n["half_life_hours"]) for h in headlines]
    with _connect(cfg) as con:
        kv = db.KV(con)
        results = sentiment.compare(headlines, cfg, weights, cache=kv) if headlines else {}
        with con:
            for name, r in results.items():
                con.execute("INSERT INTO sentiment_runs(day, created, method, score, confidence, "
                            "reason, headlines_hash) VALUES (?,?,?,?,?,?,?)",
                            (day, now.isoformat(), "compare:" + name, r.score, r.confidence,
                             r.reason, sentiment.headlines_hash(headlines)))
        series: dict[str, dict[str, float]] = {}
        for row in con.execute("SELECT day, method, score FROM sentiment_runs "
                               "WHERE method LIKE 'compare:%' ORDER BY id"):
            series.setdefault(row["method"][8:], {})[row["day"]] = row["score"]
        corr = {}
        names = sorted(series)
        for i, a in enumerate(names):
            for b in names[i + 1:]:
                days = sorted(set(series[a]) & set(series[b]))
                xs, ys = [series[a][x] for x in days], [series[b][x] for x in days]
                try:
                    corr[f"{a}~{b}"] = round(statistics.correlation(xs, ys), 3)
                except statistics.StatisticsError:  # < 2 días o serie constante
                    corr[f"{a}~{b}"] = None
        kv.set("compare:last", json.dumps({"day": day, "correlation": corr}))
    print({k: (round(v.score, 3), round(v.confidence, 3)) for k, v in results.items()})
    print("Correlación histórica entre métodos:", corr)
    return corr


def healthcheck(cfg: dict, now: datetime | None = None) -> bool:
    now = _now(cfg, now)
    with _connect(cfg) as con:
        last = db.last_decision(con)
    hours = cfg["general"]["healthcheck_hours"]
    ok = last is not None and now - datetime.fromisoformat(last["created"]) < timedelta(hours=hours)
    if not ok:
        telegram.send(f"⚠️ newsbot: sin decisión en las últimas {hours} h "
                      f"(última: {last['created'] if last else 'nunca'}).")
    return ok


def resume(cfg: dict) -> None:
    with _connect(cfg) as con:
        kv = db.KV(con)
        kv.delete("paused")
        kv.delete("kill_switch")
    print("Reanudado: pausa y kill-switch rearmados.")


def load_dotenv(path: str = ".env") -> None:
    """Carga .env sin dependencias; no pisa variables ya definidas en el entorno."""
    p = Path(path)
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


class PortfolioTag(logging.Filter):
    """Antepone "[bolsa] " a los mensajes de una cartera extra; los de cripto quedan igual."""

    def filter(self, record: logging.LogRecord) -> bool:
        w = portfolio.CURRENT.get()
        if w and not getattr(record, "_portfolio", None):  # un registro pasa por varios handlers
            record.msg, record._portfolio = f"[{w}] {record.msg}", w
        return True


def setup_logging(cfg: dict) -> None:
    data_dir = Path(cfg["general"]["data_dir"])
    data_dir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    file = RotatingFileHandler(data_dir / "newsbot.log", maxBytes=1_000_000, backupCount=5,
                               encoding="utf-8")
    # con pythonw (sin consola) no hay stderr: solo el fichero
    handlers = [file] + ([logging.StreamHandler()] if sys.stderr else [])
    for h in handlers:
        h.setFormatter(fmt)
        h.addFilter(PortfolioTag())
    logging.basicConfig(level=logging.INFO, handlers=handlers, force=True)


def load_extras(cfg: dict, path: str | Path) -> list[dict]:
    """Carteras de general.portfolios. Una config rota no impide arrancar cripto: queda en el log
    y se avisa una vez por Telegram."""
    load = getattr(config_mod, "load_portfolios", None)  # lo aporta config.py
    if load is None or not cfg["general"].get("portfolios"):
        return []
    try:
        return list(load(cfg, path))
    except Exception as exc:
        log.error("No se pudieron cargar las carteras extra (%s: %s); sigo solo con cripto",
                  type(exc).__name__, exc)
        telegram.send(f"⚠️ No se pudieron cargar las carteras extra ({type(exc).__name__}: {exc}). "
                      "Sigo solo con cripto."[:500])
        return []


def main() -> None:
    p = argparse.ArgumentParser(description="newsbot: una decisión diaria, paper por defecto")
    p.add_argument("--config", default="config.toml")
    p.add_argument("--dry-run", action="store_true", help="calcula la decisión sin guardar nada")
    p.add_argument("--force", action="store_true", help="ignora el guard diario (solo depurar)")
    p.add_argument("--compare-sentiment", action="store_true", help="LLM vs FinBERT vs léxico")
    p.add_argument("--report", action="store_true", help="informe semanal (y lo envía a Telegram)")
    p.add_argument("--healthcheck", action="store_true", help="avisa si no hay decisión reciente")
    p.add_argument("--telegram-poll", action="store_true", help="procesa comandos de Telegram")
    p.add_argument("--reconcile", action="store_true", help="concilia órdenes pendientes (live)")
    p.add_argument("--resume", action="store_true", help="rearma kill-switch y quita la pausa")
    p.add_argument("--watch", action="store_true",
                   help="vigilante: guarda noticias y adelanta la decisión ante una noticia fuerte")
    p.add_argument("--loop", action="store_true",
                   help="bucle continuo: decide cada general.tick_minutes (Docker / portátil)")
    args = p.parse_args()
    load_dotenv()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")  # consola de Windows
    cfg = config_mod.load(args.config)
    setup_logging(cfg)
    extras = load_extras(cfg, args.config)
    portfolio.register([cfg, *extras])
    try:
        _dispatch(args, cfg, extras)
    except SystemExit:
        raise
    except ccxt.AuthenticationError as exc:  # key sin confirmar, revocada, sin permisos o IP no permitida
        log.error("El exchange rechazó la API key: %s", exc)
        telegram.send(f"❌ newsbot: el exchange rechazó la API key ({exc})"[:500])
        sys.exit(1)
    except Exception as exc:
        log.exception("Error")
        telegram.send(f"❌ newsbot error: {type(exc).__name__}: {exc}"[:500])
        raise


def _dispatch(args, cfg: dict, extras: list[dict]) -> None:
    with portfolio.active(cfg):  # --config config_bolsa.toml: avisos y log con su etiqueta
        if args.compare_sentiment:
            compare_sentiment(cfg)
        elif args.report:
            with _connect(cfg) as con:
                text = report.weekly(con, cfg["general"]["initial_capital"])
            print(text)
            telegram.send(text)
        elif args.healthcheck:
            sys.exit(0 if healthcheck(cfg) else 1)
        elif args.telegram_poll:
            with _connect(cfg) as con:
                telegram.poll(con, cfg)
        elif args.reconcile:
            print(f"{reconcile(cfg)} órdenes conciliadas")
        elif args.resume:
            resume(cfg)
        elif args.watch:
            with single_instance(cfg):
                watch(cfg)
        elif args.loop:
            loop(cfg, extras=extras)
        elif args.dry_run and cfg["general"].get("engine") == "stocks":
            stocks.run_tick(cfg, dry_run=True)  # ranking y lo que propondría; no escribe ni opera
        elif args.dry_run:
            run_once(cfg, dry_run=True)
        elif cfg["general"].get("engine") == "stocks":
            with single_instance(cfg):
                stocks.run_tick(cfg)
        else:
            with single_instance(cfg):
                reconcile(cfg)
                run_once(cfg, force=args.force)


if __name__ == "__main__":
    main()
