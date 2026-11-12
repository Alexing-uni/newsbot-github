"""Historial de eventos: cómo reaccionó cada valor a eventos pasados del mismo tipo (estudio de eventos).

Con velas diarias de Yahoo (10 años) mide, para cada evento con fecha conocida, el retorno del día
siguiente (t+1, cuando ya se conocen los resultados en Europa) y de t+5, y su diferencia con el S&P 500
(el efecto propio del valor). Se guarda en <data_dir>/event_history.json y events.refresh se lo pasa a
Claude para que calibre dirección y confianza con datos, no solo con intuición.
Ojo: pocas muestras (5 elecciones). Es una pista, no una regla.
python -m newsbot.stocks.history --config config_bolsa.toml
"""
import argparse
import json
import logging
import time
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from newsbot import config as config_mod
from newsbot.features import yahoo

log = logging.getLogger(__name__)
# Eventos con fecha cierta. Ampliable con más fechas pasadas del mismo tipo (Yahoo da unos 10 años).
EVENTS = {
    "elecciones EE. UU.": {
        "presidenciales 2016": date(2016, 11, 8), "medio mandato 2018": date(2018, 11, 6),
        "presidenciales 2020": date(2020, 11, 3), "medio mandato 2022": date(2022, 11, 8),
        "presidenciales 2024": date(2024, 11, 5)},
}
BENCH = "SP500"
FILE = "event_history.json"


def _closes(ex, symbol: str, tz: ZoneInfo) -> dict[date, float]:
    """Cierres diarios por fecha local (desde 2016: cubre todos los EVENTS)."""
    since = int(datetime(2016, 1, 1, tzinfo=tz).timestamp() * 1000)
    return {datetime.fromtimestamp(c[0] / 1000, tz).date(): c[4]
            for c in ex.fetch_ohlcv(symbol, "1d", since=since)}


def _window(series: dict[date, float], d0: date) -> tuple[float, float] | None:
    days = sorted(series)
    i = next((k for k, d in enumerate(days) if d >= d0), None)
    if i is None or days[i] != d0 or i + 5 >= len(days):
        return None
    c0, c1, c5 = series[days[i]], series[days[i + 1]], series[days[i + 5]]
    if c1 == c0:  # línea sin negociación ese día (precio repetido): no informa
        return None
    return c1 / c0 - 1, c5 / c0 - 1


def study(cfg: dict, fetch=None) -> dict:
    """{tipo: {código: {evento: [t+1, t+5, t+1 vs S&P]}, ...}}"""
    ex = yahoo.YahooExchange(cfg["market"], fetch=fetch) if fetch else yahoo.YahooExchange(cfg["market"])
    series, tz = {}, ZoneInfo(cfg["general"]["timezone"])
    for code in cfg["market"]:
        try:
            series[code] = _closes(ex, f"{code}/EUR", tz)
        except Exception as exc:
            log.warning("Historial de %s no disponible (%s)", code, type(exc).__name__)
    out = {}
    for kind, events in EVENTS.items():
        bench = series.get(BENCH, {})
        out[kind] = {}
        for code, s in series.items():
            rows = {}
            for name, d0 in events.items():
                w, b = _window(s, d0), _window(bench, d0)
                if w:
                    rows[name] = [round(w[0], 4), round(w[1], 4), round(w[0] - b[0], 4) if b else None]
            out[kind][code] = rows
    return out


def save(cfg: dict, data: dict) -> Path:
    path = Path(cfg["general"]["data_dir"]) / FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"updated": date.today().isoformat(), "events": data}, ensure_ascii=False,
                               indent=1), encoding="utf-8")
    return path


def load(cfg: dict) -> dict:
    if not (data_dir := cfg.get("general", {}).get("data_dir")):
        return {}
    path = Path(data_dir) / FILE
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("events", {})
    except (OSError, ValueError):
        return {}


def prompt_lines(cfg: dict) -> str:
    """Resumen para Claude: media de t+1 frente al S&P por tipo de evento y valor (solo con ≥ 3 casos)."""
    lines = []
    for kind, codes in load(cfg).items():
        cells = []
        for code, rows in codes.items():
            ab = [r[2] for r in rows.values() if r[2] is not None]
            if code != BENCH and len(ab) >= 3:
                ups = sum(a > 0 for a in ab)
                cells.append(f"{code} {sum(ab) / len(ab):+.1%} ({ups}/{len(ab)} por encima)")
        if cells:
            lines.append(f"- {kind}, día siguiente frente al S&P 500: " + ", ".join(cells))
    return "\n".join(lines)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--config", default="config_bolsa.toml")
    args = p.parse_args()
    cfg = config_mod.load(args.config)
    t0 = time.monotonic()
    path = save(cfg, study(cfg))
    print(f"Historial de eventos guardado en {path} ({time.monotonic() - t0:.0f} s)")
    print(prompt_lines(cfg) or "(sin datos suficientes)")


if __name__ == "__main__":
    main()
