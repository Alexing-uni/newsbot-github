"""Pensamiento del bot (qué planea y por qué) y repaso de cada pérdida ("🧐 Por qué perdí").

Todo sale de lo que el motor ya tiene (señales, componentes, posiciones y la tabla ticks): no gasta
Claude. Las pistas de una pérdida son heurísticas, no certezas; el ajuste de pesos lo hace learn con
todos los ciclos, y las lecciones quedan en el KV "lessons" para /pensamiento.
"""
import json
import sqlite3
from bisect import bisect_left
from datetime import datetime
from statistics import median

from newsbot.storage import db

LESSONS = 30  # lecciones guardadas


def _pct(x: float) -> str:
    return f"{x * 100:+.1f} %".replace("-", "−")


def _eur(x: float) -> str:
    return f"{x:+.2f} €".replace("-", "−")


def round_trip(cfg: dict) -> float:
    """Coste de comprar y vender (comisión + deslizamiento, las dos patas) en tanto por uno."""
    c = cfg["costs"]
    return 2 * (c["fee_pct"] + c["slippage_pct"])


def buy_thought(cfg: dict, plan: dict, drivers: str, feats: dict) -> str:
    """Por qué compra, cuánto puede ganar y cuánto perder (comisiones incluidas)."""
    eur, price, stop = plan["eur"], plan["price"], plan.get("stop") or 0.0
    costs, tp = eur * round_trip(cfg), cfg["risk"].get("take_profit_pct", 0.0)
    out = [f"Compro por {drivers.rstrip('…')}."]
    if tp > 0:
        out.append(f"Objetivo {_pct(tp)} ({price * (1 + tp):.2f} €): ≈ {_eur(eur * tp - costs)} netos.")
    if stop and price:
        out.append(f"Si toca el stop pierdo ≈ {_eur(-(eur * (1 - stop / price) + costs))}.")
    out.append(f"Comisiones ida y vuelta ≈ {costs:.2f} €.")
    if k := feats.get("kronos"):
        out.append(f"Kronos prevé {'subida' if k > 0 else 'bajada'} en 2 h ({k:+.2f}).")
    if (d := feats.get("dip", 0.0)) > 0.3:
        out.append(f"Viene de una caída fuerte con tendencia de fondo alcista (rebote {d:+.2f}).")
    return " ".join(out)


def sell_thought(cfg: dict, pos: dict, price: float, reason: str) -> str:
    qty, avg = pos["qty"], pos.get("avg_entry") or 0.0
    pnl = qty * (price - avg) - qty * price * round_trip(cfg) / 2
    ret = f" ({_pct(price / avg - 1)})" if avg > 0 else ""
    return f"Vendo: {reason}. Resultado estimado {_eur(pnl)}{ret} tras comisiones."


def thinking(cfg: dict, mk: dict, ds: dict, reasons: dict, positions: dict, plans: list[dict],
             ranking: list[str], cash: float, equity: float, is_open: bool, buy_th: float,
             names: dict[str, str]) -> list[str]:
    """Líneas (texto plano) de lo que el bot ve y planea en este ciclo."""
    code = lambda s: s.split("/")[0]  # noqa: E731
    tier = f" · nivel {cfg['_tier']}" if cfg.get("_tier") else ""
    lines = [f"Capital {equity:.2f} € · efectivo {cash:.2f} €{tier}"]
    if off := cfg.get("_adaptive"):
        lines.append(f"Umbral de compra {buy_th:+.2f} (ajustado {off:+.2f} por mis últimos resultados)")
    if mk:
        med = median(m["feats"]["intraday"] for m in mk.values())
        mood = "subiendo" if med > 0.15 else "bajando" if med < -0.15 else "plano"
        lines.append(f"Mercado {mood} (intradía mediano {med:+.2f})"
                     + ("" if is_open else " · cerrado: solo observo"))
    for p in plans:
        verb = "comprar" if p["side"] == "buy" else "vender"
        lines.append(f"→ Propongo {verb} {code(p['symbol'])}: {p.get('thought') or p['reason']}")
    tp = cfg["risk"].get("take_profit_pct", 0.0)
    for s, pos in positions.items():
        avg, px = pos.get("avg_entry") or 0.0, mk[s]["price"] if s in mk else 0.0
        if avg <= 0 or px <= 0:
            continue
        bits = [f"{code(s)}: {_pct(px / avg - 1)} desde la entrada"]
        if pos.get("stop_price"):
            bits.append(f"stop {pos['stop_price']:.2f} € ({_pct(pos['stop_price'] / px - 1)})")
        if tp > 0:
            bits.append(f"objetivo {avg * (1 + tp):.2f} €")
        lines.append(" · ".join(bits))
    shown = 0
    for s in ranking:
        if s in positions or any(p["symbol"] == s for p in plans) or shown >= 3:
            continue
        shown += 1
        gap = buy_th - ds[s].signal
        need = (f"le falta {gap:.2f} para el umbral {buy_th:+.2f}" if gap >= 0.01
                else "en el umbral de compra" if gap > 0 else reasons.get(s, ""))
        lines.append(f"Vigilo {code(s)} {ds[s].signal:+.2f}: {need}")
    for s in mk:
        if (d := mk[s]["feats"].get("dip", 0.0)) >= 0.6 and s not in positions:
            lines.append(f"Caída fuerte en {code(s)} con tendencia de fondo alcista (rebote {d:+.2f}): "
                         "posible compra si la señal acompaña")
    if not plans:
        lines.append("Nada que proponer en este ciclo.")
    return [ln.replace(" -", " −") for ln in lines]


def dip_stats(con: sqlite3.Connection, drop: float = -0.04, days: int = 3) -> dict | None:
    """Qué pasó en el histórico (ticks BACKFILL) tras caer ≥ |drop| en 24 h: rentabilidad media a
    `days` días y cuántas veces subió. Una muestra por valor y caída (separadas ≥ 2 días). Caché diaria
    en el KV "idle:dips"; None sin histórico suficiente."""
    kv, today = db.KV(con), datetime.now().date().isoformat()
    try:
        hit = json.loads(kv.get("idle:dips") or "{}")
    except ValueError:
        hit = {}
    if hit.get("day") == today:
        return hit.get("stats")
    by: dict[str, list] = {}
    for s, ts, p in con.execute("SELECT symbol, ts, price FROM ticks WHERE action = 'BACKFILL' AND price > 0 "
                                "ORDER BY symbol, ts"):
        by.setdefault(s, []).append((ts, p))
    rets: list[float] = []
    for xs in by.values():
        tss, last = [x[0] for x in xs], float("-inf")
        for i, (ts, p) in enumerate(xs):
            j0 = bisect_left(tss, ts - 86400)
            if j0 >= i or p / xs[j0][1] - 1 > drop or ts - last < 2 * 86400:
                continue
            j = bisect_left(tss, ts + days * 86400)
            if j < len(xs):
                rets.append(xs[j][1] / p - 1)
                last = ts
    stats = ({"n": len(rets), "mean": sum(rets) / len(rets), "up": sum(r > 0 for r in rets) / len(rets),
              "drop": drop, "days": days} if len(rets) >= 20 else None)
    kv.set("idle:dips", json.dumps({"day": today, "stats": stats}))
    return stats


def daily_closes(con: sqlite3.Connection, before: str) -> dict[str, dict[str, float]]:
    """{día: {símbolo: último precio de ese día}} con todos los ticks (histórico y en vivo), solo días
    anteriores a `before` (YYYY-MM-DD): el día en curso aún no tiene cierre."""
    out: dict[str, dict[str, float]] = {}
    for s, c, p in con.execute("SELECT symbol, created, price FROM ticks WHERE price > 0 AND created < ? "
                               "ORDER BY ts, id", (before,)):
        out.setdefault(c[:10], {})[s] = p
    return out


def panic_stats(con: sqlite3.Connection, drop: float, share: float, med_drop: float,
                worst: int = 3) -> dict | None:
    """Días de pánico del histórico (al menos `share` de los valores caen ≥ |drop| frente al cierre anterior
    y la mediana cae ≥ |med_drop|): qué hicieron AL DÍA SIGUIENTE los `worst` que más cayeron y el conjunto.
    Caché diaria en el KV "panic:stats"; None si no hay ningún caso."""
    kv, today = db.KV(con), datetime.now().date().isoformat()
    try:
        hit = json.loads(kv.get("panic:stats") or "{}")
    except ValueError:
        hit = {}
    if hit.get("day") == today:
        return hit.get("stats")
    closes = daily_closes(con, today)
    days, cases = sorted(closes), []
    for i in range(1, len(days) - 1):
        a, b, c = closes[days[i - 1]], closes[days[i]], closes[days[i + 1]]
        ch = {x: b[x] / a[x] - 1 for x in b if a.get(x)}
        falling = sum(v <= drop for v in ch.values())
        if len(ch) < 8 or median(ch.values()) > med_drop or falling < share * len(ch):
            continue
        nxt = {x: c[x] / b[x] - 1 for x in ch if c.get(x)}
        low = [x for x in sorted(ch, key=ch.get)[:worst] if x in nxt]
        if low:
            cases.append({"day": days[i], "worst": sum(nxt[x] for x in low) / len(low),
                          "all": median(nxt.values())})
    stats = None
    if cases:
        w = [x["worst"] for x in cases]
        stats = {"n": len(cases), "worst_mean": sum(w) / len(w), "worst_median": median(w),
                 "worst_up": sum(x > 0 for x in w) / len(w),
                 "all_mean": sum(x["all"] for x in cases) / len(cases), "cases": cases[-8:]}
    kv.set("panic:stats", json.dumps({"day": today, "stats": stats}))
    return stats


def idle_cash(con: sqlite3.Connection, cfg: dict, mk: dict, ds: dict, ranking: list[str],
              positions: dict, cash: float, equity: float, buy_th: float, now: datetime) -> list[str]:
    """Razonamiento sobre el efectivo sin invertir (más de un 25 % del capital por encima de la caja
    mínima): cuánto, desde cuándo, qué tan cerca está la mejor compra y qué haría, con datos. Nunca
    opera: compra solo una señal que pase el umbral."""
    if equity <= 0:
        return []
    free = cash - cfg["portfolio"].get("min_cash_pct", 0.0) * equity
    if free / equity < 0.25:
        return []
    code = lambda s: s.split("/")[0]  # noqa: E731
    share = f"{free / equity:.0%} del capital, aparte de la caja mínima"
    lines = [f"💶 Dinero parado: {free:.2f} € libres ({share})"]
    last = con.execute("SELECT max(created) FROM orders WHERE side = 'buy' "
                       "AND status = 'filled'").fetchone()[0]
    idle_h = (now - datetime.fromisoformat(last)).total_seconds() / 3600 if last else None
    if idle_h is not None:
        ago = f"{idle_h:.0f} h" if idle_h < 48 else f"{idle_h / 24:.0f} días"
        lines.append(f"Última compra hace {ago}")
    day0 = now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    best = next((r for r in con.execute(
        "SELECT symbol, max(signal) FROM ticks WHERE ts >= ? AND action != 'BACKFILL' AND held = 0 "
        "GROUP BY symbol ORDER BY 2 DESC", (day0,)) if r[0] not in positions), None)
    cands = [s for s in ranking if s not in positions]
    now_best = cands[0] if cands else None
    gap = buy_th - ds[now_best].signal if now_best else None
    if best and best[1] is not None:
        lines.append(f"Mejor señal de hoy: {code(best[0])} {best[1]:+.2f} (compro desde {buy_th:+.2f})")
    med = median(m["feats"]["intraday"] for m in mk.values()) if mk else 0.0
    cost = round_trip(cfg)
    if med < -0.15:  # el mercado cae en bloque
        stats = dip_stats(con)
        hist = ""
        if stats:
            hist = (f" En mi histórico, tras caer ≥{-stats['drop']:.0%} en un día, a {stats['days']} "
                    f"días la media fue {_pct(stats['mean'])} y subió {stats['up']:.0%} de las veces "
                    f"({stats['n']} casos), frente a {_pct(cost).lstrip('+')} de costes de ida y vuelta: "
                    + ("ahí sí suele haber rebote, así que vigilo cuándo se gira la señal."
                       if stats["mean"] > 2 * cost and stats["up"] >= 0.6 else "casi una moneda al aire."))
        lines.append(f"Qué haría: esperar. El mercado cae en bloque (intradía mediano {med:+.2f}); "
                     "comprar solo porque ha bajado es intentar coger un cuchillo que cae." + hist
                     + " Prefiero entrar cuando la señal se gire.")
    elif gap is not None and gap <= 0.05:
        lines.append(f"Qué haría: esperar un poco. {code(now_best)} está a {max(gap, 0):.2f} del "
                     "umbral; si sigue mejorando, la compra llegará sola.")
    elif idle_h is None or idle_h >= 72:
        since = "sin estrenar este dinero" if idle_h is None else f"{idle_h / 24:.0f} días sin comprar"
        lines.append(f"Qué haría: no forzar compras, pero revisar el umbral ({buy_th:+.2f}). Llevo "
                     f"{since}: si sigue así en la revisión semanal, toca decidir si bajarlo un poco, "
                     "pasar el dinero a la cartera que sí tenga señales o dejarlo en efectivo, que no "
                     "pierde.")
    else:
        lines.append("Qué haría: esperar. El efectivo no pierde dinero y comprar sin una señal clara es "
                     "apostar; con la caja libre aprovecho la siguiente buena oportunidad.")
    return [ln.replace(" -", " −") for ln in lines]


def _tick_at(con: sqlite3.Connection, sym: str, ts_ms: int, last: bool = False):
    q = ("SELECT price, raw_json, contributions_json FROM ticks WHERE symbol = ? AND action != 'BACKFILL' "
         + ("ORDER BY id DESC LIMIT 1" if last else "AND ts >= ? ORDER BY id LIMIT 1"))
    return con.execute(q, (sym,) if last else (sym, ts_ms)).fetchone()


def loss_review(con: sqlite3.Connection, cfg: dict, sym: str, opened: str | None, avg: float,
                exit_price: float, qty: float, fees: float, why: str, now: datetime,
                names: dict[str, str]) -> list[str]:
    """Pistas de por qué una operación perdió dinero, y una lección. Se guarda en el KV "lessons"."""
    ret = exit_price / avg - 1 if avg > 0 else 0.0
    start = datetime.fromisoformat(opened) if opened else now
    hours = max(0.0, (now - start).total_seconds() / 3600)
    held = f"{hours:.0f} h" if hours < 48 else f"{hours / 24:.0f} días"
    lines = [f"{sym.split('/')[0]}: {_pct(ret)} ({_eur((exit_price - avg) * qty - fees)}) en {held}",
             f"Salida: {why}"]
    ts0 = int(start.timestamp())  # ticks.ts está en segundos
    moves = []
    for s in cfg["general"]["symbols"]:
        if s == sym:
            continue
        a, b = _tick_at(con, s, ts0), _tick_at(con, s, ts0, last=True)
        if a and b and a["price"]:
            moves.append(b["price"] / a["price"] - 1)
    market_fell = False
    if moves:
        med = median(moves)
        market_fell = med < 0 and med <= ret / 2
        lines.append(f"El resto de valores se movió {_pct(med)} en ese tiempo: "
                     + ("cayó casi todo, fue más el mercado que la elección" if market_fell
                        else "fue sobre todo cosa de este valor"))
    entry, last = _tick_at(con, sym, ts0), _tick_at(con, sym, ts0, last=True)
    flipped = []
    if entry and entry["contributions_json"]:
        contrib = json.loads(entry["contributions_json"])
        raw0 = json.loads(entry["raw_json"] or "{}")
        raw1 = json.loads(last["raw_json"] or "{}") if last else {}
        for k, _ in sorted(((k, v) for k, v in contrib.items() if v > 0), key=lambda kv: -kv[1])[:2]:
            r0, r1 = raw0.get(k), raw1.get(k)
            if isinstance(r0, int | float) and isinstance(r1, int | float):
                turn = r1 < 0 < r0
                flipped += [k] if turn else []
                lines.append(f"Compré sobre todo por {names.get(k, k)} ({r0:+.2f}); ahora {r1:+.2f}"
                             + (" → se giró" if turn else ""))
    invested = qty * avg
    if fees > 0 and invested > 0:
        lines.append(f"Comisiones {fees:.2f} € ({fees / invested:.1%} de lo invertido)")
    loss = abs(min(0.0, (exit_price - avg) * qty - fees))
    if loss and fees / loss > 0.3:
        lesson = ("la operación era demasiado pequeña para sus comisiones: mejor menos operaciones y "
                  "más grandes")
    elif market_fell:
        lesson = "compré con el mercado girándose a la baja; debo exigir más señal cuando el conjunto cae"
    elif "news" in flipped:
        lesson = "me fié de noticias que cambiaron enseguida; una sola noticia no basta"
    elif hours < 8 and "stop" in why:
        lesson = "el stop saltó el mismo día: probablemente entré tarde, en un pico"
    elif flipped:
        why_in = ", ".join(names.get(k, k) for k in flipped)
        lesson = f"lo que justificaba la compra ({why_in}) se dio la vuelta"
    else:
        lesson = "sin una causa clara; cuenta en la estadística con la que learn ajusta los pesos"
    lines.append(f"Lección: {lesson}")
    kv = db.KV(con)
    try:
        saved = json.loads(kv.get("lessons", "[]"))
    except ValueError:
        saved = []
    kv.set("lessons", json.dumps((saved + [{"ts": now.isoformat(), "symbol": sym, "ret": ret,
                                            "lines": lines}])[-LESSONS:], ensure_ascii=False))
    return [ln.replace(" -", " −") for ln in lines]
