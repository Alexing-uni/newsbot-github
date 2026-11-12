"""Fixtures compartidas. Ningún test usa red: FakeExchange, RSS fijo y LLM/Telegram mockeados."""
import math
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from newsbot import config as config_mod
from newsbot.features import market
from newsbot.news import fng, ingest, rss

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 11, 2, 9, 5, tzinfo=ZoneInfo("Europe/Madrid"))
DAY0 = int(datetime(2026, 9, 3, tzinfo=UTC).timestamp() * 1000)

RSS = """<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>
<item><title>Bitcoin ETF inflows surge to record</title>
<description>&lt;p&gt;Big day&lt;/p&gt;</description>
<pubDate>Mon, 02 Nov 2026 06:00:00 GMT</pubDate></item>
<item><title>Bitcoin crash deepens</title><pubDate>Fri, 30 Oct 2026 06:00:00 GMT</pubDate></item>
<item><title>Local football results</title><pubDate>Mon, 02 Nov 2026 05:00:00 GMT</pubDate></item>
</channel></rss>"""


def candle(ts: int, c: float, o: float | None = None) -> list[float]:
    return [ts, o or c, c * 1.02, c * 0.98, c, 1.0]  # ATR ~4 %: el filtro de ventaja deja comprar


class FakeExchange:
    """`closes` son las velas de BTC/EUR; `others` = {símbolo: cierres} para el resto."""

    def __init__(self, closes, price=None, others=None, short=None):
        series = {"BTC/EUR": closes, **(others or {})}
        self.series = {s: [candle(DAY0 + i * market.DAY_MS, c) for i, c in enumerate(cl)]
                       for s, cl in series.items()}
        # velas cortas (15 min) que acaban con la última vela diaria; planas salvo que se indique
        self.short = {s: short.get(s) if short and s in short else [cl[-1]] * 100
                      for s, cl in series.items()}
        self.candles = self.series["BTC/EUR"]
        self.prices = {s: cl[-1] for s, cl in series.items()} | {"BTC/EUR": price or closes[-1]}
        self.price = self.prices["BTC/EUR"]
        self.options: dict = {}

    def milliseconds(self):
        return self.candles[-1][0] + market.DAY_MS + 1

    def fetch_ohlcv(self, symbol, timeframe, limit=None, since=None):
        if timeframe != "1d":
            step, end = market.TIMEFRAME_MS[timeframe], self.series[symbol][-1][0]
            cl = self.short[symbol]
            out = [candle(end - (len(cl) - i) * step, c) for i, c in enumerate(cl)]
            return out[-limit:] if limit else out
        out = [c for c in self.series[symbol] if since is None or c[0] >= since]
        return out[-limit:] if limit else out

    def fetch_ticker(self, symbol):
        return {"last": self.prices[symbol]}

    def load_markets(self):
        return {s: self.market(s) for s in self.series}

    def market(self, symbol):
        return {"base": symbol.split("/")[0], "quote": "EUR",
                "limits": {"cost": {"min": 5.0}, "amount": {"min": 1e-5}}}


def uptrend(n=61):  # la última vela es la del 2-nov (día en curso)
    return [50_000 * 1.01**i for i in range(n)]


def wave(n=700):
    """Serie con tendencias alcistas y bajistas para backtests."""
    return [50_000 * (1 + 0.3 * math.sin(i / 40)) * 1.0005**i for i in range(n)]


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    for k in ("ANTHROPIC_API_KEY", "CRYPTOPANIC_TOKEN", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
              "CONFIRM_LIVE", "BITVAVO_API_KEY", "BITVAVO_API_SECRET", "TRADING212_API_KEY",
              "TRADING212_API_SECRET"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(fng, "fetch", lambda: 50)  # zona neutra: aporta 0
    from newsbot import portfolio
    monkeypatch.setattr(portfolio, "REGISTRY", {})  # cada test empieza solo con la principal
    c = config_mod.load(ROOT / "config.toml")
    c["general"].pop("portfolios", None)  # los tests nunca arrancan la cartera de bolsa real
    c["general"].pop("enabled", None)  # ni dependen de que el usuario tenga la cripto apagada
    c["general"]["mode"] = "paper"  # los tests no dependen del modo del config.toml del usuario
    c["general"]["symbols"] = ["BTC/EUR"]  # los tests multi-moneda lo amplían explícitamente
    c["general"]["data_dir"] = str(tmp_path / "data")
    c["news"]["feeds"] = {"a": RSS, "b": RSS}  # feedparser acepta el XML como cadena
    c["sentiment"]["cascade"] = ["llm", "lexicon"]  # sin FinBERT: torch no está en los tests
    c["adapt"]["enabled"] = False
    return c


def fixed_headlines():
    return ingest.select(rss.fetch({"a": RSS}), ["bitcoin"], NOW, 24)
