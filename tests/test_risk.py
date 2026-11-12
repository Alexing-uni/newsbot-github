"""Reglas de decisión y riesgo, con casos límite."""
import pytest

from newsbot.execution.paper import PaperBroker
from newsbot.risk import sizing
from newsbot.strategy.fusion import decide, smooth, track_high

STRONG = dict(news=1.0, trend=1.0, intraday=1.0, fng=1.0, votes=1.0, atr_pct=0.04)


def state(cfg, **kw):
    base = dict(news=0.0, trend=0.0, price=100.0, cash=10.0, qty=0.0, avg_entry=0.0,
                min_order_eur=5.0, cfg=cfg, atr_pct=0.04)
    return decide(**{**base, **kw})


def test_buy_on_strong_signal(cfg):
    d = state(cfg, **STRONG)
    assert d.action == "BUY" and d.target_exposure == 1.0 and d.signal == pytest.approx(1.0)
    assert set(d.contributions) == {"news", "trend", "intraday", "fng", "votes"}
    assert d.order_eur == pytest.approx(10 / 1.0025)  # deja sitio para la comisión


def test_hold_on_neutral_signal(cfg):
    assert state(cfg, trend=0.1).action == "HOLD"


def test_sell_everything_on_sell_signal(cfg):
    d = state(cfg, news=-1.0, trend=-1.0, cash=0.0, qty=0.1, avg_entry=100)
    assert d.action == "SELL" and d.target_exposure == 0.0 and d.order_eur == pytest.approx(-10)


def test_stop_loss_has_priority_over_buy_signal(cfg):
    d = state(cfg, **STRONG, price=91.0, cash=0.0, qty=0.1, avg_entry=100.0)
    assert d.action == "SELL" and "stop-loss" in d.reason


def test_trailing_stop(cfg):
    cfg["risk"]["trailing_stop_pct"] = 0.10
    d = state(cfg, **STRONG, price=105.0, cash=0.0, qty=0.1, avg_entry=90.0, high_since_entry=120.0)
    assert d.action == "SELL" and "trailing" in d.reason
    cfg["risk"]["trailing_stop_pct"] = 0.0
    assert "trailing" not in state(cfg, **STRONG, price=105.0, cash=0.0, qty=0.1, avg_entry=90.0,
                                   high_since_entry=120.0).reason


def test_no_trade_below_exchange_minimum(cfg):
    d = state(cfg, **STRONG, cash=4.0)
    assert d.action == "HOLD" and "mínimo" in d.reason


def test_capital_zero_does_not_crash(cfg):
    assert state(cfg, **STRONG, cash=0.0).action == "HOLD"


def test_no_trade_when_rebalance_too_small(cfg):
    # 100 € con 70 % invertido; objetivo 60 % → ajuste de 10 € < 20 % del capital
    d = state(cfg, news=0.5, trend=0.5, cash=30.0, qty=0.7)
    assert d.action == "HOLD"


def test_bear_regime_raises_buy_threshold(cfg):
    kw = dict(news=0.4, trend=0.4, atr_pct=0.06)  # señal 0,32: compra salvo en régimen bajista
    assert state(cfg, **kw).action == "BUY"
    d = state(cfg, **kw, regime="bear")
    assert d.action == "HOLD" and "bajista" in d.reason


def test_kill_switch_blocks_buys_but_allows_sells(cfg):
    d = state(cfg, **STRONG, cash=7.0, peak=10.0)  # drawdown 30 % >= 25 %
    assert d.kill_switch and d.action == "HOLD" and "kill-switch" in d.reason
    assert state(cfg, **STRONG, buy_blocked=True).action == "HOLD"
    d = state(cfg, news=-1.0, trend=-1.0, cash=0.0, qty=0.07, avg_entry=100.0, peak=10.0)
    assert d.kill_switch and d.action == "SELL"
    assert not state(cfg, **STRONG, cash=8.0, peak=10.0).kill_switch  # drawdown 20 %


def test_edge_filter_blocks_when_move_does_not_cover_costs(cfg):
    d = state(cfg, **{**STRONG, "atr_pct": 0.01})  # 1 % < 2 × 0,7 %
    assert d.action == "HOLD" and "sin ventaja" in d.reason
    assert sizing.has_edge(0.04, 0.5, 0.0025, 0.001, 2.0)
    assert not sizing.has_edge(0.02, 0.5, 0.0025, 0.001, 2.0)


def test_vol_target_scales_exposure(cfg):
    cfg["risk"]["vol_target"] = 0.5
    cfg["risk"]["min_rebalance_pct"] = 0.0
    d = state(cfg, **STRONG, vol=1.0)
    assert d.target_exposure == pytest.approx(0.5) and "vol" in d.reason
    assert sizing.vol_scale(1.0, 0.3, 0.5) == 1.0 and sizing.vol_scale(1.0, 1.0, 0.0) == 1.0


def test_smooth_and_track_high():
    assert smooth(None, 0.6, 3) == 0.6
    assert smooth(0.0, 0.6, 3) == pytest.approx(0.3)  # alpha = 2 / (3 + 1)
    assert track_high(0.0, 1.0, 0.0, 100.0) == 100.0
    assert track_high(1.0, 1.0, 120.0, 100.0) == 120.0
    assert track_high(1.0, 0.0, 120.0, 100.0) == 0.0


def test_paper_broker_applies_fee_and_slippage():
    b = PaperBroker(0.0025, 0.001, cash=10.0)
    f = b.buy(5.0, 100.0)
    assert f.price == pytest.approx(100.1) and b.cash == pytest.approx(10 - 5 - 0.0125)
    b.sell(b.qty, 100.0)
    assert b.qty == 0 and b.cash < 10.0  # ida y vuelta siempre cuesta
