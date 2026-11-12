"""Exportación de ganancias y pérdidas patrimoniales por FIFO, en euros, una fila por lote casado.

python -m newsbot.reporting.tax --year 2026 [--mode live|paper]
Solo exporta datos para la declaración (base del ahorro del IRPF). No es asesoramiento fiscal.
Las comisiones suman al valor de adquisición y restan del valor de transmisión.
"""
import argparse
import csv
import sqlite3
from collections import deque
from contextlib import closing
from pathlib import Path

from newsbot import config as config_mod
from newsbot.storage import db

FIELDS = ["activo", "fecha_transmision", "fecha_adquisicion", "cantidad", "valor_adquisicion_eur",
          "valor_transmision_eur", "ganancia_perdida_eur"]


def fifo(orders: list[dict]) -> list[dict]:
    """orders: dicts con day, symbol, side, qty, eur (bruto) y fee (EUR), en orden cronológico.
    FIFO separado por activo: una venta de ETH solo casa con compras de ETH."""
    lots: dict[str, deque[list]] = {}  # activo -> [fecha, cantidad restante, coste unitario]
    rows = []
    for o in orders:
        sym = o.get("symbol") or "BTC/EUR"
        queue = lots.setdefault(sym, deque())
        if o["side"] == "buy":
            queue.append([o["day"], o["qty"], (o["eur"] + o["fee"]) / o["qty"]])
            continue
        remaining, unit = o["qty"], (o["eur"] - o["fee"]) / o["qty"]
        while remaining > 1e-12:
            if not queue:
                raise ValueError(f"Venta de {sym} del {o['day']} sin compras previas registradas")
            lot = queue[0]
            q = min(remaining, lot[1])
            rows.append({"activo": sym.split("/")[0], "fecha_transmision": o["day"],
                         "fecha_adquisicion": lot[0],
                         "cantidad": round(q, 8), "valor_adquisicion_eur": round(q * lot[2], 2),
                         "valor_transmision_eur": round(q * unit, 2),
                         "ganancia_perdida_eur": round(q * (unit - lot[2]), 2)})
            lot[1] -= q
            remaining -= q
            if lot[1] <= 1e-12:
                queue.popleft()
    return rows


def export(con: sqlite3.Connection, year: int, mode: str, path: Path) -> list[dict]:
    orders = [dict(r) for r in con.execute(
        "SELECT day, symbol, side, qty, eur, fee FROM orders WHERE mode = ? AND status = 'filled' "
        "ORDER BY created, id", (mode,))]
    rows = [r for r in fifo(orders) if r["fecha_transmision"].startswith(str(year))]
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)
    return rows


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--config", default="config.toml")
    p.add_argument("--year", type=int, required=True)
    p.add_argument("--mode", default="live", choices=["live", "paper"])
    args = p.parse_args()
    cfg = config_mod.load(args.config)
    data = Path(cfg["general"]["data_dir"])
    out = data / f"fiscal_{args.year}_{args.mode}.csv"
    with closing(db.connect(data / "newsbot.db")) as con:
        rows = export(con, args.year, args.mode, out)
    total = sum(r["ganancia_perdida_eur"] for r in rows)
    print(f"{len(rows)} filas -> {out} | ganancia/pérdida total {total:+.2f} € "
          "(no es asesoramiento fiscal)")


if __name__ == "__main__":
    main()
