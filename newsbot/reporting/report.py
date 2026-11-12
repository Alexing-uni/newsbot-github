import sqlite3

from newsbot.news.rss import Headline
from newsbot.strategy.fusion import Decision


def daily(day: str, mode: str, symbol: str, d: Decision, price: float, equity: float,
          initial: float, top: list[tuple[float, Headline]], fng_value: int | None,
          ranking: list[tuple[str, Decision]] = (), held: str | None = None,
          show_fng: bool = True) -> str:
    parts = "\n".join(f"  {k}: {v:+.3f}" for k, v in d.contributions.items())
    heads = "\n".join(f"  · ({w:.2f}) {h.title}" for w, h in top) or "  (sin titulares)"
    rank = "\n".join(f"  {s:<8} {x.signal:+.3f} {x.action:<4} {x.reason}"
                     for s, x in sorted(ranking, key=lambda r: r[1].signal, reverse=True))
    pnl = equity - initial
    return (f"newsbot [{mode}] {day} | posición: {held or 'ninguna'}\n"
            + (f"Señales del día:\n{rank}\n" if rank else "")
            + f"Decisión: {d.action} {symbol}  señal {d.signal:+.3f}\n{parts}\n"
            f"Motivo: {d.reason}\n"
            f"{symbol} {price:,.4f} | capital {equity:.2f} € | P&L {pnl:+.2f} € "
            f"({pnl / initial:+.1%})\n"
            + (f"F&G (alternative.me): {fng_value}\n" if show_fng else "")
            + f"Titulares de {symbol.split('/')[0]} con más peso:\n{heads}")


def weekly(con: sqlite3.Connection, initial: float, days: int = 7) -> str:
    rows = con.execute("SELECT day, equity FROM equity_daily ORDER BY day DESC LIMIT ?",
                       (days + 1,)).fetchall()[::-1]
    if not rows:
        return "Informe semanal: todavía no hay datos."
    since = rows[0]["day"]
    orders = con.execute("SELECT count(*), coalesce(sum(fee), 0) FROM orders "
                         "WHERE day > ? AND status = 'filled'", (since,)).fetchone()
    acts = con.execute("SELECT action, count(*) FROM decisions WHERE day > ? GROUP BY action",
                       (since,)).fetchall()
    start, end = rows[0]["equity"], rows[-1]["equity"]
    return (f"Informe semanal {since} → {rows[-1]['day']}\n"
            f"Capital: {start:.2f} € → {end:.2f} € ({end / start - 1:+.1%})\n"
            f"Desde el inicio: {end - initial:+.2f} € ({end / initial - 1:+.1%})\n"
            f"Decisiones: " + (", ".join(f"{a} {n}" for a, n in acts) or "ninguna") + "\n"
            f"Órdenes: {orders[0]} | comisiones: {orders[1]:.4f} €\n"
            "Rentabilidad pasada no garantiza la futura.")
