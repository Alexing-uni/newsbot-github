"""Aprendizaje honesto a partir de los ticks registrados (tabla ticks, uno por moneda cada 5 min).

Etiqueta cada tick con lo que hizo el precio después (retorno logarítmico a 1h/4h/24h; 5 min es
ruido), mide el poder predictivo de cada componente (IC de Spearman con IC de Fisher, acierto con
intervalo de Wilson) y, como mucho cada `cadence_days`, propone pesos nuevos pequeños y encogidos
hacia los actuales. En live nunca aplica nada: hace falta /aprobar.

Cautelas: solo muestras no solapadas (las etiquetas solapadas inflan la significación) y las monedas
están correlacionadas, así que n_eff = n · min(monedas, k) / monedas
(k = learn.independent_bets: 2 en cripto, más en bolsa).
"""
import html
import json
import logging
import math
import sqlite3
from datetime import datetime, timedelta

from newsbot import config as config_mod
from newsbot import portfolio
from newsbot.features import session
from newsbot.storage import db

log = logging.getLogger(__name__)
HORIZONS = {"1h": 3600, "4h": 14400, "24h": 86400}
# solo los calcula la bolsa (newsbot/stocks)
STOCKS_ONLY = ("events", "breakout", "macd", "high52", "kronos", "dip", "finbert")
COMPONENTS = ("news", "trend", "intraday", "fng", "votes", "rsi", "bollinger", "momentum", "volume",
              *STOCKS_ONLY)
DEFAULTS = {"enabled": True, "horizon": "4h", "lookback_days": 60, "min_n_eff": 400,
            "max_step": 0.05, "shrinkage_k": 400, "cadence_days": 7, "auto_apply_live": False,
            "independent_bets": 2,
            "cost": 0.006, "min_abs": 0.05}
Z95 = 1.959964


def settings(cfg: dict) -> dict:
    return DEFAULTS | cfg.get("learn", {})


# --- estadística (solo math) --------------------------------------------------------------------
def _ranks(xs: list[float]) -> list[float]:
    """Rangos con empates promediados (1..n)."""
    order = sorted(range(len(xs)), key=xs.__getitem__)
    out = [0.0] * len(xs)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        for k in range(i, j + 1):
            out[order[k]] = (i + j) / 2 + 1
        i = j + 1
    return out


def spearman(xs: list[float], ys: list[float]) -> float | None:
    if len(xs) < 10:
        return None
    rx, ry = _ranks(xs), _ranks(ys)
    mx, my = sum(rx) / len(rx), sum(ry) / len(ry)
    sxy = sum((a - mx) * (b - my) for a, b in zip(rx, ry, strict=True))
    sxx, syy = sum((a - mx) ** 2 for a in rx), sum((b - my) ** 2 for b in ry)
    return sxy / math.sqrt(sxx * syy) if sxx > 0 and syy > 0 else None


def fisher_ci(r: float | None, n_eff: float) -> tuple[float, float] | None:
    """IC 95 % del Spearman: atanh(r) ± 1.96 · 1.06/√(n_eff−3)."""
    if r is None or n_eff <= 3:
        return None
    z, se = math.atanh(max(-0.999999, min(0.999999, r))), 1.06 / math.sqrt(n_eff - 3)
    return math.tanh(z - Z95 * se), math.tanh(z + Z95 * se)


def wilson(hits: float, n: float) -> tuple[float, float] | None:
    if n <= 0:
        return None
    p, z2 = hits / n, Z95 ** 2
    mid, half = p + z2 / (2 * n), Z95 * math.sqrt(p * (1 - p) / n + z2 / (4 * n * n))
    return max(0.0, (mid - half) / (1 + z2 / n)), min(1.0, (mid + half) / (1 + z2 / n))


def _stats(pairs: list[tuple[float, float]], factor: float, min_abs: float) -> dict:
    """pairs = (x, retorno futuro). `factor` pasa de muestras a muestras efectivas."""
    n = len(pairs)
    ic = spearman([x for x, _ in pairs], [r for _, r in pairs]) if n else None
    strong = [(x, r) for x, r in pairs if abs(x) >= min_abs and r != 0]
    hits = sum((x > 0) == (r > 0) for x, r in strong)
    up = [r for x, r in pairs if x >= min_abs]
    down = [r for x, r in pairs if x <= -min_abs]
    return {"n": n, "ic": ic, "ic_ci": fisher_ci(ic, n * factor),
            "hit_n": len(strong), "hit": hits / len(strong) if len(strong) >= 10 else None,
            "hit_ci": wilson(hits * factor, len(strong) * factor) if len(strong) >= 10 else None,
            "ret_up": sum(up) / len(up) if up else None, "n_up": len(up),
            "ret_down": sum(down) / len(down) if down else None, "n_down": len(down)}


# --- evaluación ---------------------------------------------------------------------------------
def _samples(rows: list, h: int, now_ts: int) -> list[tuple[sqlite3.Row, float]]:
    """Muestras no solapadas de una moneda (filas en orden de ts) con su retorno log a `h` s."""
    tol, out, last, j = max(900, h * 0.1), [], None, 0
    for row in rows:
        t = row["ts"]
        if last is not None and t < last + h:
            continue
        while j < len(rows) and rows[j]["ts"] < t + h:
            j += 1
        if j >= len(rows) or rows[j]["ts"] > now_ts:
            break  # aún no se conoce el futuro de este tick ni de los siguientes
        if rows[j]["ts"] > t + h + tol:
            continue  # hueco en los datos: el siguiente tick no es "a h vista"
        p0, p1 = row["price"], rows[j]["price"]
        if not (p0 and p1 and p0 > 0 and p1 > 0):
            continue
        out.append((row, math.log(p1 / p0)))
        last = t
    return out


def evaluate(con: sqlite3.Connection, cfg: dict, now: datetime, horizon: str | None = None) -> dict:
    s = settings(cfg)
    horizon = horizon or s["horizon"]
    if horizon not in HORIZONS:
        raise ValueError(f"Horizonte desconocido: {horizon} (usa {', '.join(HORIZONS)})")
    h, now_ts = HORIZONS[horizon], int(now.timestamp())
    since = now - timedelta(days=s["lookback_days"])
    by_symbol: dict[str, list] = {}
    for row in con.execute("SELECT symbol, ts, price, signal, raw_json FROM ticks WHERE ts >= ? "
                           "AND ts <= ? ORDER BY symbol, ts", (int(since.timestamp()), now_ts)):
        by_symbol.setdefault(row["symbol"], []).append(row)
    pairs: dict[str, list[tuple[float, float]]] = {c: [] for c in (*COMPONENTS, "signal")}
    n = n_symbols = 0
    for rows in by_symbol.values():
        samples = _samples(rows, h, now_ts)
        n, n_symbols = n + len(samples), n_symbols + bool(samples)
        for row, ret in samples:  # solo se parsea el JSON de las muestras
            raw = json.loads(row["raw_json"] or "{}")
            for c in COMPONENTS:
                if isinstance(raw.get(c), int | float):
                    pairs[c].append((float(raw[c]), ret))
            if row["signal"] is not None:
                pairs["signal"].append((row["signal"], ret))
    factor = min(n_symbols, s["independent_bets"]) / n_symbols if n_symbols else 0.0
    st = {c: _stats(v, factor, s["min_abs"]) for c, v in pairs.items()}
    return {"horizon": horizon, "horizon_s": h, "n": n, "n_eff": n * factor, "n_symbols": n_symbols,
            "since": since.isoformat(), "components": {c: st[c] for c in COMPONENTS},
            "fused": st["signal"]}


# --- informe (/aprendizaje) ---------------------------------------------------------------------
def _f(v: float | None, fmt: str) -> str:
    return "—" if v is None else format(v, fmt)


def _pct(v: float | None) -> str:
    return "—" if v is None else f"{v * 100:+.2f} %"


def pending(con: sqlite3.Connection) -> int | None:
    """Id de la última propuesta del aprendizaje aún sin aprobar."""
    row = con.execute("SELECT id FROM params_history WHERE status = 'proposed' AND note LIKE ? "
                      "ORDER BY id DESC LIMIT 1", ('%"origin": "learn"%',)).fetchone()
    return row["id"] if row else None


def progress(con: sqlite3.Connection, cfg: dict, now: datetime, ev: dict | None = None) -> dict:
    """Cuánto lleva aprendido: muestras efectivas del horizonte principal frente a min_n_eff,
    días que faltan al ritmo actual y el volumen de datos guardados (ticks y titulares)."""
    s = settings(cfg)
    ev = ev or evaluate(con, cfg, now, s["horizon"])
    n_sym = ev["n_symbols"] or len(cfg["general"]["symbols"])
    # n_eff nuevo por día; en bolsa solo hay muestras con el mercado abierto
    rate = 86400 / HORIZONS[s["horizon"]] * min(n_sym, s["independent_bets"]) * session.fraction(cfg)
    ticks, first, past = con.execute("SELECT count(*), min(ts), sum(action = 'BACKFILL') FROM ticks"
                                     ).fetchone()
    heads, forums = con.execute("SELECT count(DISTINCT title), count(DISTINCT CASE WHEN source "
                                "LIKE 'reddit%' THEN title END) FROM headlines").fetchone()
    pct = min(1.0, ev["n_eff"] / s["min_n_eff"])
    return {"n_eff": ev["n_eff"], "min_n_eff": s["min_n_eff"], "pct": pct,
            "days_left": 0 if pct >= 1 else math.ceil((s["min_n_eff"] - ev["n_eff"]) / rate),
            "horizon": s["horizon"], "ticks": ticks, "backfill": past or 0, "first_tick": first,
            "headlines": heads,
            "forums": forums, "significant": [c for c, st in ev["components"].items()
                                              if st["ic_ci"] and (st["ic_ci"][0] > 0 or st["ic_ci"][1] < 0)]}


def bar(pct: float, width: int = 10) -> str:
    full = round(max(0.0, min(1.0, pct)) * width)
    return "▓" * full + "░" * (width - full)


def progress_lines(p: dict) -> list[str]:
    """Panel de memoria en HTML (para /estado y /aprendizaje)."""
    if p["pct"] < 1:
        phase = (f"recogiendo datos · faltan ≈{p['days_left']} días para poder juzgar qué señales "
                 "funcionan")
    elif p["significant"]:
        phase = "datos suficientes · señales con poder predictivo: " + ", ".join(p["significant"])
    else:
        phase = "datos suficientes · ninguna señal predice mejor que el azar todavía"
    return [f"🧠 Memoria {bar(p['pct'])} {p['pct']:.0%} "
            f"({p['n_eff']:.0f}/{p['min_n_eff']} muestras a {p['horizon']})",
            f"   {html.escape(phase)}",
            f"📚 {p['headlines']} titulares ({p['forums']} de foros) · {p['ticks']} lecturas de precio"
            + (f" ({p['backfill']} del histórico: solo precio, sin noticias)" if p.get("backfill") else "")]


def _shown(ev: dict, w: dict) -> dict:
    """Componentes del informe y de las notas: los de bolsa (STOCKS_ONLY) solo con datos (n > 0) o
    con peso, así los de cripto no ganan filas vacías."""
    return {c: st for c, st in ev["components"].items()
            if c not in STOCKS_ONLY or st["n"] > 0 or w.get(c, 0) > 0}


def report(con: sqlite3.Connection, cfg: dict, now: datetime, horizon: str | None = None) -> str:
    s = settings(cfg)
    try:
        ev = evaluate(con, cfg, now, horizon)
    except ValueError as exc:
        return html.escape(str(exc))
    w, h = cfg["weights"], ev["horizon"]
    try:
        mem = progress_lines(progress(con, cfg, now, ev if h == s["horizon"] else None))
    except Exception:  # el panel no debe romper el informe
        mem = []
    lines = [f"<b>Aprendizaje</b> · horizonte {h} · últimos {s['lookback_days']} d", *mem,
             f"muestras n={ev['n']}, efectivas ≈{ev['n_eff']:.0f} (no solapadas, "
             f"{ev['n_symbols']} monedas)"]
    comps = sorted(_shown(ev, w).items(),
                   key=lambda kv: (kv[1]["ic"] is None, -(kv[1]["ic"] or 0)))
    rows = [f"{'comp':<17}{'IC [95 %]':<21}{'acierto':>8}{'peso':>6}"]
    for c, st in comps:
        shadow = c in config_mod.SHADOW_WEIGHTS and (c not in STOCKS_ONLY or not w.get(c))
        name, ci = c + ("·sombra" if shadow else ""), st["ic_ci"]
        ic = f"{st['ic']:+.2f} [{ci[0]:+.2f},{ci[1]:+.2f}]" if ci else _f(st["ic"], "+.2f")
        rows.append(f"{name:<17}{ic:<21}{_f(st['hit'], '.0%'):>8}{w.get(c, 0):>6.2f}")
    lines.append("<pre>" + html.escape("\n".join(rows)) + "</pre>")
    fu, m = ev["fused"], s["min_abs"]
    lines.append(f"Señal fusionada: IC {_f(fu['ic'], '+.2f')}; retorno medio a {h} con señal "
                 f"≥{m:+.2f}: {_pct(fu['ret_up'])} (n={fu['n_up']}) vs ≤{-m:+.2f}: "
                 f"{_pct(fu['ret_down'])} (n={fu['n_down']}); coste ida y vuelta ≈{s['cost']:.2%}")
    pos = [c for c, st in comps if st["ic_ci"] and st["ic_ci"][0] > 0]
    neg = [c for c, st in comps if st["ic_ci"] and st["ic_ci"][1] < 0]
    if pos:
        lines.append(f"Predicen (IC &gt; 0 con 95 %): {', '.join(pos)}")
    if neg:
        lines.append(f"Van al revés (IC &lt; 0 con 95 %): {', '.join(neg)}")
    if not pos and not neg:
        lines.append("Ningún componente es significativo todavía.")
    if ev["n_eff"] < s["min_n_eff"]:
        lines.append(f"Aún no hay datos suficientes para cambiar pesos (n_eff {ev['n_eff']:.0f}/"
                     f"{s['min_n_eff']}). Solo se mide.")
        n_sym = ev["n_symbols"] or len(cfg["general"]["symbols"])
        # n_eff nuevo por día
        rate = 86400 / ev["horizon_s"] * min(n_sym, s["independent_bets"]) * session.fraction(cfg)
        if rate * s["lookback_days"] < s["min_n_eff"]:
            lines.append(f"Con horizonte {h} y {s['lookback_days']} d de ventana no se llega nunca; "
                         f"las propuestas usan {s['horizon']}.")
        else:
            lines.append(f"Faltan ≈{math.ceil((s['min_n_eff'] - ev['n_eff']) / rate)} días al "
                         "ritmo actual.")
    else:
        lines.append(f"Datos suficientes: cada {s['cadence_days']} d se puede proponer mover cada "
                     f"peso como mucho ±{s['max_step']:.2f}"
                     + (f" (se aplica solo; {portfolio.cmd('deshacer')} para revertir)."
                        if s.get("auto_apply_live") else f" (con {portfolio.cmd('aprobar')} en live)."))
    if (pid := pending(con)) is not None:
        lines.append(f"Propuesta pendiente #{pid}: {portfolio.cmd('aprobar', pid)} o "
                     f"{portfolio.cmd('rechazar', pid)}.")
    return "\n".join(lines)[:3500]


# --- propuesta semanal --------------------------------------------------------------------------
def _normalize(raw: dict) -> dict | None:
    total = sum(raw.values())
    if total <= 0:
        return None
    out = {k: round(v / total, 4) for k, v in raw.items()}
    top = max(out, key=out.get)
    out[top] = round(out[top] + 1 - sum(out.values()), 4)  # el residuo del redondeo, al mayor
    return out if out[top] >= 0 else None


def new_weights(current: dict, ev: dict, s: dict) -> dict | None:
    """Vector COMPLETO de pesos (núcleo y sombra, ceros incluidos: así un cambio posterior en
    config.toml no altera la suma), encogido hacia los actuales, ±max_step, ≥0 y que suma 1.
    Si al renormalizar algún peso se pasa del paso, se reduce el cambio en vez de descartarlo."""
    step, shrink = s["max_step"], ev["n_eff"] / (ev["n_eff"] + s["shrinkage_k"])
    keys = sorted(config_mod.CORE_WEIGHTS | config_mod.SHADOW_WEIGHTS)
    delta = {}
    for k in keys:
        w, ci = current.get(k, 0.0), ev["components"].get(k, {}).get("ic_ci")
        target = w + step if ci and ci[0] > 0 else 0.0 if ci and ci[1] < 0 else w
        delta[k] = max(-step, min(step, shrink * (target - w)))
    alpha = 1.0
    while alpha >= 1 / 64:
        out = _normalize({k: max(0.0, current.get(k, 0.0) + alpha * delta[k]) for k in keys})
        if out and all(abs(out[k] - current.get(k, 0.0)) <= step + 1e-9 for k in keys):
            return out
        alpha /= 2
    return None


def maybe_propose(con: sqlite3.Connection, cfg: dict, now: datetime, send=None) -> str | None:
    s, kv = settings(cfg), db.KV(con)
    if not s["enabled"]:
        return None
    last = kv.get("learn:last")
    if last and (now.date() - datetime.fromisoformat(last).date()).days < s["cadence_days"]:
        return None  # barato: no se evalúa nada hasta que toque
    kv.set("learn:last", now.date().isoformat())
    active_id, active = db.active_params(con, cfg["general"]["mode"])
    try:
        eff = config_mod.apply(cfg, active)  # idempotente si `cfg` ya es la efectiva
    except ValueError:
        eff = cfg
    ev = evaluate(con, eff, now)
    if ev["n_eff"] < s["min_n_eff"]:
        log.info("Aprendizaje: n_eff %.0f < %s, solo se mide", ev["n_eff"], s["min_n_eff"])
        return None
    cur = eff["weights"]
    if (new := new_weights(cur, ev, s)) is None:
        log.info("Aprendizaje: propuesta descartada (renormalizar rompe max_step)")
        return None
    changed = {k: (cur.get(k, 0.0), v) for k, v in new.items() if abs(v - cur.get(k, 0.0)) >= 0.005}
    if not changed:
        return None
    override = {"weights": new}  # solo su sección: active_params acumula las aplicadas
    try:
        config_mod.apply(cfg, active | override)
    except ValueError as exc:
        log.warning("Aprendizaje: override inválido (%s)", exc)
        return None
    auto = (s.get("auto_apply_live") if cfg["general"]["mode"] == "live"
            else cfg.get("adapt", {}).get("auto_apply_in_paper"))
    status = "applied" if auto else "proposed"
    note = {"origin": "learn", "horizon": ev["horizon"], "n_eff": round(ev["n_eff"], 1),
            "based_on": active_id, "auto": status == "applied", "mode": cfg["general"]["mode"],
            "ic": {c: st["ic"] and round(st["ic"], 4) for c, st in _shown(ev, cur).items()},
            "ic_ci": {c: st["ic_ci"] and [round(x, 4) for x in st["ic_ci"]]
                      for c, st in _shown(ev, cur).items()}}
    with con:
        pid = con.execute("INSERT INTO params_history(created, params_json, status, note) "
                          "VALUES (?,?,?,?)", (now.isoformat(), json.dumps(override), status,
                                               json.dumps(note, ensure_ascii=False))).lastrowid
    diff = ", ".join(f"{k} {a:.2f}→{b:.2f}".replace(" 0.00→", " 0→") for k, (a, b) in changed.items())
    head = f"(aprendizaje, {ev['horizon']}, n_eff {ev['n_eff']:.0f}): {diff}."
    msg = (f"🧠 Pesos aplicados solos #{pid} {head} Rigen desde el próximo tick; "
           f"{portfolio.cmd('deshacer', pid)} para revertirlo." if status == "applied" else
           f"🧠 Propuesta de pesos #{pid} {head} Envía {portfolio.cmd('aprobar', pid)} para "
           f"aplicarla o {portfolio.cmd('rechazar', pid)}.")
    if send is None:
        from newsbot.reporting import telegram  # diferido: telegram importa este módulo
        w = "" if portfolio.word(cfg) == portfolio.MAIN else portfolio.word(cfg)
        kb = None if status == "applied" else {"inline_keyboard": [[  # aceptar con un toque
            {"text": "✅ Aplicar", "callback_data": f"m:{w}:{pid}:y"},
            {"text": "❌ No", "callback_data": f"m:{w}:{pid}:n"}]]}
        send = lambda m: telegram.send(m, markup=kb)  # noqa: E731
    send(msg)
    return msg
