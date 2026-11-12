"""Componentes sombra (rsi, bollinger, momentum, volumen), ticks y journal."""
import json

import pytest
from conftest import NOW, FakeExchange, uptrend

from newsbot import config as config_mod
from newsbot.bot import _connect, run_once
from newsbot.features import market
from newsbot.reporting import journal
from newsbot.storage import db
from newsbot.strategy.fusion import decide


def test_rsi_extremes_and_flat():
    assert market.rsi_score([float(i) for i in range(1, 40)]) == pytest.approx(-1.0)  # sobrecompra
    assert market.rsi_score([float(i) for i in range(40, 1, -1)]) == pytest.approx(1.0)
    assert market.rsi_score([5.0] * 30) == 0.0
    assert market.rsi_score([1.0, 2.0]) == 0.0  # historia insuficiente


def test_bollinger_momentum_volume():
    flat_then_up = [100.0] * 19 + [110.0]
    assert market.bollinger_score(flat_then_up) < 0  # por encima de la banda: contraria negativa
    assert market.bollinger_score([100.0] * 25) == 0.0
    assert market.momentum_score([100.0] * 7 + [105.0]) == pytest.approx(0.5)
    assert market.momentum_score([100.0] * 7 + [50.0]) == -1.0
    candles = [[i, 1, 1, 1, 100.0, 10.0] for i in range(20)] + [[20, 1, 1, 1, 90.0, 30.0]]
    assert market.volume_score(candles) == -1.0  # cae con el triple de volumen


def test_shadow_components_only_count_with_weight(cfg):
    base = dict(news=0.0, trend=0.0, price=100.0, cash=10.0, qty=0.0, avg_entry=0.0,
                min_order_eur=5.0, cfg=cfg, atr_pct=0.04, extra={"rsi": 1.0, "momentum": 1.0})
    d = decide(**base)
    assert "rsi" not in d.contributions and d.signal == 0.0
    w = cfg["weights"]
    cfg2 = config_mod.apply(cfg, {"weights": {"news": w["news"] - 0.1, "rsi": 0.1}})
    d2 = decide(**{**base, "cfg": cfg2})
    assert d2.contributions["rsi"] == pytest.approx(0.1) and "momentum" not in d2.contributions


@pytest.mark.parametrize("override,msg", [
    ({"rsi": 0.1}, "sumar 1"),
    ({"news": 0.5, "magic": -0.1}, "desconocidos"),
    ({"news": 0.5, "rsi": -0.1}, "negativos"),
])
def test_weight_validation(cfg, override, msg):
    with pytest.raises(ValueError, match=msg):
        config_mod.apply(cfg, {"weights": override})


def test_every_tick_is_logged_with_raw_components(cfg):
    run_once(cfg, FakeExchange(uptrend()), NOW)
    run_once(cfg, FakeExchange(uptrend()), NOW.replace(minute=10), intraday=True)
    with _connect(cfg) as con:
        rows = con.execute("SELECT * FROM ticks ORDER BY ts").fetchall()
        latest = db.latest_ticks(con)
    assert len(rows) == 2 and [r["symbol"] for r in latest] == ["BTC/EUR"]
    raw = json.loads(latest[0]["raw_json"])
    assert set(raw) == {"news", "trend", "intraday", "fng", "votes", *market.SHADOW, "_regime", "_atr"}
    assert latest[0]["ts"] == int(NOW.replace(minute=10).timestamp())


def test_dry_run_logs_no_ticks(cfg):
    run_once(cfg, FakeExchange(uptrend()), NOW, dry_run=True)
    with _connect(cfg) as con:
        assert con.execute("SELECT count(*) FROM ticks").fetchone()[0] == 0


def test_journal_rotates_when_columns_change(tmp_path):
    path = tmp_path / "journal.csv"
    journal.append(path, {"a": 1, "b": 2})
    journal.append(path, {"a": 1, "b": 2, "c": 3})
    journal.append(path, {"a": 1})
    assert path.read_text(encoding="utf-8").splitlines() == ["a", "1"]
    assert (tmp_path / "journal.1.csv").read_text(encoding="utf-8").splitlines()[0] == "a,b"
    assert (tmp_path / "journal.2.csv").exists()
