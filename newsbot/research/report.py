"""Informe Markdown: estrategia frente a buy & hold y DCA, walk-forward y Monte Carlo.

python -m newsbot.research.report --days 1095
"""
import argparse
from contextlib import closing
from datetime import date
from pathlib import Path

from newsbot import config as config_mod
from newsbot.features import market
from newsbot.research.backtest import compare, fetch_history, precompute
from newsbot.research.montecarlo import overfit_test
from newsbot.research.walkforward import grid, walk_forward
from newsbot.storage import db


def render(cmp: dict, wf: dict, mc: dict, news_days: int, symbol: str) -> str:
    lines = [f"# Backtest {symbol} — {date.today().isoformat()}", "",
             f"{cmp['_days']} días. **Resultados históricos: no son una predicción del futuro.**", "",
             "| | Capital final | CAGR | Sharpe | Sortino | Máx. drawdown | Operaciones | Comisiones "
             "| Tiempo invertido |", "|---|---|---|---|---|---|---|---|---|"]
    for name, m in cmp.items():
        if name.startswith("_"):
            continue
        fees = f"{m['comisiones']:.2f} €" if "comisiones" in m else "—"
        invested = f"{m['invertido']:.0%}" if "invertido" in m else "—"
        lines.append(f"| {name} | {m['final']:.2f} € | {m['cagr']:.1%} | {m['sharpe']:.2f} | "
                     f"{m['sortino']:.2f} | {m['max_drawdown']:.1%} | {m.get('operaciones', '')} | "
                     f"{fees} | {invested} |")
    lines += ["", "## Walk-forward (umbrales)", "",
              f"{len(wf['folds'])} ventanas. Fuera de muestra: parámetros optimizados "
              f"×{wf['oos_candidate']:.3f} frente a actuales ×{wf['oos_current']:.3f}.", "",
              "## Monte Carlo de sobreajuste", "",
              f"Resultado real {mc['original']:.2f} € frente a mediana simulada "
              f"{mc['median_sim']:.2f} € → percentil {mc['percentile']:.0f}. "
              + ("**Sospechoso de overfit.**" if mc["suspicious"] else "No sospechoso."), "",
              "## Noticias", "",
              f"Histórico propio de titulares: {news_days} días. El backtest de la parte de noticias "
              "solo es válido sobre este histórico (sesgo de look-ahead de los LLM: Glasserman y "
              "Lin 2023; arXiv 2512.23847). Con menos de ~6 meses no hay muestra suficiente.", ""]
    return "\n".join(lines)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--config", default="config.toml")
    p.add_argument("--days", type=int, default=1095)
    p.add_argument("--mc-runs", type=int, default=100)
    p.add_argument("--symbol", help="moneda a analizar (por defecto la primera de general.symbols)")
    args = p.parse_args()
    cfg = config_mod.load(args.config)
    g, a = cfg["general"], cfg["adapt"]
    symbol = args.symbol or g["symbols"][0]
    candles = fetch_history(market.make_exchange(g["exchange"], markets=cfg.get("market")), symbol, args.days)
    feats = precompute(candles, cfg)
    cmp = compare(candles, cfg, feats)
    wf = walk_forward(candles, cfg, grid(cfg), a["train_days"], a["test_days"], feats)
    mc = overfit_test(candles, cfg, args.mc_runs)
    data = Path(g["data_dir"])
    data.mkdir(parents=True, exist_ok=True)
    with closing(db.connect(data / "newsbot.db")) as con:
        news_days = con.execute("SELECT count(DISTINCT day) FROM headlines").fetchone()[0]
    out = Path("reports") / f"backtest_{symbol.split('/')[0]}.md"
    out.parent.mkdir(exist_ok=True)
    out.write_text(render(cmp, wf, mc, news_days, symbol), encoding="utf-8")
    print(f"Informe -> {out}")


if __name__ == "__main__":
    main()
