"""Autopsia de cada pérdida y la mejora que propone: el usuario solo tiene que aceptarla con ✅.

Tras CUALQUIER cierre con pérdida (aunque sea de céntimos: venta confirmada, stop del broker,
take-profit/trailing que acaba en negativo o venta a mano) se reúnen los hechos:
- por qué compró: el 💭 de la propuesta, la señal frente al umbral y los componentes que la empujaron;
- qué decían las noticias entonces y qué cambió después (componentes al entrar y ahora);
- si cayó todo el mercado o solo el valor, cuánto duró y cuánto se llevaron las comisiones.
Con eso se elige UNA mejora de un menú cerrado de cambios seguros (MENU): cada una es un override
de parámetros con topes que config.apply valida. Claude (si hay ANTHROPIC_API_KEY) redacta el
diagnóstico y elige del menú; si no está o falla, decide la regla local. La mejora se guarda en
params_history como 'proposed' (origen "postmortem") y llega a Telegram con ✅ Aplicar / ❌ No
(callback "m:<palabra>:<id>:y|n"). Aplicada, rige desde el siguiente ciclo y /deshacer la revierte.
"""
import json
import logging
import os
import sqlite3
from datetime import UTC, datetime, timedelta
from statistics import median
from zoneinfo import ZoneInfo

from newsbot import config as config_mod
from newsbot.news import ingest
from newsbot.storage import db

log = logging.getLogger(__name__)
STEP_THRESHOLD, MAX_THRESHOLD = 0.03, 0.45
STEP_WEIGHT = 0.05
OPEN_STEP, OPEN_MAX = 15, 60          # minutos sin comprar tras la apertura
MARKET_LEVELS = (-0.30, -0.15, 0.0)   # filtro de mercado: intradía mediano mínimo para comprar
MIN_ORDER_STEP, MIN_ORDER_MAX = 1.0, 8.0

MENU_TEXT = {
    "threshold_up": "subir el umbral de compra {a} → {b} (solo señales más claras)",
    "weight_down": "bajar el peso de {comp} {a} → {b} (se reparte entre los demás)",
    "no_buy_open": "no comprar en los primeros {b} min tras la apertura (antes {a})",
    "market_filter": "no comprar si el mercado cae: intradía mediano mínimo {b} (antes {a})",
    "min_order_up": "orden mínima {a} → {b} € (que las comisiones no se coman la ganancia)",
    "none": "ningún cambio: fue ruido normal del mercado",
}


# --- hechos ---------------------------------------------------------------------------------------
def _json(s) -> dict:
    try:
        return json.loads(s or "{}") or {}
    except ValueError:
        return {}


def _tick(con: sqlite3.Connection, sym: str, ts: int, last: bool = False):
    q = ("SELECT ts, price, signal, raw_json, contributions_json FROM ticks WHERE symbol = ? "
         "AND action != 'BACKFILL' "
         + ("ORDER BY id DESC LIMIT 1" if last else "AND ts >= ? ORDER BY id LIMIT 1"))
    return con.execute(q, (sym,) if last else (sym, ts)).fetchone()


def facts(con: sqlite3.Connection, cfg: dict, sym: str, opened: str | None, avg: float, exit_price: float,
          qty: float, fees: float, why: str, now: datetime) -> dict:
    """Todo lo que se sabe de la operación perdedora, en un dict serializable."""
    start = datetime.fromisoformat(opened) if opened else now
    ts0 = int(start.timestamp())
    buy = con.execute("SELECT * FROM proposals WHERE symbol = ? AND side = 'buy' AND status = 'executed' "
                      "AND created <= ? ORDER BY id DESC LIMIT 1", (sym, now.isoformat())).fetchone()
    entry, last = _tick(con, sym, ts0), _tick(con, sym, ts0, last=True)
    raw0, raw1 = _json(entry["raw_json"]) if entry else {}, _json(last["raw_json"]) if last else {}
    contrib = _json(entry["contributions_json"]) if entry else {}
    moves = []
    for s in cfg["general"]["symbols"]:
        a, b = (_tick(con, s, ts0), _tick(con, s, ts0, last=True)) if s != sym else (None, None)
        if a and b and a["price"]:
            moves.append(b["price"] / a["price"] - 1)
    code = config_mod.asset(sym)
    words = (cfg["news"].get("assets") or {}).get(code) or [code]
    news = []
    utc0 = start.astimezone(UTC)  # headlines.published se guarda en UTC ("...+00:00")
    for r in con.execute("SELECT title, source, published FROM headlines WHERE published >= ? "
                         "AND published <= ? ORDER BY published DESC LIMIT 400",
                         ((utc0 - timedelta(hours=24)).isoformat(), utc0.isoformat())):
        h = type("H", (), {"title": r["title"], "summary": ""})()
        if ingest.mentions(h, words):
            news.append(f"[{r['source']}] {r['title']}")
        if len(news) >= 6:
            break
    st = cfg["strategy"]
    sess = cfg.get("session") or {}
    minutes_after_open = None
    if sess.get("open"):
        local = start.astimezone(ZoneInfo(sess["tz"]))
        h, m = (int(x) for x in sess["open"].split(":"))
        minutes_after_open = round((local - local.replace(hour=h, minute=m, second=0, microsecond=0))
                                   .total_seconds() / 60)
    invested = qty * avg
    pnl = (exit_price - avg) * qty - fees
    return {
        "symbol": code, "exit_reason": why, "hours_held": round((now - start).total_seconds() / 3600, 1),
        "entry_price": round(avg, 4), "exit_price": round(exit_price, 4), "invested_eur": round(invested, 2),
        "pnl_eur": round(pnl, 2), "ret_pct": round((exit_price / avg - 1) * 100, 2) if avg else 0.0,
        "fees_eur": round(fees, 3), "fees_share_of_loss": round(fees / abs(pnl), 2) if pnl < 0 else None,
        "market_median_move_pct": round(median(moves) * 100, 2) if moves else None,
        "minutes_after_open": minutes_after_open,
        "buy_thought": buy["thought"] if buy and "thought" in buy.keys() else None,
        "buy_reason": buy["reason"] if buy else None,
        "buy_signal": round(buy["signal"], 3) if buy and buy["signal"] is not None else None,
        "buy_threshold": st["buy_threshold"],
        "regime_at_entry": raw0.get("_regime"),
        "contributions_at_entry": {k: round(v, 3) for k, v in contrib.items() if abs(v) >= 0.005},
        "components_at_entry": {k: round(v, 3) for k, v in raw0.items() if isinstance(v, int | float)
                                and not k.startswith("_")},
        "components_now": {k: round(v, 3) for k, v in raw1.items() if isinstance(v, int | float)
                           and not k.startswith("_")},
        "market_intraday_at_entry": _market_intraday(con, cfg, ts0),
        "headlines_before_entry": news,
        "weights": {k: v for k, v in cfg["weights"].items() if v},
        "no_buy_first_minutes": st.get("no_buy_first_minutes", 0),
        "min_market_intraday": st.get("min_market_intraday"),
        "min_order_eur": cfg["risk"]["min_order_eur"],
    }


def _market_intraday(con: sqlite3.Connection, cfg: dict, ts: int) -> float | None:
    vals = []
    for s in cfg["general"]["symbols"]:
        t = _tick(con, s, ts)
        if t and isinstance(v := _json(t["raw_json"]).get("intraday"), int | float):
            vals.append(v)
    return round(median(vals), 3) if vals else None


# --- menú de mejoras -----------------------------------------------------------------------------
def option(cfg: dict, kind: str, comp: str | None = None) -> dict | None:
    """Override concreto de una mejora del menú (None si no aplica o ya está en el tope)."""
    st, w = cfg["strategy"], cfg["weights"]
    if kind == "threshold_up":
        a = st["buy_threshold"]
        if a + STEP_THRESHOLD > MAX_THRESHOLD:
            return None
        bear = round(st["buy_threshold_bear"] + STEP_THRESHOLD, 3)
        return {"kind": kind, "a": a, "b": round(a + STEP_THRESHOLD, 3),
                "override": {"strategy": {"buy_threshold": round(a + STEP_THRESHOLD, 3),
                                          "buy_threshold_bear": bear}}}
    if kind == "weight_down":
        if not comp or w.get(comp, 0.0) <= 0:
            return None
        d = min(STEP_WEIGHT, w[comp])
        others = {k: v for k, v in w.items() if k != comp and v > 0}
        total = sum(others.values())
        if total <= 0:
            return None
        new = {k: round(v + d * v / total, 4) for k, v in others.items()} | {comp: round(w[comp] - d, 4)}
        top = max(others, key=others.get)
        new[top] = round(new[top] + 1 - sum({**w, **new}.values()), 4)  # redondeo: suma exacta 1
        return {"kind": kind, "comp": comp, "a": w[comp], "b": new[comp], "override": {"weights": new}}
    if kind == "no_buy_open":
        a = int(st.get("no_buy_first_minutes", 0))
        if a + OPEN_STEP > OPEN_MAX:
            return None
        return {"kind": kind, "a": a, "b": a + OPEN_STEP,
                "override": {"strategy": {"no_buy_first_minutes": a + OPEN_STEP}}}
    if kind == "market_filter":
        a = st.get("min_market_intraday")
        nxt = next((x for x in MARKET_LEVELS if a is None or x > a), None)
        if nxt is None:
            return None
        return {"kind": kind, "a": "sin filtro" if a is None else a, "b": nxt,
                "override": {"strategy": {"min_market_intraday": nxt}}}
    if kind == "min_order_up":
        a = cfg["risk"]["min_order_eur"]
        if a + MIN_ORDER_STEP > MIN_ORDER_MAX:
            return None
        return {"kind": kind, "a": a, "b": a + MIN_ORDER_STEP,
                "override": {"risk": {"min_order_eur": a + MIN_ORDER_STEP}}}
    if kind == "none":
        return {"kind": kind, "a": None, "b": None, "override": {}}
    return None


def menu(cfg: dict, f: dict) -> list[dict]:
    """Las mejoras posibles para ESTA pérdida (el componente a bajar es el que más empujó la compra)."""
    top = max(f["contributions_at_entry"].items(), key=lambda kv: kv[1], default=(None, 0))[0]
    out = [option(cfg, "threshold_up"), option(cfg, "weight_down", top), option(cfg, "no_buy_open"),
           option(cfg, "market_filter"), option(cfg, "min_order_up"), option(cfg, "none")]
    return [o for o in out if o]


def rule_choice(f: dict, opts: list[dict]) -> tuple[str, str]:
    """Elección local (sin Claude): (kind, diagnóstico)."""
    kinds = {o["kind"] for o in opts}
    mkt, ret = f["market_median_move_pct"], f["ret_pct"]
    flipped = [k for k, v in f["components_at_entry"].items() if v > 0.05
               and f["components_now"].get(k, 0) < 0 and f["contributions_at_entry"].get(k, 0) > 0]
    margin = (f["buy_signal"] or 0) - f["buy_threshold"]
    if f["fees_share_of_loss"] and f["fees_share_of_loss"] > 0.5 and "min_order_up" in kinds:
        return "min_order_up", ("La pérdida fue sobre todo comisión de cambio de divisa: la orden era "
                                "demasiado pequeña.")
    if f["minutes_after_open"] is not None and 0 <= f["minutes_after_open"] < 30 and "no_buy_open" in kinds:
        return "no_buy_open", (f"Compré {f['minutes_after_open']} min después de la apertura, cuando el "
                               "precio es más errático; la subida de la apertura se deshizo.")
    if mkt is not None and mkt < 0 and mkt <= ret / 2 and "market_filter" in kinds:
        return "market_filter", (f"Cayó casi todo el mercado ({mkt:+.1f} %): compré con el conjunto "
                                 "girándose a la baja.")
    if margin < 0.05 and "threshold_up" in kinds:
        return "threshold_up", (f"La señal ({f['buy_signal']:+.2f}) apenas superaba el umbral "
                                f"({f['buy_threshold']:+.2f}): era una compra dudosa.")
    if flipped and "weight_down" in kinds:
        return "weight_down", f"Lo que justificó la compra ({', '.join(flipped)}) se dio la vuelta enseguida."
    return "none", ("Pérdida pequeña dentro de lo normal para un mercado volátil; no hay un patrón claro "
                    "que corregir.")


PROMPT = """Eres el analista de riesgos de un bot de bolsa que opera acciones de EE. UU. con muy poco dinero.
Una operación ha cerrado con pérdida. Estudia los hechos y explica en español, en 2-4 frases claras para
un no experto, POR QUÉ el bot decidió comprar y POR QUÉ se perdió (¿fue la decisión, el momento, el
mercado entero, una noticia que cambió, las comisiones o simple ruido?). Después elige UNA mejora del
menú (por su "kind") que habría evitado esta pérdida sin dejar al bot sin operar; elige "none" si fue
ruido normal. No inventes datos.

Responde SOLO con JSON: {"diagnosis": "<2-4 frases>", "kind": "<uno del menú>", "lesson": "<1 frase>"}

Hechos:
{facts}

Menú de mejoras posibles:
{menu}"""


def claude_choice(f: dict, opts: list[dict], model: str) -> tuple[str, str, str] | None:
    """(kind, diagnóstico, lección) de Claude, o None si no hay clave o falla."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return None
    try:
        import anthropic
        lines = "\n".join(f'- {o["kind"]}: {describe(o)}' for o in opts)
        facts_json = json.dumps(f, ensure_ascii=False, indent=1)
        prompt = PROMPT.replace("{facts}", facts_json).replace("{menu}", lines)
        msg = anthropic.Anthropic(timeout=40.0).messages.create(
            model=model, max_tokens=1024, output_config={"effort": "low"},
            messages=[{"role": "user", "content": prompt}])
        raw = "".join(b.text for b in msg.content if b.type == "text")
        data = json.loads(raw[raw.index("{"): raw.rindex("}") + 1])
        kind = data.get("kind")
        if kind not in {o["kind"] for o in opts}:
            return None
        return kind, str(data.get("diagnosis", ""))[:700], str(data.get("lesson", ""))[:200]
    except Exception as exc:
        log.warning("Autopsia con Claude no disponible (%s)", type(exc).__name__)
        return None


def describe(o: dict) -> str:
    return MENU_TEXT[o["kind"]].format(a=o.get("a"), b=o.get("b"), comp=o.get("comp"))


# --- propuesta -----------------------------------------------------------------------------------
def run(con: sqlite3.Connection, cfg: dict, sym: str, opened: str | None, avg: float, exit_price: float,
        qty: float, fees: float, why: str, now: datetime, use_claude: bool = True) -> dict:
    """Autopsia completa. Devuelve {"facts", "kind", "pid" (propuesta o None), "diagnosis", ...} y guarda
    la propuesta. `cfg` puede ser el de la cartera o el de un tick: se parte de los parámetros en vigor
    (las propuestas aplicadas) SIN el ajuste del umbral adaptativo, que no es un parámetro."""
    if off := cfg.get("_adaptive"):
        st = cfg["strategy"]
        cfg = cfg | {"strategy": st | {"buy_threshold": st["buy_threshold"] - off,
                                       "buy_threshold_bear": st["buy_threshold_bear"] - off}}
    try:
        cfg = config_mod.apply(cfg, db.active_params(con, cfg["general"]["mode"])[1])
    except ValueError:
        pass
    f = facts(con, cfg, sym, opened, avg, exit_price, qty, fees, why, now)
    opts = menu(cfg, f)
    model = cfg["sentiment"].get("llm_model", "claude-haiku-5-5")
    got = claude_choice(f, opts, model) if use_claude else None
    kind, diagnosis = (got[0], got[1]) if got else rule_choice(f, opts)
    lesson = got[2] if got else ""
    chosen = next(o for o in opts if o["kind"] == kind)
    pid = None
    active_id, active = db.active_params(con, cfg["general"]["mode"])
    if chosen["override"]:
        try:
            config_mod.apply(cfg, active | chosen["override"])  # nunca se propone algo que no valide
        except ValueError as exc:
            log.warning("Mejora %s inválida (%s): no se propone", kind, exc)
            chosen = next(o for o in opts if o["kind"] == "none")
    if chosen["override"]:
        note = {"origin": "postmortem", "based_on": active_id, "mode": cfg["general"]["mode"], "auto": False,
                "symbol": f["symbol"], "kind": chosen["kind"], "pnl_eur": f["pnl_eur"],
                "diagnosis": diagnosis, "by": "claude" if got else "reglas"}
        with con:
            pid = con.execute("INSERT INTO params_history(created, params_json, status, note) "
                              "VALUES (?,?,?,?)",
                              (now.isoformat(), json.dumps(chosen["override"]), "proposed",
                               json.dumps(note, ensure_ascii=False))).lastrowid
    kv = db.KV(con)
    try:
        saved = json.loads(kv.get("postmortems") or "[]")
    except ValueError:
        saved = []
    entry = {"ts": now.isoformat(), "symbol": f["symbol"], "pnl": f["pnl_eur"], "kind": chosen["kind"],
             "pid": pid, "diagnosis": diagnosis}
    kv.set("postmortems", json.dumps((saved + [entry])[-50:], ensure_ascii=False))
    return {"facts": f, "kind": chosen["kind"], "pid": pid, "diagnosis": diagnosis, "lesson": lesson,
            "change": describe(chosen), "by": "Claude" if got else "reglas"}
