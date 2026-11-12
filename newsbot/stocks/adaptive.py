"""Umbral de compra que se ajusta solo con los resultados ([adaptive] en el config de la cartera).

Tras cada operación cerrada (venta, stop o venta fuera del bot) se apunta su rentabilidad neta en
el KV "trades:closed". Con al menos `min_trades` cerradas, se miran las últimas `window`:
- pierde (menos del 40 % ganadoras o media negativa): sube el umbral `step` → más exigente;
- gana (60 % o más ganadoras y media positiva): lo baja `step` → más atrevido;
siempre dentro de [min_offset, max_offset] sobre el umbral del config. El ajuste ("adaptive:offset")
se suma a buy_threshold y buy_threshold_bear en cada ciclo y se avisa por Telegram al cambiar.
"""
import json
from datetime import datetime

from newsbot.storage import db

KEEP = 100


def apply(cfg: dict, kv) -> dict:
    """cfg con el ajuste aplicado a los umbrales de compra (y una nota para /pensamiento)."""
    a = cfg.get("adaptive") or {}
    off = float(kv.get("adaptive:offset") or 0.0) if a.get("enabled") else 0.0
    if not off:
        return cfg
    st = cfg["strategy"]
    return cfg | {"strategy": st | {"buy_threshold": st["buy_threshold"] + off,
                                    "buy_threshold_bear": st["buy_threshold_bear"] + off},
                  "_adaptive": off}


def closed(kv) -> list[dict]:
    try:
        return json.loads(kv.get("trades:closed") or "[]")
    except ValueError:
        return []


def record(con, cfg: dict, symbol: str, ret: float, now: datetime) -> str | None:
    """Apunta una operación cerrada y recalcula el ajuste. Devuelve el aviso si cambió."""
    kv = db.KV(con)
    trades = (closed(kv) + [{"ts": now.isoformat(), "symbol": symbol, "ret": ret}])[-KEEP:]
    kv.set("trades:closed", json.dumps(trades))
    a = cfg.get("adaptive") or {}
    if not a.get("enabled") or len(trades) < a.get("min_trades", 4):
        return None
    last = [t["ret"] for t in trades[-a.get("window", 6):]]
    wins, avg = sum(r > 0 for r in last) / len(last), sum(last) / len(last)
    old = float(kv.get("adaptive:offset") or 0.0)
    step = a.get("step", 0.03)
    if wins < 0.4 or avg < 0:
        new, why = old + step, "más exigente"
    elif wins >= 0.6 and avg > 0:
        new, why = old - step, "más atrevido"
    else:
        return None
    new = round(max(a.get("min_offset", -0.08), min(a.get("max_offset", 0.15), new)), 4)
    if new == old:
        return None
    kv.set("adaptive:offset", repr(new))
    base = cfg["strategy"]["buy_threshold"] - cfg.get("_adaptive", 0.0)  # el del config
    return (f"🎚️ Ajusto el umbral de compra: {base + old:+.2f} → {base + new:+.2f} ({why}). Últimas "
            f"{len(last)} operaciones: {wins:.0%} ganadoras, media {avg:+.2%}.").replace("-", "−")
