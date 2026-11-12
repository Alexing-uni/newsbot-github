"""Relleno histórico de ticks para que el aprendizaje no empiece de cero.

Reconstruye, cada vela intradía (15 min) de los últimos N días, los componentes que salen del
PRECIO (tendencia, impulso, RSI, Bollinger, momentum, volumen) y el Fear & Greed, usando en cada
instante solo velas ya cerradas y el F&G del día anterior: lo mismo que el bot habría visto.
Las NOTICIAS no se rellenan: los feeds RSS solo dan los últimos titulares y puntuar titulares
antiguos con un LLM tiene sesgo de look-ahead (ver README). Se miden desde el primer tick real.
Los ticks rellenados llevan action='BACKFILL' y signal NULL (la señal fusionada sin noticias no
sería la del bot), así que /senales y la "señal fusionada" de /aprendizaje no los usan.
Con [session] (bolsa) solo hay ticks con el mercado abierto y vela corta reciente, sin F&G
(news.fng = false), y Yahoo da como mucho 59 días de velas de 15 min: lo anterior se rellena con
velas de 1 h (hasta 2 años), un tick por hora y SIN el componente intradía (que se calcula con
velas de 15 min: con las de 1 h sería otra escala y confundiría al aprendizaje).
python -m newsbot.research.backfill [--config config.toml] [--days 60]
"""
import argparse
from contextlib import closing
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from newsbot import config as config_mod
from newsbot.features import market, session
from newsbot.http import get_json
from newsbot.news import fng
from newsbot.storage import db

FNG_URL = "https://api.alternative.me/fng/?limit={n}"
INTRADAY_MAX_DAYS = {"yahoo": 59}  # días de velas cortas que da cada fuente (sin entrada: sin límite)
HOURLY_MAX_DAYS = {"yahoo": 727}   # y de velas de 1 h, para rellenar antes de las cortas


def fetch_ohlcv(ex, symbol: str, timeframe: str, since_ms: int, until_ms: int,
                limit: int = 1000) -> list[list[float]]:
    """Velas entre `since_ms` y `until_ms`, paginando (Bitvavo devuelve como mucho 1440)."""
    out: dict[int, list[float]] = {}
    cur = since_ms
    while cur < until_ms:
        batch = [c for c in market.with_retry(ex.fetch_ohlcv, symbol, timeframe, since=cur,
                                              limit=limit) if c[0] >= cur]
        if not batch:
            break
        out |= {int(c[0]): c for c in batch if c[0] < until_ms}
        cur = int(batch[-1][0]) + market.TIMEFRAME_MS[timeframe]
    return [out[k] for k in sorted(out)]


def fng_history(days: int, get=get_json) -> dict[str, int]:
    """{día UTC: valor} del Fear & Greed (alternative.me publica uno al día)."""
    data = get(FNG_URL.format(n=days + 3))["data"]
    return {datetime.fromtimestamp(int(d["timestamp"]), UTC).date().isoformat(): int(d["value"])
            for d in data}


def build_ticks(symbol: str, daily: list, short: list, fng_by_day: dict[str, int], cfg: dict,
                start_ts: int, end_ts: int, tf: str | None = None) -> list[dict]:
    """Un tick por vela corta en [start_ts, end_ts), cada uno solo con datos ya cerrados. Con `tf`
    distinto de la vela intradía de la estrategia (relleno antiguo en 1 h) no hay componente intradía."""
    s = cfg["strategy"]
    tf = tf or s["intraday_timeframe"]
    intraday = tf == s["intraday_timeframe"]
    step_ms = market.TIMEFRAME_MS[tf]
    first = -(-start_ts * 1000 // step_ms) * step_ms  # primer múltiplo de la vela corta
    rows, di, si, feats_at = [], 0, 0, (-1, {})
    for ms in range(first, end_ts * 1000, step_ms):
        while di < len(daily) and daily[di][0] + market.DAY_MS <= ms:
            di += 1
        while si < len(short) and short[si][0] + step_ms <= ms:
            si += 1
        if di < s["sma_slow"] + 1 or si < s["ema_slow"]:
            continue  # historia insuficiente: el bot tampoco habría tenido señal
        if "session" in cfg and (short[si - 1][0] + 2 * step_ms < ms or not session.is_open(
                cfg, datetime.fromtimestamp(ms // 1000, UTC))):
            continue  # mercado cerrado o última vela de hace más de un paso: precio viejo
        if feats_at[0] != di:  # las diarias solo cambian una vez al día
            feats_at = (di, market.features(daily[max(0, di - 250):di], cfg))
        f = feats_at[1]
        closes = [c[4] for c in short[max(0, si - 100):si]]
        ts = ms // 1000
        prev_day = (datetime.fromtimestamp(ts, UTC).date() - timedelta(days=1)).isoformat()
        raw = {"trend": f["trend"], **{k: f[k] for k in market.SHADOW}, "_regime": f["regime"],
               "_atr": f["atr_pct"], "_backfill": True}
        if intraday:
            raw["intraday"] = market.intraday_score(closes, s["ema_fast"], s["ema_slow"])
        else:
            raw["_tf"] = tf
        if (value := fng_by_day.get(prev_day)) is not None:
            raw["fng"] = fng.contrarian(value)
        rows.append({"ts": ts, "symbol": symbol, "price": closes[-1], "signal": None,
                     "action": "BACKFILL", "reason": "histórico (solo precio)", "held": False,
                     "raw": raw, "contributions": {}})
    return rows


def run(con, cfg: dict, ex, now: datetime, days: int, get=get_json) -> dict[str, int]:
    """Rellena los `days` días anteriores al primer tick real (o a `now`). Idempotente: borra
    antes el relleno anterior."""
    now_ts = int(now.timestamp())
    first_live = con.execute("SELECT min(ts) FROM ticks WHERE action != 'BACKFILL'").fetchone()[0]
    end_ts = min(first_live or now_ts, now_ts)
    start_ts = want_ts = end_ts - days * 86400
    short_since = start_ts - 2 * 86400
    if (max_days := INTRADAY_MAX_DAYS.get(cfg["general"]["exchange"])) is not None:
        short_since = max(short_since, now_ts - max_days * 86400)
        start_ts = max(start_ts, short_since + 2 * 86400)  # 2 días de velas previas para el impulso
    hourly_start = None  # antes de las velas cortas, velas de 1 h (si la fuente las da)
    if want_ts < start_ts and (h_days := HOURLY_MAX_DAYS.get(cfg["general"]["exchange"])):
        hourly_start = max(want_ts, now_ts - h_days * 86400 + 7 * 86400)
    fng_by_day = fng_history(days, get) if cfg["news"].get("fng", True) else {}
    tf = cfg["strategy"]["intraday_timeframe"]
    with con:
        con.execute("DELETE FROM ticks WHERE action = 'BACKFILL'")
    out = {}
    for symbol in cfg["general"]["symbols"]:
        daily = fetch_ohlcv(ex, symbol, "1d", (min(start_ts, hourly_start or start_ts) - 300 * 86400) * 1000,
                            end_ts * 1000)
        short = fetch_ohlcv(ex, symbol, tf, short_since * 1000, end_ts * 1000)
        rows = build_ticks(symbol, daily, short, fng_by_day, cfg, start_ts, end_ts)
        if hourly_start is not None and hourly_start < start_ts:
            hourly = fetch_ohlcv(ex, symbol, "1h", (hourly_start - 7 * 86400) * 1000, start_ts * 1000)
            rows = build_ticks(symbol, daily, hourly, fng_by_day, cfg, hourly_start, start_ts, "1h") + rows
        for i in range(0, len(rows), 2000):  # transacciones cortas: el bucle sigue escribiendo
            db.save_ticks(con, now, rows[i:i + 2000])
        out[symbol] = len(rows)
    db.KV(con).set("backfill:last", now.isoformat())
    return out


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--config", default="config.toml")
    p.add_argument("--days", type=int, default=None, help="por defecto, learn.lookback_days")
    args = p.parse_args()
    cfg = config_mod.load(args.config)
    from newsbot.research import learn
    days = args.days or learn.settings(cfg)["lookback_days"]
    data = Path(cfg["general"]["data_dir"])
    data.mkdir(parents=True, exist_ok=True)
    with closing(db.connect(data / "newsbot.db")) as con:
        ex = market.make_exchange(cfg["general"]["exchange"], markets=cfg.get("market"))
        counts = run(con, cfg, ex, datetime.now(UTC), days)
        p = learn.progress(con, cfg, datetime.now(UTC))
    print("Ticks históricos: " + ", ".join(f"{k} {v}" for k, v in counts.items()))
    print(f"Memoria: {p['pct']:.0%} ({p['n_eff']:.0f}/{p['min_n_eff']} muestras efectivas a "
          f"{p['horizon']}) desde {date.today() - timedelta(days=days)}")


if __name__ == "__main__":
    main()
