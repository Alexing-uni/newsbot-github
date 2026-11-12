"""Motor de backtest de la parte de precio. Usa las mismas funciones puras que el bot
(market.features, decide, execute, PaperBroker): no hay lógica duplicada.

No usa vectorbt: la decisión depende del estado (stop-loss, trailing, kill-switch, posición), así que
hay que recorrer los días uno a uno; vectorizarlo obligaría a reescribir decide().
"""
import math

from newsbot.execution.paper import PaperBroker, execute
from newsbot.features import market
from newsbot.research.metrics import metrics
from newsbot.strategy.fusion import decide, track_high

WARMUP = 260  # velas de historia que necesitan la SMA200 y compañía


def precompute(candles: list[list[float]], cfg: dict) -> list[dict]:
    """features[i] usa solo velas < i: lo que el bot sabría al abrir el día i."""
    return [market.features(candles[max(0, i - WARMUP):i], cfg) for i in range(len(candles))]


def run(candles: list[list[float]], cfg: dict, feats: list[dict] | None = None,
        start: int | None = None, end: int | None = None) -> dict:
    """Decide el día i con velas < i y ejecuta en la apertura de i. Sin noticias (news = 0)."""
    feats = feats or precompute(candles, cfg)
    start = cfg["strategy"]["sma_slow"] if start is None else start
    end = len(candles) if end is None else end
    cap, c = cfg["general"]["initial_capital"], cfg["costs"]
    broker = PaperBroker(c["fee_pct"], c["slippage_pct"], cap)
    peak, high, blocked = cap, 0.0, False
    equity, decisions = [], []
    trades, fees, invested = 0, 0.0, 0
    for i in range(start, end):
        price, f = candles[i][1], feats[i]
        d = decide(news=0.0, extra={k: f[k] for k in market.SHADOW},
                   trend=f["trend"], price=price, cash=broker.cash, qty=broker.qty,
                   avg_entry=broker.avg_entry, min_order_eur=cfg["risk"]["min_order_eur"], cfg=cfg,
                   regime=f["regime"], atr_pct=f["atr_pct"], vol=f["vol"], high_since_entry=high,
                   peak=peak, buy_blocked=blocked)
        qty_before = broker.qty
        fill = execute(broker, d, price)
        if fill:
            trades, fees = trades + 1, fees + fill.fee
        high = track_high(qty_before, broker.qty, high, price)
        eq = broker.cash + broker.qty * candles[i][4]
        peak, blocked = max(peak, eq), blocked or d.kill_switch  # sin reactivación manual
        invested += broker.qty > 0
        equity.append(eq)
        decisions.append(d)
    return {"equity": equity, "decisions": decisions, "trades": trades, "fees": fees,
            "invested_days": invested, "final_equity": equity[-1] if equity else cap}


def buy_hold(candles: list[list[float]], cfg: dict, start: int) -> list[float]:
    c, cap = cfg["costs"], cfg["general"]["initial_capital"]
    b = PaperBroker(c["fee_pct"], c["slippage_pct"], cap)
    b.buy(cap, candles[start][1])
    return [b.cash + b.qty * x[4] for x in candles[start:]]


def dca(candles: list[list[float]], cfg: dict, start: int, every: int = 7) -> list[float]:
    """Mismo capital repartido en compras semanales. Ignora el mínimo de orden (es solo referencia)."""
    c, cap = cfg["costs"], cfg["general"]["initial_capital"]
    b = PaperBroker(c["fee_pct"], c["slippage_pct"], cap)
    chunk = cap / math.ceil((len(candles) - start) / every)
    out = []
    for i in range(start, len(candles)):
        if (i - start) % every == 0:
            b.buy(min(chunk, b.cash / (1 + c["fee_pct"])), candles[i][1])
        out.append(b.cash + b.qty * candles[i][4])
    return out


def compare(candles: list[list[float]], cfg: dict, feats: list[dict] | None = None) -> dict:
    start = min(WARMUP, len(candles) - 2)
    cap = cfg["general"]["initial_capital"]
    r = run(candles, cfg, feats, start=start)
    days = len(r["equity"])
    return {
        "Estrategia": {**metrics([cap] + r["equity"]), "operaciones": r["trades"],
                       "comisiones": r["fees"], "invertido": r["invested_days"] / max(days, 1)},
        "Buy & hold": {**metrics([cap] + buy_hold(candles, cfg, start)), "operaciones": 1,
                       "invertido": 1.0},
        "DCA semanal": {**metrics([cap] + dca(candles, cfg, start)),
                        "operaciones": math.ceil(days / 7)},
        "_days": days,
    }


def fetch_history(ex, symbol: str, days: int) -> list[list[float]]:
    since = ex.milliseconds() - days * market.DAY_MS
    out: list[list[float]] = []
    while True:
        batch = market.with_retry(ex.fetch_ohlcv, symbol, "1d", since=since, limit=1000)
        batch = [b for b in batch if not out or b[0] > out[-1][0]]
        if not batch:
            break
        out += batch
        since = out[-1][0] + market.DAY_MS
    return [b for b in out if b[0] + market.DAY_MS <= ex.milliseconds()]
