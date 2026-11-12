"""Reoptimización mensual con aprobación (idea: FreqAI, sin su complejidad).

Solo propone parámetros si (a) mejoran fuera de muestra en walk-forward, (b) pasan el Monte Carlo
y (c) ningún valor cambia más de adapt.max_weight_step. En live nunca aplica sin /aprobar.
python -m newsbot.research.reoptimize
"""
import argparse
import json
from contextlib import closing
from datetime import datetime
from pathlib import Path

from newsbot import config as config_mod
from newsbot import portfolio
from newsbot.features import market
from newsbot.reporting import telegram
from newsbot.research.backtest import WARMUP, fetch_history, precompute
from newsbot.research.montecarlo import overfit_test
from newsbot.research.walkforward import grid, identity, walk_forward
from newsbot.storage import db


def propose(candles: list[list[float]], cfg: dict) -> dict:
    a = cfg["adapt"]
    candles = candles[-(a["lookback_days"] + WARMUP):]
    wf = walk_forward(candles, cfg, grid(cfg), a["train_days"], a["test_days"],
                      precompute(candles, cfg))
    best = wf["last_best"]
    out = {"params": best, "oos_candidate": wf["oos_candidate"], "oos_current": wf["oos_current"],
           "mc": None, "reasons": []}
    if best is None:
        out["reasons"].append("historial insuficiente para walk-forward")
    elif best == identity(cfg):
        out["reasons"].append("los parámetros actuales siguen siendo los mejores")
    else:
        if wf["oos_candidate"] <= wf["oos_current"]:
            out["reasons"].append("no mejora fuera de muestra")
        step = max(abs(v - cfg[sec][k]) for sec, vals in best.items() for k, v in vals.items())
        if step > a["max_weight_step"] + 1e-9:
            out["reasons"].append(f"cambio de {step:.2f} > max_weight_step")
    if not out["reasons"]:
        out["mc"] = overfit_test(candles, config_mod.apply(cfg, best), a["mc_runs"])
        if out["mc"]["suspicious"]:
            out["reasons"].append("sospechoso de overfit (Monte Carlo)")
    out["accepted"] = not out["reasons"]
    return out


# Límites duros: por mucho que mejore el backtest, nunca se compra con señal floja ni se vende tarde.
BOUNDS = {"buy_threshold": (0.15, 0.60), "sell_threshold": (-0.40, -0.05)}


def auto_apply(cfg: dict) -> bool:
    a, mode = cfg["adapt"], cfg["general"]["mode"]
    return bool(a.get("auto_apply_live") if mode == "live" else a["auto_apply_in_paper"])


def run(con, cfg: dict, ex, now: datetime) -> str:
    a, g = cfg["adapt"], cfg["general"]
    # los umbrales son comunes: se calibran sobre la primera moneda (BTC, la de más historia)
    p = propose(fetch_history(ex, g["symbols"][0], a["lookback_days"] + WARMUP), cfg)
    for k, v in ((p["params"] or {}).get("strategy") or {}).items():
        lo, hi = BOUNDS.get(k, (-1.0, 1.0))
        if not lo <= v <= hi:
            p["accepted"] = False
            p["reasons"].append(f"{k}={v} fuera de los límites [{lo}, {hi}]")
    status = "rejected" if not p["accepted"] else "applied" if auto_apply(cfg) else "proposed"
    note = {k: p[k] for k in ("reasons", "oos_candidate", "oos_current", "mc")} | {
        "origin": "reoptimize", "based_on": db.active_params(con, g["mode"])[0],
        "auto": status == "applied", "mode": g["mode"]}
    with con:
        cur = con.execute("INSERT INTO params_history(created, params_json, status, note) "
                          "VALUES (?,?,?,?)", (now.isoformat(), json.dumps(p["params"]), status,
                                               json.dumps(note, ensure_ascii=False)))
    db.KV(con).set("reopt:last", now.date().isoformat())
    pid = cur.lastrowid
    msg = (f"Reoptimización #{pid}: {status}. Parámetros {p['params']}. Fuera de muestra "
           f"×{p['oos_candidate']:.3f} vs actuales ×{p['oos_current']:.3f}. "
           + ("; ".join(p["reasons"]) if p["reasons"] else "")
           + (f" Envía {portfolio.cmd('aprobar', pid)} para aplicarla o "
              f"{portfolio.cmd('rechazar', pid)}." if status == "proposed" else
              f" Aplicada sola desde el próximo tick; {portfolio.cmd('deshacer', pid)} para "
              "revertirla." if status == "applied" else ""))
    telegram.send(msg)
    return msg


def due(con, cfg: dict, now: datetime) -> bool:
    last = db.KV(con).get("reopt:last")
    return cfg["adapt"]["enabled"] and (
        last is None or (now.date() - datetime.fromisoformat(last).date()).days
        >= cfg["adapt"]["cadence_days"])


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--config", default="config.toml")
    args = p.parse_args()
    cfg = config_mod.load(args.config)
    data = Path(cfg["general"]["data_dir"])
    data.mkdir(parents=True, exist_ok=True)
    with closing(db.connect(data / "newsbot.db")) as con, portfolio.active(cfg):
        cfg = config_mod.apply(cfg, db.active_params(con, cfg["general"]["mode"])[1])
        ex = market.make_exchange(cfg["general"]["exchange"], markets=cfg.get("market"))
        print(run(con, cfg, ex, datetime.now()))


if __name__ == "__main__":
    main()
