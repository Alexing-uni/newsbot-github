"""Notificaciones y comandos por Telegram (TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID).

Los comandos (solo se aceptan desde TELEGRAM_CHAT_ID) los atiende el hilo de escucha del bucle
(`--loop`) con long polling de getUpdates; `--telegram-poll` procesa una vez los pendientes.
Respuestas en HTML con todo el texto externo (titulares, fuentes, motivos, urls) escapado.
/ayuda lista los comandos (en español, con alias en inglés); register_commands() los publica en
el menú de Telegram.
Bolsa (general.engine = "stocks"): cada compra o venta llega como propuesta con botones ✅/❌
(callback "o:<palabra>:<id>:y|n") o se responde con "sí"/"no"; solo se ejecuta si la aceptas.
"""
import contextvars
import html
import json
import logging
import os
import re
import sqlite3
import urllib.parse
from collections.abc import Mapping
from contextlib import closing
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from newsbot import config as config_mod
from newsbot import http, portfolio
from newsbot.features import session
from newsbot.news import ingest
from newsbot.news.rss import Headline
from newsbot.reporting import report
from newsbot.sentiment import lexicon
from newsbot.storage import db

log = logging.getLogger(__name__)
API = "https://api.telegram.org/bot{token}/{method}"
LIMIT = 3800  # Telegram admite 4096; margen para el cierre de <pre>
HORIZONS = ("1h", "4h", "24h")  # = research.learn.HORIZONS
ERROR = "⚠️ Error interno procesando el comando."
ID = "&lt;id&gt;"  # marcador de los mensajes de uso
STALE_SENSITIVE = {"aprobar", "rechazar", "pausa", "reanudar", "deshacer"}  # cambian el estado del bot
REFRESHABLE = {"estado", "noticias", "senales", "moneda", "aprendizaje", "eventos",  # con botón 🔄
               "pensamiento"}
YES = {"sí", "si", "yes", "✅"}  # respuestas de texto a una propuesta de bolsa ("ok"/"vale" no: ambiguas)
NO = {"no", "❌"}
NO_PENDING = "No hay ninguna propuesta pendiente."


# --- envío ------------------------------------------------------------------------------------
def chunks(text: str, limit: int = LIMIT) -> list[str]:
    """Trozos <= `limit` cortando por líneas (los formateadores no abren etiquetas entre líneas);
    un bloque <pre> partido se cierra al final del trozo y se reabre en el siguiente."""
    out: list[str] = []
    cur: list[str] = []
    size, pre = 0, False
    for line in text.split("\n"):
        for p in [line[i:i + limit - 20] for i in range(0, len(line), limit - 20)] or [""]:
            if cur and size + len(p) + 7 > limit:
                out.append("\n".join(cur) + ("</pre>" if pre else ""))
                cur, size, p = [], 0, ("<pre>" + p if pre else p)
            cur.append(p)
            size += len(p) + 1
            opened, closed = p.rfind("<pre>"), p.rfind("</pre>")
            pre = pre if opened == closed else opened > closed
    out.append("\n".join(cur))
    return out


def _plain(text: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", text))


def send(text: str, env: Mapping[str, str] | None = None, post=http.post_json, html: bool = False,
         silent: bool = False, markup: dict | None = None) -> bool:
    """Envía `text` (en trozos si es largo). Con `html` usa parse_mode HTML y, si Telegram
    rechaza un trozo (p. ej. 400 por una entidad mal formada), lo reintenta como texto plano.
    Los avisos de una cartera que no es la principal llevan delante su etiqueta ("📈 Bolsa · ")."""
    return _deliver(text, env, post, html, silent, markup)[0]


def send_get_id(text: str, env: Mapping[str, str] | None = None, post=http.post_json, html: bool = True,
                silent: bool = False, markup: dict | None = None) -> int | None:
    """Como send, pero devuelve el message_id del último trozo (el de los botones) para poder
    editarlo después con edit_message; None si no se pudo enviar."""
    ok, mid = _deliver(text, env, post, html, silent, markup)
    return mid if ok else None


def _deliver(text, env, post, html, silent, markup) -> tuple[bool, int | None]:
    env = os.environ if env is None else env
    token, chat = env.get("TELEGRAM_BOT_TOKEN"), env.get("TELEGRAM_CHAT_ID")
    if not (token and chat):
        return False, None
    if lbl := portfolio.LABEL.get():
        text = f"{e(lbl) if html else lbl} · {text}"
    url = API.format(token=token, method="sendMessage")
    extra = {"disable_notification": True} if silent else {}
    parts, mid = chunks(text), None
    for i, part in enumerate(parts):
        payload = {"chat_id": chat, "text": part, **extra}
        if markup and i == len(parts) - 1:  # los botones, en el último trozo
            payload["reply_markup"] = markup
        if html:
            payload |= {"parse_mode": "HTML", "link_preview_options": {"is_disabled": True}}
        try:
            mid = _message_id(post(url, payload))
            continue
        except Exception as exc:  # sin el mensaje de la excepción: la URL lleva el token
            log.warning("Telegram falló (%s)", _why(exc))
            if not html:
                return False, None
        try:
            mid = _message_id(post(url, {"chat_id": chat, "text": _plain(part), **extra}))
        except Exception as exc:
            log.warning("Telegram falló también en texto plano (%s)", _why(exc))
            return False, None
    return True, mid


def send_photo(photo: bytes, caption: str = "", env: Mapping[str, str] | None = None,
                markup: dict | None = None) -> bool:
    """Envía una imagen PNG (sendPhoto) con su pie en HTML y la etiqueta de la cartera activa."""
    env = os.environ if env is None else env
    token, chat = env.get("TELEGRAM_BOT_TOKEN"), env.get("TELEGRAM_CHAT_ID")
    if not (token and chat):
        return False
    if lbl := portfolio.LABEL.get():
        caption = f"{e(lbl)} · {caption}"
    fields = {"chat_id": chat, "caption": chunks(caption, 1000)[0], "parse_mode": "HTML"}
    if markup:
        fields["reply_markup"] = json.dumps(markup)
    try:
        http.post_multipart(API.format(token=token, method="sendPhoto"), fields,
                            {"photo": ("grafico.png", photo, "image/png")})
        return True
    except Exception as exc:  # sin el mensaje de la excepción: la URL lleva el token
        log.warning("Telegram: sendPhoto falló (%s)", _why(exc))
        return False


def _message_id(resp) -> int | None:
    """message_id de la respuesta de Telegram ({"ok": true, "result": {"message_id": 7, ...}})."""
    res = resp.get("result") if isinstance(resp, dict) else None
    mid = res.get("message_id") if isinstance(res, dict) else None
    return mid if isinstance(mid, int) else None


def edit_message(message_id: int | None, text: str, env: Mapping[str, str] | None = None,
                 post=http.post_json, markup: dict | None = None) -> bool:
    """Reescribe (HTML) un mensaje ya enviado; sin `markup` le quita los botones. Lleva la etiqueta
    de la cartera activa como send y, si Telegram rechaza el HTML, lo reintenta en texto plano."""
    env = os.environ if env is None else env
    token, chat = env.get("TELEGRAM_BOT_TOKEN"), env.get("TELEGRAM_CHAT_ID")
    if not (token and chat and message_id):
        return False
    if lbl := portfolio.LABEL.get():
        text = f"{e(lbl)} · {text}"
    url, part = API.format(token=token, method="editMessageText"), chunks(text)[0]
    base = {"chat_id": chat, "message_id": message_id, "reply_markup": markup or {"inline_keyboard": []}}
    try:
        post(url, base | {"text": part, "parse_mode": "HTML", "link_preview_options": {"is_disabled": True}})
        return True
    except Exception as exc:  # sin el mensaje de la excepción: la URL lleva el token
        why = _why(exc)
        if "not modified" in why:  # mismo contenido: no hay nada que cambiar
            return True
        log.warning("Telegram: no se pudo editar el mensaje (%s)", why)
    try:
        post(url, base | {"text": _plain(part)})
        return True
    except Exception as exc:
        log.warning("Telegram: tampoco en texto plano (%s)", _why(exc))
        return False


# --- utilidades de formato --------------------------------------------------------------------
def e(x) -> str:
    return html.escape("" if x is None else str(x), quote=True)


def _local(cfg: dict, when: float | datetime | None = None) -> datetime:
    """Hora de general.timezone (Europe/Madrid: cambia sola entre horario de invierno y verano)."""
    tz = ZoneInfo(cfg["general"]["timezone"])
    if when is None:
        return datetime.now(tz)
    return datetime.fromtimestamp(when, tz) if isinstance(when, int | float) else when.astimezone(tz)


def _ago(seconds: float) -> str:
    m = max(0, int(seconds // 60))
    return f"hace {m} min" if m < 120 else f"hace {m // 60} h" if m < 2880 else f"hace {m // 1440} d"


def _dt(iso: str | None) -> datetime | None:
    try:
        d = datetime.fromisoformat(iso or "")
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=UTC)


def _json(s: str | None) -> dict:
    try:
        out = json.loads(s or "{}")
    except ValueError:
        return {}
    return out if isinstance(out, dict) else {}


def _short(s: str, n: int) -> str:
    return s if len(s) <= n else s[:n - 1] + "…"


def _link(text: str, url: str | None) -> str:
    return f'<a href="{e(url)}">{e(text)}</a>' if (url or "").startswith("https://") else e(text)


def _emoji(score: float) -> str:
    return "🟢" if score > 0.1 else "🔴" if score < -0.1 else "⚪"


def _headline(r: sqlite3.Row) -> Headline:
    return Headline(r["title"], r["summary"] or "", r["source"] or "",
                    _dt(r["published"]) or datetime.now(UTC), r["url"] or "")


def _score(r: sqlite3.Row) -> float | None:
    """Sentimiento del titular con el léxico; None si no tiene ninguna palabra clave."""
    s = lexicon.analyze([_headline(r)], [1.0])
    return s.score if s.confidence > 0 else None


def _scored(cfg: dict) -> bool:
    """Con sentiment.fallback = "none" (bolsa) el léxico no puntúa titulares: con oro o defensa
    acierta el sentido al revés (guerra = sube)."""
    return cfg["sentiment"].get("fallback", "lexicon") != "none"


def _news_line(r: sqlite3.Row, now: datetime, scored: bool = True) -> str:
    s, pub = _score(r) if scored else None, _dt(r["published"])
    age = _ago((now - pub).total_seconds()) if pub else "sin fecha"
    tag = "⚪  —  " if s is None else f"{_emoji(s)} {s:+.2f}"
    return (f"{tag} · {_link(_short(r['title'], 120), r['url'])}\n"
            f"   <i>{e(r['source'] or '?')} · {age}</i>")


def _headlines(con: sqlite3.Connection, since: datetime | None = None, limit: int = 500) -> list:
    """Titulares de más reciente a más antiguo, sin repetir título (UNIQUE es por día) ni enlace
    (los medios retocan el titular de una noticia ya publicada)."""
    cut = since.astimezone(UTC).replace(microsecond=0).isoformat() if since else ""
    seen: set[str] = set()
    out = []
    for r in con.execute("SELECT * FROM headlines WHERE published >= ? ORDER BY published DESC "
                         "LIMIT ?", (cut, limit)):
        keys = {r["title"], r["url"]} - {None, ""}
        if not keys & seen:
            seen |= keys
            out.append(r)
    return out


def _coin(cfg: dict, arg: str) -> str | None:
    code = config_mod.asset(arg.upper())
    return code if code in cfg["news"]["assets"] else None


def _unsupported(cfg: dict) -> str:
    what = "Moneda no soportada" if portfolio.word(cfg) == portfolio.MAIN else "Valor no soportado"
    return f"{what}: " + e(", ".join(cfg["news"]["assets"]))


def _example(cfg: dict) -> str:
    """Código para los ejemplos de los mensajes (BTC en cripto)."""
    assets = cfg["news"]["assets"]
    return "BTC" if "BTC" in assets else next(iter(assets), "")


DAYS = ("lun", "mar", "mié", "jue", "vie", "sáb", "dom")


def _closed_line(cfg: dict, now: datetime) -> str:
    nxt = session.next_open(cfg, now)
    if nxt is None:
        return "🔒 Mercado cerrado"
    loc = _local(cfg, nxt)
    return f"🔒 Mercado cerrado · abre {DAYS[loc.weekday()]} {loc:%H:%M}"


def _peer_line(w: str, c: dict) -> str:
    """Resumen de otra cartera leyendo su base de datos en solo lectura."""
    path = (Path(c["general"]["data_dir"]) / "newsbot.db").resolve()
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as con:
        con.row_factory = sqlite3.Row
        st, top = db.last_state(con), db.latest_ticks(con)[:1]
    name = "🪙 Cripto" if w == portfolio.MAIN else portfolio.label(c) or w
    cap = f"{st['equity']:.2f} €" if st else "sin capital"
    lead = (f"{e(config_mod.asset(top[0]['symbol']))} {top[0]['signal']:+.3f}" if top
            else "sin señales")
    mode = "live" if c["general"]["mode"] == "live" else "papel"
    return f"{e(name)}: {cap} ({mode}) · {lead} · /estado" + ("" if w == portfolio.MAIN else f" {e(w)}")


def _peer_lines(cfg: dict) -> list[str]:
    """Una línea por cada otra cartera registrada (solo con más de una); los fallos se omiten."""
    if len(portfolio.REGISTRY) < 2 or _ALL.get():  # vista conjunta: cada cartera ya sale entera
        return []
    out = []
    for w, c in portfolio.REGISTRY.items():
        if w == portfolio.word(cfg) or not _enabled(c):
            continue
        try:
            out.append(_peer_line(w, c))
        except Exception as exc:
            log.debug("Telegram: resumen de %s no disponible (%s)", w, type(exc).__name__)
    return out


def _balance_line(con, cfg: dict, now: datetime) -> str:
    """Saldo leído de Trading 212 (caché de 1 min): demuestra la conexión aunque no haya habido ciclos."""
    try:
        from newsbot.stocks import engine
        b = engine.broker_balance(cfg, con, now)
    except Exception as exc:
        return f"⚠️ No pude leer el saldo del broker ({e(type(exc).__name__)})"
    when = f"{_local(cfg, b['ts']):%H:%M}" if b.get("ts") else "?"
    if b.get("error"):
        return f"⚠️ Trading 212 no responde ({when}): {e(b['error'])}"
    inv = f" + {b['value']:.2f} € invertidos" if b.get("positions") else ""
    return (f"💶 Trading 212: <b>{b.get('cash', 0):.2f} €</b> disponibles{inv} · "
            f"{b.get('positions', 0)} posiciones (leído {when})")


# --- comandos: todos reciben (con, cfg, cfg_eff, args, now) y devuelven HTML ------------------
def _estado(con, cfg, cfg_eff, args, now) -> str:
    kv, g = db.KV(con), cfg["general"]
    ts = con.execute("SELECT max(ts) FROM ticks WHERE action != 'BACKFILL'").fetchone()[0]
    age = now.timestamp() - ts if ts else None
    closed = not session.is_open(cfg, now)  # bolsa fuera de horario: no actualiza y es normal
    paused, period = kv.get("paused") == "1", g["tick_minutes"] * 60
    opened = session.last_open(cfg, now)  # el primer ciclo de la sesión llega hasta un periodo después
    ref = max(ts or 0, opened.timestamp() if opened else 0) or None
    since = now.timestamp() - ref if ref else None
    stale = since is not None and since > max(900, 2 * period) and not closed and not paused
    waiting = bool(opened) and (not ts or ts < opened.timestamp()) and not stale
    cycle = ("sin ciclos todavía" + ("" if closed or waiting or paused else " ⚠️") if age is None
             else f"último ciclo {_local(cfg, ts):%H:%M} ({_ago(age)})" + (" ⚠️" if stale else ""))
    head = e(portfolio.label(cfg)) or "newsbot"
    resume = portfolio.cmd("reanudar", "CONFIRMAR")
    second = (_closed_line(cfg, now) if closed else
              f"⏸ En pausa: no analiza ni propone ({resume})" if paused else
              f"⏳ Mercado abierto desde las {cfg['session']['open']}: primer análisis en curso"
              if waiting else _countdown(age, period))
    lines = [f"<b>{head} · {'LIVE' if g['mode'] == 'live' else 'PAPER'}</b> · {cycle}", second]
    flags = [f for f in ("paused", "kill_switch") if kv.get(f) == "1"]
    lines.append(f"{'🛑 ' if flags else ''}Flags: {', '.join(flags) or 'ninguno'}")
    st, dec, ini = db.last_state(con), db.last_decision(con), g["initial_capital"]
    if _is_stocks(cfg) and g["mode"] == "live":
        lines.append(_balance_line(con, cfg, now))
    if st:
        pnl = st["equity"] - ini
        lines.append(f"Capital <b>{st['equity']:.2f} €</b> · P&amp;L {pnl:+.2f} € ({pnl / ini:+.1%})"
                     f" · efectivo {st['cash']:.2f} €")
    elif not (_is_stocks(cfg) and g["mode"] == "live"):
        lines.append("Sin capital registrado")
    if _is_stocks(cfg):  # varias posiciones, caja y propuestas pendientes
        lines += _positions_lines(con, cfg, now)
    elif st and st["qty"] and st["symbol"]:
        avg, px = st["avg_entry"] or 0.0, st["price"] or 0.0
        lines.append(f"Posición: {e(st['symbol'])} {st['qty']:.8f} · entrada {avg:,.4f} · "
                     f"precio {px:,.4f}" + (f" ({px / avg - 1:+.1%})" if avg > 0 else ""))
    elif st:
        lines.append("Posición: ninguna")
    if dec:
        lines.append(f"Última decisión {dec['day']}: {e(dec['action'])} {e(dec['symbol'] or '')} — "
                     f"{e(dec['reason'])}")
    if top := db.latest_ticks(con)[:3]:
        lines += ["<b>Ranking</b>"] + [f"{i}. {e(t['symbol'])} {t['signal']:+.3f} {e(t['action'])}"
                                       for i, t in enumerate(top, 1)]
        if need := _need_line(cfg_eff, top[0]):
            lines.append(need)
    lines.append(_claude_line(con, cfg))
    try:
        from newsbot.research import learn
        lines += learn.progress_lines(learn.progress(con, cfg_eff, now))
    except Exception as exc:  # el panel no debe romper /estado
        log.warning("Telegram: panel de memoria falló (%s)", type(exc).__name__)
    lines += _peer_lines(cfg)
    day, sc = len(_headlines(con, since=now - timedelta(hours=24))), _scored(cfg)
    lines += [f"<b>📰 Últimas noticias</b> · {day} en 24 h"]
    lines += [_news_line(r, now, sc) for r in _headlines(con, limit=50)[:2 if _ALL.get() else 5]] \
        or ["(sin titulares)"]
    c = portfolio.cmd
    lines.append(f"<i>{c('noticias', _example(cfg))} · {c('senales')} · {c('aprendizaje')} · "
                 f"{c('ayuda')}</i>")
    return "\n".join(lines)


def _is_stocks(cfg: dict | None) -> bool:
    """Cartera multiposición con propuestas confirmadas por Telegram (newsbot/stocks)."""
    return bool(cfg) and cfg["general"].get("engine") == "stocks"


def _stocks_cfgs(cfg: dict | None = None) -> list[dict]:
    """Carteras de bolsa registradas (o `cfg` si no hay ninguna registrada y lo es)."""
    return [c for c in (list(portfolio.REGISTRY.values()) or [cfg]) if _is_stocks(c)]


def _positions_lines(con, cfg, now) -> list[str]:
    try:
        from newsbot.stocks import engine
        return list(engine.positions_lines(con, cfg, now))
    except Exception as exc:  # el panel no debe romper /estado
        log.warning("Telegram: posiciones de bolsa no disponibles (%s)", type(exc).__name__)
        return ["Posiciones: no disponibles"]


def _countdown(age: float | None, period: int) -> str:
    """Cuánto falta para el próximo tick (foto del momento: pulsa 🔄 Actualizar para refrescar)."""
    if age is None:
        return "⏳ Primera actualización en curso…"
    left = period - age
    if left <= 0:
        return ("⏳ Actualizando ahora…" if left > -300 else
                "⚠️ El bot no actualiza: ¿está encendido? (revisa el PC)")
    return f"⏳ Próxima actualización en {int(left // 60)} min {int(left % 60):02d} s"


def needed_to_buy(cfg: dict, raw: dict) -> float | None:
    """Señal mínima para abrir: el umbral (más alto en régimen bajista) o, si es mayor, la que
    exige el filtro de comisiones (ATR × señal ≥ múltiplo × coste de ida y vuelta)."""
    st, r, c = cfg["strategy"], cfg["risk"], cfg["costs"]
    atr = raw.get("_atr")
    if not atr:
        return None
    th = st["buy_threshold_bear"] if raw.get("_regime") == "bear" else st["buy_threshold"]
    edge = r["edge_cost_multiple"] * 2 * (c["fee_pct"] + c["slippage_pct"]) / atr
    return max(th, edge) if edge <= 1 else None  # > 1: con este ATR nunca compensa


def _need_line(cfg: dict, t) -> str | None:
    need = needed_to_buy(cfg, _json(t["raw_json"]))
    if need is None or t["held"]:
        return None
    gap = need - t["signal"]
    return (f"🎯 Para comprar {e(config_mod.asset(t['symbol']))} necesita señal ≥ {need:+.2f} "
            + ("(¡la tiene!)" if gap <= 0 else f"(le faltan {gap:+.2f})"))


def _claude_line(con, cfg) -> str:
    sc = cfg["sentiment"]
    if "llm" not in sc["cascade"] or not os.environ.get("ANTHROPIC_API_KEY"):
        return "🤖 Claude: sin API key (sentimiento con léxico)"
    from newsbot.sentiment import llm, usage_today
    u, price = usage_today(db.KV(con)), llm.PRICES.get(sc["llm_model"])
    cost = f" ≈ {(u['in'] * price[0] + u['out'] * price[1]) / 1e6:.3f} $" if price else ""
    return f"🤖 Claude hoy: {u['calls']}/{sc.get('llm_max_calls_day', 60)} llamadas{cost}"


def _noticias(con, cfg, cfg_eff, args, now) -> str:
    coin, n = None, 8
    for a in args:
        if a.isdigit():
            n = min(max(int(a), 1), 15)
        elif not (coin := _coin(cfg, a)):
            return _unsupported(cfg)
    rows = _headlines(con, since=now - timedelta(hours=24))
    if coin:
        rows = [r for r in rows if ingest.mentions(_headline(r), cfg["news"]["assets"][coin])]
    label = f" de {coin}" if coin else ""
    if not rows:
        return f"Sin titulares{e(label)} en las últimas 24 h."
    if not (sc := _scored(cfg)):
        mood = "— (sin puntuar: en bolsa solo puntúa Claude)"
    else:
        scored = [(s, r["weight"] or 0.0) for r in rows if (s := _score(r)) is not None]
        total = sum(w for _, w in scored)
        avg = (None if not scored else sum(s * w for s, w in scored) / total if total > 0
               else sum(s for s, _ in scored) / len(scored))
        mood = ("—" if avg is None else f"{_emoji(avg)} {avg:+.2f}") + f" ({len(scored)} con señal)"
    head = (f"<b>Noticias{e(label)} · últimas 24 h</b> · {min(n, len(rows))} de {len(rows)} · "
            f"sentimiento medio {mood}")
    return "\n".join([head] + [_news_line(r, now, sc) for r in rows[:n]])


def _senales(con, cfg, cfg_eff, args, now) -> str:
    ticks = db.latest_ticks(con)
    if not ticks:
        return f"Todavía no hay ticks: espera al próximo ciclo ({cfg['general']['tick_minutes']} min)."
    st, ts = cfg_eff["strategy"], max(t["ts"] for t in ticks)
    table = [e(f"{config_mod.asset(t['symbol']):<6} {t['signal']:+.3f}  {t['action']}") for t in ticks]
    lines = [f"<b>Señales</b> · tick {_local(cfg, ts):%d/%m %H:%M} "
             f"({_ago(now.timestamp() - ts)})",
             "<pre>" + e(f"{'moneda':<6} {'señal':>6}  acción"), *table[:-1], table[-1] + "</pre>"]
    for t in ticks:
        top = sorted(_json(t["contributions_json"]).items(), key=lambda kv: -abs(kv[1]))[:3]
        lines.append(f"<b>{e(t['symbol'])}</b>: " + (", ".join(f"{e(k)} {v:+.3f}" for k, v in top)
                                                    or "sin aportes"))
    lines += [x for t in ticks if (x := _need_line(cfg_eff, t))]
    lines.append(f"Umbrales: compra ≥ {st['buy_threshold']:+.2f} (bajista "
                 f"{st.get('buy_threshold_bear', st['buy_threshold']):+.2f}) · venta ≤ "
                 f"{st['sell_threshold']:+.2f}")
    return "\n".join(lines)


def _moneda(con, cfg, cfg_eff, args, now) -> str:
    if not args:
        what = "una moneda" if portfolio.word(cfg) == portfolio.MAIN else "un valor"
        return f"Elige {what} (o escribe {portfolio.cmd('moneda', _example(cfg))}):"
    if not (code := _coin(cfg, args[0])):
        return _unsupported(cfg)
    t = next((t for t in db.latest_ticks(con) if config_mod.asset(t["symbol"]) == code), None)
    lines = [f"<b>{e(code)}</b>"]
    if t:
        w, contrib = cfg_eff["weights"], _json(t["contributions_json"])
        lines = [f"<b>{e(t['symbol'])}</b> · {t['price'] or 0:,.4f} · tick {_ago(now.timestamp() - t['ts'])}"
                 + (" · en cartera" if t["held"] else ""),
                 f"Señal <b>{t['signal']:+.3f}</b> → {e(t['action'])}",
                 f"Motivo: {e(t['reason'])}",
                 *[x for x in [_need_line(cfg_eff, t)] if x],
                 "<b>Componentes</b> (valor · peso → aporte)"]
        for k, v in _json(t["raw_json"]).items():
            if k.startswith("_") or not isinstance(v, int | float):
                continue  # datos internos (régimen, ATR...), no componentes de la señal
            wk = w.get(k, 0)
            lines.append(f"<code>{e(f'{k:<10}')}</code> {v or 0.0:+.3f} · " + (
                f"{wk:.2f} → {contrib.get(k) or 0.0:+.3f}" if wk > 0 else "sombra"))
    else:
        lines.append("Todavía no hay ticks de esta moneda.")
    kw = cfg["news"]["assets"][code]
    news, sc = [r for r in _headlines(con) if ingest.mentions(_headline(r), kw)][:3], _scored(cfg)
    lines += ["<b>Titulares recientes</b>"] + ([_news_line(r, now, sc) for r in news]
                                               or ["(sin titulares)"])
    return "\n".join(lines)


def _aprendizaje(con, cfg, cfg_eff, args, now) -> str:
    horizon = args[0].lower() if args else None
    if horizon is not None and horizon not in HORIZONS:
        return f"Horizonte no válido: usa {' | '.join(HORIZONS)}."
    try:
        from newsbot.research import learn
    except ImportError:
        return "El módulo de aprendizaje todavía no está disponible."
    return learn.report(con, cfg_eff, now, horizon)


def _operaciones(con, cfg, cfg_eff, args, now) -> str:
    n = min(max(int(args[0]), 1), 20) if args and args[0].isdigit() else 10
    rows = con.execute("SELECT * FROM orders ORDER BY created DESC, id DESC LIMIT ?", (n,)).fetchall()
    if not rows:
        return "Todavía no hay operaciones."
    return "\n".join([f"<b>Últimas {len(rows)} operaciones</b>"] + [
        f"{e((r['created'] or r['day'] or '')[:16].replace('T', ' '))} · <b>{e((r['side'] or '').upper())}"
        f"</b> {e(r['symbol'] or '')} {r['qty'] or 0:.8f} @ {r['price'] or 0:,.4f} = {r['eur'] or 0:.2f} €"
        f" · comisión {r['fee'] or 0:.4f} € · {e(r['status'])}" for r in rows])


def _rendimiento(con, cfg, cfg_eff, args, now) -> str:
    rows = con.execute("SELECT day, equity FROM equity_daily ORDER BY day").fetchall()
    if not rows:
        return "Rendimiento: todavía no hay datos."
    ini, last = cfg["general"]["initial_capital"], rows[-1]

    def change(days: int) -> str:
        ref = (date.fromisoformat(last["day"]) - timedelta(days=days)).isoformat()
        base = next((r for r in reversed(rows) if r["day"] <= ref), rows[0])
        d = last["equity"] - base["equity"]
        return f"{d:+.2f} € ({d / base['equity']:+.1%})" if base["equity"] else f"{d:+.2f} €"

    peak = mdd = 0.0
    for r in rows:
        peak = max(peak, r["equity"])
        mdd = max(mdd, 1 - r["equity"] / peak) if peak > 0 else mdd
    cur_dd = 1 - last["equity"] / peak if peak > 0 else 0.0
    n, fees = con.execute("SELECT count(*), coalesce(sum(fee), 0) FROM orders WHERE status = 'filled'"
                          ).fetchone()
    pnl = last["equity"] - ini
    return "\n".join([
        f"<b>Rendimiento</b> ({e(rows[0]['day'])} → {e(last['day'])})",
        f"Capital {last['equity']:.2f} € · inicial {ini:.2f} € · {pnl:+.2f} € ({pnl / ini:+.1%})",
        f"7 d: {change(7)} · 30 d: {change(30)}",
        f"Máx. drawdown: {-mdd:.1%} · ahora {-cur_dd:.1%} desde el máximo ({peak:.2f} €)",
        f"Órdenes llenadas: {n} · comisiones: {fees:.4f} €",
        "<i>Rentabilidad pasada no garantiza la futura.</i>"])


def _origin(note: str | None) -> str:
    if not note:
        return "?"
    d = _json(note)
    if not d:
        return _short(note, 80)
    return str(d.get("origin") or d.get("source") or ("reoptimización" if "oos_candidate" in d else "?"))


def _propuestas(con, cfg, cfg_eff, args, now) -> str:
    rows = con.execute("SELECT * FROM params_history WHERE status = 'proposed' ORDER BY id").fetchall()
    done = con.execute("SELECT * FROM params_history WHERE status = 'applied' ORDER BY id DESC "
                       "LIMIT 3").fetchall()
    applied = [f"#{r['id']} · {e((r['created'] or '')[:10])} · {e(_origin(r['note']))} · "
               f"<code>{e(_short(_flat(r['params_json']), 200))}</code>" for r in done]
    tail = ([f"<b>Cambios en vigor</b> ({portfolio.cmd('deshacer', ID)})", *applied] if applied else
            ["Sin cambios aplicados: rige config.toml."])
    if not rows:
        return "\n".join(["No hay propuestas pendientes.", *tail])
    lines = [f"<b>Propuestas pendientes ({len(rows)})</b>"]
    for r in rows:
        lines.append(f"#{r['id']} · {e((r['created'] or '')[:10])} · origen {e(_origin(r['note']))}: "
                     f"<code>{e(_short(_flat(r['params_json']), 300))}</code>")
    lines.append(f"<i>{portfolio.cmd('aprobar', ID)} · {portfolio.cmd('rechazar', ID)}</i>")
    return "\n".join(lines + tail)


def _flat(params_json: str | None) -> str:
    p = _json(params_json)
    return ", ".join(f"{s}.{k}={v}" for s, vals in p.items() if isinstance(vals, dict)
                     for k, v in vals.items()) or (params_json or "")


def _deshacer(con, cfg, cfg_eff, args, now) -> str:
    """Revierte un cambio aplicado (a mano o solo): deja de contar desde el próximo tick."""
    if len(args) != 1 or not re.fullmatch(r"\d+", args[0], re.ASCII):
        return (f"Uso: {portfolio.cmd('deshacer', ID)} (número del aviso o de "
                f"{portfolio.cmd('propuestas')})")
    row = con.execute("SELECT id FROM params_history WHERE id = ? AND status = 'applied'",
                      (int(args[0]),)).fetchone()
    if not row:
        return "No hay ningún cambio aplicado con ese id."
    with con:
        con.execute("UPDATE params_history SET status = 'reverted' WHERE id = ?", (row["id"],))
    pid, override = db.active_params(con, cfg["general"]["mode"])
    return (f"Cambio {row['id']} revertido: desde el próximo tick rige "
            + (f"#{pid}." if pid else "config.toml.")
            + " Mientras el aprendizaje siga viendo lo mismo, puede volver a proponerlo.")


def _proposal(con, args, verb: str):
    if len(args) != 1 or not re.fullmatch(r"\d+", args[0], re.ASCII):
        return None, f"Uso: {portfolio.cmd(verb, ID)} (número de {portfolio.cmd('propuestas')})"
    row = con.execute("SELECT id, params_json, note FROM params_history WHERE id = ? AND status = 'proposed'",
                      (int(args[0]),)).fetchone()
    return row, "No hay ninguna propuesta pendiente con ese id."


def _aprobar(con, cfg, cfg_eff, args, now) -> str:
    row, msg = _proposal(con, args, "aprobar")
    if not row:
        return msg
    active_id, active = db.active_params(con, cfg["general"]["mode"])
    base = _json(row["note"]).get("based_on", active_id)
    if base != active_id:  # se calculó sobre otros parámetros: aplicarla desharía cambios
        return (f"La propuesta {row['id']} está obsoleta (se calculó sobre #{base}; ahora rige "
                f"#{active_id}). Recházala con {portfolio.cmd('rechazar', row['id'])} y espera a la "
                "siguiente.")
    try:  # una propuesta que no valida no se aplica: el bot no arrancaría con ella
        config_mod.apply(cfg, active | _json(row["params_json"]))
    except Exception as exc:
        return f"La propuesta {row['id']} no es válida y no se aplica ({e(type(exc).__name__)})."
    with con:
        # aprobada a mano: cuenta también en live aunque se propusiera en papel
        note = _json(row["note"]) | {"auto": False, "approved": datetime.now(UTC).isoformat()}
        con.execute("UPDATE params_history SET status = 'applied', note = ? WHERE id = ?",
                    (json.dumps(note, ensure_ascii=False), row["id"]))
    return f"Propuesta {row['id']} aplicada: entra en vigor desde el próximo tick."


def _rechazar(con, cfg, cfg_eff, args, now) -> str:
    row, msg = _proposal(con, args, "rechazar")
    if not row:
        return msg
    with con:
        con.execute("UPDATE params_history SET status = 'rejected' WHERE id = ?", (row["id"],))
    return f"Propuesta {row['id']} rechazada."


def _pausa(con, cfg, cfg_eff, args, now) -> str:
    db.KV(con).set("paused", "1")
    return f"Pausado: no habrá decisiones hasta {portfolio.cmd('reanudar')}."


def _reanudar(con, cfg, cfg_eff, args, now) -> str:
    if args != ["CONFIRMAR"]:
        return f"Esto reanuda el bot y rearma el kill-switch. Envía: {portfolio.cmd('reanudar', 'CONFIRMAR')}"
    kv = db.KV(con)
    kv.delete("paused")
    kv.delete("kill_switch")
    if _is_stocks(cfg) and (st := db.last_state(con)):  # si no, el siguiente ciclo lo vuelve a disparar
        kv.set("peak", repr(st["equity"]))
    return "Reanudado. Kill-switch rearmado."


def _informe(con, cfg, cfg_eff, args, now) -> str:
    return e(report.weekly(con, cfg["general"]["initial_capital"]))


def _eventos(con, cfg, cfg_eff, args, now) -> str:
    from newsbot.stocks import events
    return "\n".join(events.lines(con, cfg, now)) or "Sin eventos próximos."


def _symbol(cfg: dict, code: str) -> str | None:
    return next((s for s in cfg["general"]["symbols"] if config_mod.asset(s).lower() == code.lower()), None)


def send_chart(con, cfg: dict, code: str, env: Mapping[str, str] | None = None, post=http.post_json) -> str:
    """Dibuja y envía el gráfico de un valor; devuelve el resumen (HTML) o el motivo del fallo."""
    from newsbot.reporting import chart  # diferido: matplotlib solo al dibujar
    if not (sym := _symbol(cfg, code)):
        return f"No conozco «{e(code)}». Valores: " + ", ".join(
            e(config_mod.asset(s)) for s in cfg["general"]["symbols"])
    url = chart.tradingview_url(cfg, sym)
    link = f'<a href="{e(url)}">abrir en TradingView</a>'
    try:
        img = chart.render(cfg, con, sym)
    except Exception as exc:
        log.warning("Gráfico de %s no disponible (%s)", sym, type(exc).__name__)
        return f"No pude dibujar el gráfico de {e(code.upper())} ({e(type(exc).__name__)}): {link}."
    kb = {"inline_keyboard": [[{"text": "Abrir en TradingView", "url": url}]]}
    if not send_photo(img, f"📈 {e(config_mod.asset(sym))} · velas de 15 min", env, kb):
        return f"No pude enviar la imagen del gráfico de {e(config_mod.asset(sym))}: {link}."
    return f"📈 Gráfico de {e(config_mod.asset(sym))} enviado · {link}"


def _grafico(con, cfg, cfg_eff, args, now) -> str:
    if not args:
        return f"Uso: /grafico VALOR (p. ej. /grafico {e(config_mod.asset(cfg['general']['symbols'][0]))})"
    return send_chart(con, cfg, args[0])


def _pensamiento(con, cfg, cfg_eff, args, now) -> str:
    kv = db.KV(con)
    try:
        th = json.loads(kv.get("thinking") or "{}")
        lessons = json.loads(kv.get("lessons") or "[]")
    except ValueError:
        th, lessons = {}, []
    if not th.get("lines"):
        return "Todavía no hay ningún ciclo con el mercado analizado."
    age = (now - datetime.fromisoformat(th["ts"])).total_seconds() if th.get("ts") else None
    when = f" ({_ago(age)})" if age is not None else ""
    out = [f"💭 <b>Lo que pienso</b>{when}"] + [f"• {e(ln)}" for ln in th["lines"]]
    if lessons:
        last = lessons[-1]
        out += ["", f"🧐 <b>Última pérdida</b> ({e(last['symbol'].split('/')[0])})"]
        out += [f"• {e(ln)}" for ln in last["lines"][-2:]]
    return "\n".join(out)


def _ayuda(con=None, cfg=None, cfg_eff=None, args=(), now=None) -> str:
    many, stocks = len(portfolio.REGISTRY) > 1, _stocks_cfgs(cfg)
    return "\n".join(["<b>Comandos</b>"] + [
        f"/{name}{' ' + e(usage) if usage else ''} — {e(desc)}" + (f" (/{alias})" if alias else "")
        for name, (_, usage, desc, alias) in _commands(cfg).items()] + (
        [f"Añade «{e(w)}» a cualquier comando para la cartera de {e(w)} (p. ej. /estado {e(w)})."
         for w in portfolio.REGISTRY if w != portfolio.MAIN] if many else []) + (
        ["Sin palabra, cada comando vale para todas las carteras a la vez: las consultas muestran una "
         "sección por cartera y /pausa o /reanudar las cambian todas."]
        if cfg and _unified(cfg, []) else []) + (
        ["Las compras y ventas de bolsa se proponen con botones: pulsa ✅ o ❌, o responde «sí» o «no» "
         "(vale para la propuesta pendiente más reciente)."] if many and stocks else []))


def _commands(cfg: dict | None = None) -> dict:
    """Comandos de /ayuda y del menú: los de bolsa (/eventos) solo si hay alguna cartera de bolsa,
    así la ayuda y el menú de cripto sola no cambian."""
    if not _stocks_cfgs(cfg):
        return COMMANDS
    items = list(COMMANDS.items())
    return dict(items[:-1] + list(STOCKS_COMMANDS.items()) + items[-1:])  # antes de /ayuda


# nombre: (función, argumentos, descripción, alias en inglés)
COMMANDS = {
    "estado": (_estado, "", "Estado, capital, posición, ranking y últimas noticias", "status"),
    "noticias": (_noticias, "[MONEDA] [N]", "Titulares de las últimas 24 h con su sentimiento", "news"),
    "senales": (_senales, "", "Señales del último tick y sus componentes principales", "signals"),
    "moneda": (_moneda, "SYM", "Detalle de una moneda: componentes, pesos y titulares", "coin"),
    "aprendizaje": (_aprendizaje, "[1h|4h|24h]", "Qué componentes predicen el precio", "learn"),
    "operaciones": (_operaciones, "[N]", "Últimas órdenes", "trades"),
    "rendimiento": (_rendimiento, "", "Rentabilidad, drawdown y comisiones", "profit"),
    "propuestas": (_propuestas, "", "Cambios de parámetros pendientes y en vigor", "proposals"),
    "aprobar": (_aprobar, "<id>", "Aplica una propuesta desde el próximo tick", "approve"),
    "rechazar": (_rechazar, "<id>", "Descarta una propuesta", "reject"),
    "deshacer": (_deshacer, "<id>", "Revierte un cambio ya aplicado (también los automáticos)", "undo"),
    "pausa": (_pausa, "", "Pausa las decisiones", "pause"),
    "reanudar": (_reanudar, "CONFIRMAR", "Reanuda y rearma el kill-switch", "resume"),
    "informe": (_informe, "", "Informe semanal", "report"),
    "ayuda": (_ayuda, "", "Esta lista", "help"),
}
# solo de las carteras de bolsa (general.engine = "stocks"): en /ayuda y en el menú si hay alguna
STOCKS_COMMANDS = {
    "eventos": (_eventos, "", "Bolsa: eventos con fecha (presentaciones, lanzamientos, resultados)",
                "events"),
    "pensamiento": (_pensamiento, "", "Bolsa: qué ve y qué planea el bot, y su última lección", "thinking"),
    "grafico": (_grafico, "VALOR", "Bolsa: gráfico de 15 min con entrada/stop y enlace a TradingView",
                "chart"),
}
ALIASES = {alias: name for name, (*_, alias) in (COMMANDS | STOCKS_COMMANDS).items()} | {"start": "ayuda"}


def handle(con: sqlite3.Connection, cfg: dict, text: str, now: datetime | None = None) -> str:
    """Respuesta HTML a un comando. `cfg` es el base; los umbrales y /aprendizaje usan el efectivo
    (con la última propuesta aplicada encima)."""
    now = now or datetime.now(UTC)
    now = now if now.tzinfo else now.replace(tzinfo=UTC)
    parts = (text or "").split()
    name = parts[0][1:].split("@")[0].lower() if parts and parts[0].startswith("/") else ""
    name = ALIASES.get(name, name)
    if name not in COMMANDS and name not in STOCKS_COMMANDS:
        return "Comando no reconocido.\n" + _ayuda()
    if name != "ayuda" and (many := _unified(cfg, parts[1:])):
        return _handle_all(many, name, parts[1:], now)
    target, args = _route(cfg, parts[1:])
    if name in STOCKS_COMMANDS and not _is_stocks(target or cfg):  # a la primera cartera de bolsa
        if not (stocks := _stocks_cfgs(cfg)):
            return "No hay ninguna cartera de bolsa."
        target = stocks[0]
    if target is None:
        return _run(con, cfg, name, args, now)
    with _open(target) as con_x, portfolio.active(target):
        return _run(con_x, target, name, args, now)


def _open(cfg: dict):
    data = Path(cfg["general"]["data_dir"])
    data.mkdir(parents=True, exist_ok=True)
    return closing(db.connect(data / "newsbot.db"))


def _run(con, cfg, name, args, now) -> str:
    try:
        cfg_eff = config_mod.apply(cfg, db.active_params(con, cfg["general"]["mode"])[1])
    except Exception:
        cfg_eff = cfg
    return (COMMANDS.get(name) or STOCKS_COMMANDS[name])[0](con, cfg, cfg_eff, args, now)


def _route(cfg: dict, args: list[str]) -> tuple[dict | None, list[str]]:
    """Cartera elegida con su palabra en cualquier posición ("/estado bolsa", "/moneda RHM bolsa"):
    (su cfg, o None si es la de `cfg`; argumentos sin la palabra). Sin carteras registradas los
    argumentos no se tocan."""
    reg = {w.lower(): c for w, c in portfolio.REGISTRY.items()}
    if not reg:
        return None, args
    for i, a in enumerate(args):
        if (w := a.lower()) in reg or w == portfolio.MAIN:
            rest = args[:i] + args[i + 1:]
            return (None if w == portfolio.word(cfg).lower() else reg.get(w)), rest
    if not _enabled(cfg):  # la principal está apagada: por defecto, la primera cartera activa
        return next((c for c in portfolio.REGISTRY.values() if c is not cfg and _enabled(c)), None), args
    return None, args


def _enabled(cfg: dict) -> bool:
    return cfg["general"].get("enabled", True)


# --- vista conjunta: la principal apagada y dos o más carteras activas (bolsa + usa) ------------
_ALL: contextvars.ContextVar[bool] = contextvars.ContextVar("telegram_all", default=False)
SYMBOL_COMMANDS = {"noticias", "moneda", "grafico"}  # van a las carteras que tienen el valor
ID_COMMANDS = {"aprobar": "proposed", "rechazar": "proposed", "deshacer": "applied"}


def _unified(cfg: dict, args: list[str]) -> list[dict]:
    """Carteras a las que va un comando SIN palabra de cartera cuando la principal está apagada y hay
    al menos dos activas; [] en otro caso (el comando sigue el camino de siempre, _route)."""
    words = {w.lower() for w in portfolio.REGISTRY} | {portfolio.MAIN}
    if _enabled(cfg) or any(a.lower() in words for a in args):
        return []
    active = [c for c in portfolio.REGISTRY.values() if c is not cfg and _enabled(c)]
    return active if len(active) > 1 else []


def _section(c: dict, name: str, args: list[str], now: datetime) -> str:
    try:
        with _open(c) as con_x, portfolio.active(c):
            return _run(con_x, c, name, args, now)
    except Exception as exc:  # una cartera rota no tapa a las demás
        log.warning("Telegram: %s en %s falló (%s)", name, portfolio.word(c), type(exc).__name__)
        return ERROR


def _each(cfgs: list[dict], name: str, args: list[str], now: datetime) -> str:
    """El comando en cada cartera, una sección por cartera (en /estado la cabecera ya la lleva)."""
    token, out, total = _ALL.set(True), [], []
    try:
        for c in cfgs:
            body = _section(c, name, args, now)
            if name == "estado":
                try:
                    with _open(c) as con_x:
                        if st := db.last_state(con_x):
                            total.append((portfolio.label(c) or portfolio.word(c), st["equity"]))
                except Exception as exc:
                    log.debug("Telegram: capital de %s no disponible (%s)", portfolio.word(c),
                              type(exc).__name__)
                out.append(body)
            else:
                out.append(f"<b>{e(portfolio.label(c) or portfolio.word(c))}</b>\n{body}")
    finally:
        _ALL.reset(token)
    if total:
        parts = " + ".join(f"{e(lbl)} {eq:.2f} €" for lbl, eq in total)
        out.insert(0, f"🌐 <b>Todas las carteras</b> · capital <b>{sum(eq for _, eq in total):.2f} €</b>"
                      f" ({parts})")
    return "\n\n".join(out)


def _handle_all(cfgs: list[dict], name: str, args: list[str], now: datetime) -> str:
    """Comando sin palabra con varias carteras activas: consultas y /pausa, /reanudar en todas; un
    valor (/moneda NVDA) en las que lo tienen; un número (/aprobar 3) en la única que lo tiene."""
    if name in SYMBOL_COMMANDS:
        code = next((a for a in args if not a.isdigit()), None)
        if code is None and name == "moneda":
            return f"Elige un valor (o escribe /moneda {e(_example(cfgs[0]))}):"
        if code is None and name == "grafico":
            return f"Uso: /grafico VALOR (p. ej. /grafico {e(_example(cfgs[0]))})"
        if code is not None:
            have = [c for c in cfgs if _coin(c, code)]
            if not have:
                codes = dict.fromkeys(a for c in cfgs for a in c["news"]["assets"])
                return "Valor no soportado: " + e(", ".join(codes))
            if name == "grafico":  # un solo gráfico: el de la cartera con el mercado abierto
                return _section(sorted(have, key=lambda c: not session.is_open(c, now))[0], name, args, now)
            cfgs = have
    elif name in ID_COMMANDS:
        if len(args) != 1 or not re.fullmatch(r"\d+", args[0], re.ASCII):
            return f"Uso: /{name} {ID} (número del aviso o de /propuestas)"
        hits = []
        for c in cfgs:
            try:
                with _open(c) as con_x:
                    if con_x.execute("SELECT 1 FROM params_history WHERE id = ? AND status = ?",
                                     (int(args[0]), ID_COMMANDS[name])).fetchone():
                        hits.append(c)
            except Exception as exc:
                log.debug("Telegram: propuestas de %s no disponibles (%s)", portfolio.word(c),
                          type(exc).__name__)
        if not hits:
            what = "ningún cambio aplicado" if name == "deshacer" else "ninguna propuesta pendiente"
            return f"No hay {what} con ese id en ninguna cartera."
        if len(hits) > 1:
            opts = " o ".join(f"/{name} {e(portfolio.word(c))} {args[0]}" for c in hits)
            return f"El #{e(args[0])} existe en varias carteras: escribe {opts}."
        return _section(hits[0], name, args, now)
    elif name == "reanudar" and args != ["CONFIRMAR"]:
        return "Esto reanuda TODAS las carteras y rearma su kill-switch. Envía: /reanudar CONFIRMAR"
    return _each(cfgs, name, args, now)


def register_commands(env: Mapping[str, str] | None = None, post=http.post_json) -> bool:
    """Publica la lista de comandos en el menú de Telegram (solo para tu chat)."""
    env = os.environ if env is None else env
    token, chat = env.get("TELEGRAM_BOT_TOKEN"), env.get("TELEGRAM_CHAT_ID")
    if not (token and chat):
        return False
    payload = {"commands": [{"command": name, "description": desc[:256]}
                            for name, (_, _, desc, _) in _commands().items()],
               "scope": {"type": "chat", "chat_id": int(chat) if re.fullmatch(r"-?\d+", chat) else chat}}
    try:
        post(API.format(token=token, method="setMyCommands"), payload)
        return True
    except Exception as exc:  # sin el mensaje de la excepción: la URL lleva el token
        log.warning("Telegram: setMyCommands falló (%s)", _why(exc))
        return False


def poll(con: sqlite3.Connection, cfg: dict, env: Mapping[str, str] | None = None,
         get=http.get_json, post=http.post_json, wait: int = 0) -> int:
    """Procesa los comandos pendientes. Con `wait` > 0 (long polling) Telegram retiene la
    petición hasta `wait` s y la devuelve en cuanto llega un mensaje. Un comando que falla
    responde un aviso y no detiene la escucha."""
    env = os.environ if env is None else env
    token, chat = env.get("TELEGRAM_BOT_TOKEN"), env.get("TELEGRAM_CHAT_ID")
    if not (token and chat):
        return 0
    kv = db.KV(con)
    offset = kv.get("tg:offset", "0")
    query = urllib.parse.urlencode({"allowed_updates": json.dumps(["message", "callback_query"])})
    url = API.format(token=token, method="getUpdates") + f"?{query}&offset={offset}&timeout={wait}"
    data = get(url, timeout=wait + 5) if wait else get(url)
    handled = 0
    for u in data.get("result", []):
        kv.set("tg:offset", str(u["update_id"] + 1))
        if cq := u.get("callback_query"):
            kind = (cq.get("data") or "")[:2]  # o: orden ✅/❌ · m: mejora ✅/❌ · g: 📈 gráfico · r: 🔄
            handled += {"o:": _order, "m:": _params_button, "g:": _chart}.get(kind, _refresh)(
                con, cfg, cq, chat, env, post)
            continue
        msg = u.get("message") or {}
        if str((msg.get("chat") or {}).get("id")) != str(chat):
            continue  # solo obedece a tu chat
        if not isinstance(text := msg.get("text"), str) or not text.strip():
            continue  # fotos, stickers...
        name = text.split()[0][1:].split("@")[0].lower() if text.startswith("/") else ""
        sent = datetime.fromtimestamp(msg.get("date") or 0, UTC)
        if (accept := _yes_no(text)) is not None and (stocks := _stocks_cfgs(cfg)):
            if datetime.now(UTC) - sent > timedelta(minutes=5):
                send("Esa respuesta tiene más de 5 minutos (el bot no estaba escuchando); no la aplico. "
                     "Si la propuesta sigue pendiente, pulsa ✅ o ❌ en ella.", env, post)
            else:
                _answer_latest(stocks, accept, sent, env, post,
                               (msg.get("reply_to_message") or {}).get("message_id"))
            handled += 1
            continue
        if ALIASES.get(name, name) in STALE_SENSITIVE and \
                datetime.now(UTC) - sent > timedelta(minutes=5):
            send("Ese comando tiene más de 5 minutos (el bot no estaba escuchando); "
                 "no lo ejecuto. Reenvíalo si sigue haciendo falta.", env, post)
            handled += 1
            continue
        try:
            reply = handle(con, cfg, text)
        except Exception as exc:
            log.warning("Telegram: el comando falló (%s)", type(exc).__name__)
            reply = ERROR
        send(reply, env, post, html=True, markup=_markup(text, cfg))
        handled += 1
    return handled


def _markup(text: str, cfg: dict) -> dict | None:
    """Botones de la respuesta: una fila de monedas para /moneda a secas, 🔄 en las consultas."""
    parts = text.split()
    name = parts[0][1:].split("@")[0].lower() if text.startswith("/") else ""
    name = ALIASES.get(name, name)
    if many := _unified(cfg, parts[1:]):  # vista conjunta: valores de todas, botón para ver cada una
        if name == "moneda" and not parts[1:]:
            codes = dict.fromkeys(config_mod.asset(x) for c in many for x in c["general"]["symbols"])
            data = [d for x in codes if len((d := f"r:/moneda {x}").encode()) <= 64]
            return {"inline_keyboard": [[{"text": d.rsplit(" ", 1)[1], "callback_data": d}
                                         for d in data[i:i + 5]] for i in range(0, len(data), 5)]}
        kb = _refresh_button(text)
        if kb and name == "estado":
            kb["inline_keyboard"][0] += _switch_buttons("")
        return kb
    target, args = _route(cfg, parts[1:])
    cur = target or cfg
    if name == "moneda" and not args:  # monedas (o valores) de la cartera elegida
        with portfolio.active(cur):
            data = [d for x in cur["general"]["symbols"]
                    if len((d := "r:" + portfolio.cmd("moneda", config_mod.asset(x))).encode()) <= 64]
        return {"inline_keyboard": [[{"text": d.rsplit(" ", 1)[1], "callback_data": d}
                                     for d in data[i:i + 5]] for i in range(0, len(data), 5)]}
    kb = _refresh_button(text)
    if kb and name == "estado" and len(portfolio.REGISTRY) > 1:  # cambiar de cartera
        kb["inline_keyboard"][0] += _switch_buttons(portfolio.word(cur))
        if _unified(cfg, []):  # y volver a la vista conjunta
            kb["inline_keyboard"][0].append({"text": "🌐 Ver todo", "callback_data": "r:/estado"})
    return kb


def _switch_buttons(current: str) -> list[dict]:
    words = dict.fromkeys([portfolio.MAIN, *portfolio.REGISTRY])
    out = []
    for w in words:
        c = portfolio.REGISTRY.get(w)
        if w == current or (c is not None and not _enabled(c)):
            continue
        lbl = "🪙" if w == portfolio.MAIN else (portfolio.label(portfolio.REGISTRY[w]).split() or [""])[0]
        out.append({"text": f"{lbl} Ver {w}".strip(), "callback_data": f"r:/estado {w}"[:64]})
    return out


def _why(exc: Exception) -> str:
    """Tipo de error y, si Telegram lo explica, su motivo (nunca la URL: lleva el token)."""
    why = type(exc).__name__
    if hasattr(exc, "read") and hasattr(exc, "code"):
        try:
            why += f" {exc.code}: {json.loads(exc.read()).get('description', '')[:120]}"
        except Exception:
            why += f" {exc.code}"
    return why


def _refresh_button(text: str) -> dict | None:
    """Botón 🔄 para los comandos de consulta; callback_data admite 64 bytes."""
    name = text.split()[0][1:].split("@")[0].lower() if text.startswith("/") else ""
    if ALIASES.get(name, name) not in REFRESHABLE:
        return None
    data = "r:" + " ".join(text.split())[:60]
    while len(data.encode()) > 64:
        data = data[:-1]
    return {"inline_keyboard": [[{"text": "🔄 Actualizar", "callback_data": data}]]}


def _refresh(con, cfg, cq: dict, chat: str, env, post) -> int:
    """Pulsación de 🔄: rehace el comando y edita el mismo mensaje (solo consultas, solo tu chat)."""
    msg, data = cq.get("message") or {}, cq.get("data") or ""
    token = env["TELEGRAM_BOT_TOKEN"]
    _answer(cq, "Actualizado", env, post)
    if str((msg.get("chat") or {}).get("id")) != str(chat) or not data.startswith("r:/"):
        return 0
    text = data[2:]
    if _refresh_button(text) is None:  # solo comandos de consulta: nunca /aprobar, /pausa...
        return 0
    try:
        reply = handle(con, cfg, text)
    except Exception as exc:
        log.warning("Telegram: el comando falló (%s)", type(exc).__name__)
        reply = ERROR
    stamp = f"\n<i>actualizado a las {_local(cfg):%H:%M:%S}</i>"
    try:
        post(API.format(token=token, method="editMessageText"),
             {"chat_id": chat, "message_id": msg.get("message_id"),
              "text": chunks(reply, LIMIT - 60)[0] + stamp, "parse_mode": "HTML",
              "link_preview_options": {"is_disabled": True}, "reply_markup": _markup(text, cfg)})
    except Exception as exc:  # p. ej. el mensaje es demasiado antiguo para editarlo
        why = _why(exc)
        if "not modified" in why:  # mismo contenido: no hay nada que cambiar
            return 1
        log.warning("Telegram: no se pudo editar el mensaje (%s)", why)
        send(reply, env, post, html=True, markup=_markup(text, cfg))
    return 1


def _answer(cq: dict, text: str, env, post) -> None:
    """answerCallbackQuery: quita el reloj del botón y muestra `text` un instante."""
    try:
        post(API.format(token=env["TELEGRAM_BOT_TOKEN"], method="answerCallbackQuery"),
             {"callback_query_id": cq.get("id"), "text": text})
    except Exception as exc:
        log.warning("Telegram: answerCallbackQuery falló (%s)", _why(exc))


# --- propuestas de bolsa: ✅/❌ y respuestas "sí"/"no" -----------------------------------------
def _yes_no(text: str) -> bool | None:
    t = text.strip().lower().replace("️", "").strip(" .!¡")
    return True if t in YES else False if t in NO else None


def _confirm(cfg: dict, pid: int, accept: bool) -> str:
    from newsbot.stocks import engine  # diferido: solo hace falta con carteras de bolsa
    return engine.confirm(cfg, pid, accept)


def _portfolio_cfg(cfg: dict, w: str) -> dict | None:
    """cfg de la cartera de bolsa de una palabra ("" = la principal); None si no hay ninguna."""
    w = (w or portfolio.MAIN).lower()
    c = cfg if w == portfolio.word(cfg).lower() else \
        {k.lower(): v for k, v in portfolio.REGISTRY.items()}.get(w)
    return c if _is_stocks(c) else None


def _proposal_base(cfg: dict, pid: int, text: str | None) -> str:
    """Texto de la propuesta para editar su mensaje: el de stocks.engine con la fila actual o, si
    falla, el original escapado (sin la etiqueta: edit_message la vuelve a poner)."""
    try:
        from newsbot.stocks import engine
        with _open(cfg) as con:
            row = con.execute("SELECT * FROM proposals WHERE id = ?", (pid,)).fetchone()
        if row is not None:
            return engine.proposal_text(row, cfg)
    except Exception as exc:
        log.debug("Telegram: texto de la propuesta %s no disponible (%s)", pid, type(exc).__name__)
    text, lbl = text or f"Propuesta {pid}", portfolio.LABEL.get()
    return e(text[len(lbl) + 3:] if lbl and text.startswith(f"{lbl} · ") else text)


def _close(cfg: dict, pid: int, msg_id, text: str | None, result: str, env, post) -> None:
    """Edita el mensaje de la propuesta: sin botones y con el resultado debajo."""
    edit_message(msg_id, f"{_proposal_base(cfg, pid, text)}\n\n{result}", env, post)


def _order(con, cfg: dict, cq: dict, chat: str, env, post) -> int:
    """Pulsación de ✅/❌ en una propuesta de bolsa (callback "o:<palabra>:<id>:y|n"): la confirma o
    la descarta en su cartera (stocks.engine.confirm) y edita el mensaje. Solo tu chat; un fallo
    responde un aviso y no detiene la escucha."""
    msg, data = cq.get("message") or {}, cq.get("data") or ""
    if str((msg.get("chat") or {}).get("id")) != str(chat):
        return 0  # solo obedece a tu chat
    m = re.fullmatch(r"o:([^:]*):(\d+):([yn])", data, re.ASCII)
    if not (m and (target := _portfolio_cfg(cfg, m.group(1)))):
        _answer(cq, "Propuesta desconocida", env, post)
        return 1
    pid, accept = int(m.group(2)), m.group(3) == "y"
    try:  # el botón tiene que ser el del mensaje de ESTA propuesta: uno viejo (otra base, la prueba en
        with _open(target) as con_x:  # papel) con el mismo número nunca confirma una orden real
            row = con_x.execute("SELECT msg_id FROM proposals WHERE id = ?", (pid,)).fetchone()
    except Exception:
        row = None
    if row is not None and row["msg_id"] and msg.get("message_id") and row["msg_id"] != msg.get("message_id"):
        _answer(cq, "Botón de un mensaje antiguo: no hago nada", env, post)
        old = e(msg.get("text") or "") + "\n\n<i>(botón antiguo: sin efecto)</i>"
        edit_message(msg.get("message_id"), old, env, post)
        return 1
    # Telegram descarta las respuestas tardías al botón y la orden puede tardar ~30 s: se contesta ya
    _answer(cq, "Procesando…" if accept else "Descartando…", env, post)
    with portfolio.active(target):
        try:
            result = _confirm(target, pid, accept)
        except Exception as exc:
            log.warning("Telegram: la confirmación falló (%s)", type(exc).__name__)
            send(ERROR, env, post, html=True)
            return 1
        if result.startswith("Ya respondida") and not any(w in result for w in ("caducada", "fallida")):
            return 1  # doble pulsación: el mensaje ya tiene el resultado
        _close(target, pid, msg.get("message_id"), msg.get("text"), result, env, post)
    return 1


def _params_button(con, cfg: dict, cq: dict, chat: str, env, post) -> int:
    """✅/❌ de una mejora de parámetros (autopsia de una pérdida o aprendizaje), callback
    "m:<palabra>:<id>:y|n": la aplica (/aprobar) o la descarta (/rechazar) en su cartera y edita el
    mensaje con el resultado. Solo tu chat."""
    msg, data = cq.get("message") or {}, cq.get("data") or ""
    if str((msg.get("chat") or {}).get("id")) != str(chat):
        return 0
    m = re.fullmatch(r"m:([^:]*):(\d+):([yn])", data, re.ASCII)
    w = (m.group(1) if m else "").lower() or portfolio.MAIN
    target = cfg if w == portfolio.word(cfg).lower() else \
        {k.lower(): v for k, v in portfolio.REGISTRY.items()}.get(w)
    if not (m and target):
        _answer(cq, "Mejora desconocida", env, post)
        return 1
    accept = m.group(3) == "y"
    _answer(cq, "Aplicando…" if accept else "Descartando…", env, post)
    with portfolio.active(target), _open(target) as con_x:
        try:
            result = _run(con_x, target, "aprobar" if accept else "rechazar", [m.group(2)], datetime.now(UTC))
        except Exception as exc:
            log.warning("Telegram: la mejora falló (%s)", type(exc).__name__)
            result = ERROR
        lbl = portfolio.LABEL.get()
        text = msg.get("text") or ""
        text = text[len(lbl) + 3:] if lbl and text.startswith(f"{lbl} · ") else text
        icon = "✅" if accept and "aplicada" in result else "❌" if not accept else "⚠️"
        if not edit_message(msg.get("message_id"), f"{e(text)}\n\n{icon} {result}", env, post):
            send(result, env, post, html=True)
    return 1


def _chart(con, cfg: dict, cq: dict, chat: str, env, post) -> int:
    """Pulsación de 📈 en una propuesta (callback "g:<palabra>:<CÓDIGO>"): envía el gráfico."""
    msg, data = cq.get("message") or {}, cq.get("data") or ""
    if str((msg.get("chat") or {}).get("id")) != str(chat):
        return 0  # solo obedece a tu chat
    m = re.fullmatch(r"g:([^:]*):([A-Za-z0-9.\-]{1,20})", data, re.ASCII)
    if not (m and (target := _portfolio_cfg(cfg, m.group(1)))):
        _answer(cq, "Gráfico no disponible", env, post)
        return 1
    _answer(cq, "Dibujando el gráfico…", env, post)
    with portfolio.active(target), _open(target) as con_x:
        try:
            reply = send_chart(con_x, target, m.group(2), env, post)
        except Exception as exc:
            log.warning("Telegram: el gráfico falló (%s)", type(exc).__name__)
            reply = ERROR
        if not reply.startswith("📈"):  # si la imagen salió, no hace falta otro mensaje
            send(reply, env, post, html=True)
    return 1


def _answer_latest(stocks: list[dict], accept: bool, sent: datetime, env, post,
                   reply_to: int | None = None) -> None:
    """Respuesta de texto ("sí"/"no"). Si respondes al mensaje de una propuesta, decide esa; si no, solo
    actúa cuando hay UNA pendiente que ya te había llegado antes de escribir (nunca una posterior)."""
    now, cands = datetime.now(UTC), []
    for c in stocks:
        path = Path(c["general"]["data_dir"]) / "newsbot.db"
        if not path.exists():
            continue
        try:
            with closing(db.connect(path)) as con:
                rows = db.pending_proposals(con, "")  # la caducidad se compara aquí, con zona horaria
        except Exception as exc:
            log.warning("Telegram: propuestas de %s no disponibles (%s)", portfolio.word(c),
                        type(exc).__name__)
            continue
        for r in rows:
            sent_at, expires = _dt(r["sent_at"]), _dt(r["expires"])
            if r["msg_id"] and sent_at and expires and expires > now and sent_at < sent:
                cands.append((c, r))
    if reply_to is not None:
        cands = [(c, r) for c, r in cands if r["msg_id"] == reply_to]
        if not cands:
            send("Ese mensaje no es una propuesta pendiente: no hago nada.", env, post)
            return
    if not cands:
        send(NO_PENDING, env, post)
        return
    if len(cands) > 1:
        send(f"Hay {len(cands)} propuestas pendientes: pulsa ✅/❌ en la que quieras o responde «sí»/«no» "
             "a su mensaje. No hago nada.", env, post)
        return
    c, row = cands[0]
    with portfolio.active(c):
        try:
            result = _confirm(c, row["id"], accept)
        except Exception as exc:
            log.warning("Telegram: la confirmación falló (%s)", type(exc).__name__)
            send(ERROR, env, post, html=True)
            return
        if row["msg_id"]:
            _close(c, row["id"], row["msg_id"], None, result, env, post)
        send(result, env, post, html=True)
