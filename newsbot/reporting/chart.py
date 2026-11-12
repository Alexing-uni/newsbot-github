"""Gráfico de un valor para Telegram (PNG dibujado en tu ordenador) y su enlace a TradingView.

Velas de 15 min de las últimas sesiones (Yahoo, en euros) con las líneas que importan: entrada, stop
y objetivo de la posición, y el precio de la propuesta pendiente. Las horas sin mercado (noches y
fines de semana) no se dibujan: el eje X va por velas, con la fecha al empezar cada sesión.
"""
import io
import sqlite3
import urllib.parse
from datetime import UTC, datetime

from newsbot import config as config_mod
from newsbot.features import market
from newsbot.stocks import sizing
from newsbot.storage import db

TV = "https://www.tradingview.com/chart/?symbol="
TV_EXCHANGE = {"DE": "XETR", "MI": "MIL", "PA": "EURONEXT", "AS": "EURONEXT", "MC": "BME", "L": "LSE"}
BARS = 104  # ~4 sesiones de EE. UU. (26 velas de 15 min) o ~3 de Xetra


def tradingview_url(cfg: dict, symbol: str) -> str:
    """[market.CODE].tv si está ("NASDAQ:NVDA"); si no, el ticker de Yahoo traducido ("RHM.DE" → XETR:RHM)."""
    m = (cfg.get("market") or {}).get(config_mod.asset(symbol)) or {}
    tv = m.get("tv")
    if not tv:
        base, _, suffix = (m.get("ticker") or config_mod.asset(symbol)).partition(".")
        tv = f"{TV_EXCHANGE[suffix]}:{base}" if suffix in TV_EXCHANGE else base
    return TV + urllib.parse.quote(tv, safe=":")


def levels(con: sqlite3.Connection, cfg: dict, symbol: str) -> dict[str, float]:
    """Líneas horizontales: entrada/stop/objetivo de la posición y la propuesta pendiente."""
    out: dict[str, float] = {}
    pos = con.execute("SELECT * FROM positions WHERE symbol = ? AND qty > 0", (symbol,)).fetchone()
    if pos and pos["avg_entry"]:
        out["entrada"] = pos["avg_entry"]
        if pos["stop_price"]:
            out["stop"] = pos["stop_price"]
        st = db.last_state(con)
        tp = sizing.with_tier(cfg, st["equity"] if st else 0.0)["risk"].get("take_profit_pct", 0.0)
        if tp > 0:
            out["objetivo"] = pos["avg_entry"] * (1 + tp)
    prop = con.execute("SELECT * FROM proposals WHERE symbol = ? AND status = 'proposed' ORDER BY id DESC "
                       "LIMIT 1", (symbol,)).fetchone()
    if prop and prop["ref_price"]:
        out["propuesta"] = prop["ref_price"]
        if prop["side"] == "buy" and prop["stop_price"]:
            out.setdefault("stop", prop["stop_price"])
    return out


COLORS = {"entrada": "#2563eb", "stop": "#dc2626", "objetivo": "#16a34a", "propuesta": "#9333ea"}


def png(candles: list[list[float]], title: str, lines: dict[str, float]) -> bytes:
    """PNG de cierres de 15 min (área) con `lines`. Usa la API de objetos de matplotlib (sin pyplot):
    segura en el hilo de Telegram."""
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure
    closes = [c[4] for c in candles]
    x = list(range(len(closes)))
    fig = Figure(figsize=(8, 4.2), dpi=110)
    FigureCanvasAgg(fig)
    ax = fig.add_subplot()
    up = closes[-1] >= closes[0]
    color = "#16a34a" if up else "#dc2626"
    ax.plot(x, closes, color=color, linewidth=1.6)
    ax.fill_between(x, closes, min(closes + list(lines.values())) * 0.998, color=color, alpha=0.08)
    for name, y in lines.items():
        ax.axhline(y, color=COLORS.get(name, "#6b7280"), linestyle="--", linewidth=1)
        ax.annotate(f"{name} {y:.2f}", (x[-1], y), xytext=(4, 0), textcoords="offset points",
                    color=COLORS.get(name, "#6b7280"), fontsize=8, va="center")
    days, ticks = [], []
    for i, c in enumerate(candles):  # una marca al empezar cada sesión
        d = datetime.fromtimestamp(c[0] / 1000, UTC).strftime("%d/%m")
        if not days or days[-1] != d:
            if ticks and i - ticks[-1] < 8:  # sesión cortada al principio: su fecha taparía la siguiente
                days.pop()
                ticks.pop()
            days.append(d)
            ticks.append(i)
    ax.set_xticks(ticks, days)
    ax.set_xlim(0, len(x) - 1 + max(8, len(x) // 6))  # sitio para las etiquetas de las líneas
    ax.grid(alpha=0.25)
    ax.set_ylabel("€")
    chg = closes[-1] / closes[0] - 1 if closes[0] else 0.0
    ax.set_title(f"{title} · {closes[-1]:.2f} € ({chg * 100:+.1f} % en el gráfico)", fontsize=11)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png")
    return buf.getvalue()


def render(cfg: dict, con: sqlite3.Connection, symbol: str, ex=None, now: datetime | None = None) -> bytes:
    """Descarga las velas y dibuja el gráfico del valor con sus líneas."""
    ex = ex or market.make_exchange(cfg["general"]["exchange"], markets=cfg.get("market"))
    now_ms = int((now or datetime.now(UTC)).timestamp() * 1000)
    candles = market.closed_candles(ex, symbol, now_ms, limit=BARS,
                                    timeframe=cfg["strategy"].get("intraday_timeframe", "15m"))
    if len(candles) < 2:
        raise ValueError("sin velas")
    m = (cfg.get("market") or {}).get(config_mod.asset(symbol)) or {}
    title = config_mod.asset(symbol) + (f" ({m['name'].split(' (')[0]})" if m.get("name") else "")
    return png(candles, title, levels(con, cfg, symbol))
