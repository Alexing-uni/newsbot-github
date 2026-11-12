"""Backtest, look-ahead, walk-forward, Monte Carlo y reoptimización."""
import json
import random
from contextlib import closing
from pathlib import Path

import pytest
from conftest import NOW, FakeExchange, wave

from newsbot.research import backtest, montecarlo, reoptimize, walkforward
from newsbot.research.metrics import metrics
from newsbot.research.report import render
from newsbot.storage import db

# ATR ~6 %: sin noticias la señal máxima es 0,35 y el filtro de ventaja pide ATR × 0,35 >= 1,4 %
CANDLES = [[i, c, c * 1.03, c * 0.97, c, 1.0] for i, c in enumerate(wave(700))]


def test_metrics_known_series():
    m = metrics([100.0] + [100.0 * 1.001**i for i in range(1, 366)])
    assert m["cagr"] == pytest.approx(1.001**365 - 1, rel=1e-6)
    assert m["max_drawdown"] == 0.0 and m["sortino"] == 0.0
    assert metrics([10.0, 5.0, 10.0])["max_drawdown"] == pytest.approx(0.5)


def test_backtest_trades_and_benchmarks(cfg):
    cmp = backtest.compare(CANDLES, cfg)
    s = cmp["Estrategia"]
    assert s["operaciones"] >= 2 and s["comisiones"] > 0 and 0 < s["invertido"] < 1
    assert cmp["Buy & hold"]["final"] > 0 and cmp["DCA semanal"]["final"] > 0
    assert cmp["_days"] == len(CANDLES) - backtest.WARMUP


def test_no_lookahead(cfg):
    """idea: freqtrade lookahead-analysis. La decisión del día i no cambia si se borra el futuro."""
    full = backtest.run(CANDLES, cfg)["decisions"]
    start = cfg["strategy"]["sma_slow"]
    for i in (100, 300, 450, 699):
        truncated = backtest.run(CANDLES[:i + 1], cfg)["decisions"]
        assert truncated[-1] == full[i - start]


def test_walk_forward_folds(cfg):
    wf = walkforward.walk_forward(CANDLES, cfg, walkforward.grid(cfg), 180, 60)
    assert len(wf["folds"]) == 4 and wf["last_best"] in walkforward.grid(cfg)
    assert walkforward.identity(cfg) in walkforward.grid(cfg)
    assert wf["oos_candidate"] > 0 and wf["oos_current"] > 0


def test_monte_carlo_is_reproducible_and_flags(cfg):
    syn = montecarlo.synthetic(CANDLES, random.Random(1))
    assert len(syn) == len(CANDLES) and syn[0] == CANDLES[0]
    a = montecarlo.overfit_test(CANDLES, cfg, runs=5, seed=3)
    assert a == montecarlo.overfit_test(CANDLES, cfg, runs=5, seed=3)
    assert 0 <= a["percentile"] <= 100 and a["suspicious"] == (a["percentile"] >= 95)


def test_report_renders(cfg):
    cmp = backtest.compare(CANDLES, cfg)
    md = render(cmp, {"folds": [], "oos_candidate": 1.0, "oos_current": 1.0},
                {"original": 10, "median_sim": 9, "percentile": 97, "suspicious": True}, 3,
                "BTC/EUR")
    assert "Buy & hold" in md and "Sospechoso de overfit" in md and "no son una predicción" in md


def test_propose_rejects_without_history(cfg):
    p = reoptimize.propose(CANDLES[:300], cfg)
    assert not p["accepted"] and "historial insuficiente" in p["reasons"][0]


def test_propose_checks_all_three_rules(cfg, monkeypatch):
    cand = {"strategy": {"buy_threshold": 0.3, "sell_threshold": -0.15}}
    wf = {"last_best": cand, "oos_candidate": 1.2, "oos_current": 1.0}
    monkeypatch.setattr(reoptimize, "walk_forward", lambda *a, **k: wf)
    monkeypatch.setattr(reoptimize, "overfit_test", lambda *a, **k: {"suspicious": False})
    assert reoptimize.propose(CANDLES, cfg)["accepted"]
    monkeypatch.setattr(reoptimize, "overfit_test", lambda *a, **k: {"suspicious": True})
    assert "overfit" in reoptimize.propose(CANDLES, cfg)["reasons"][0]
    wf["oos_candidate"] = 0.9
    assert "fuera de muestra" in reoptimize.propose(CANDLES, cfg)["reasons"][0]
    wf["last_best"] = {"strategy": {"buy_threshold": 0.5, "sell_threshold": -0.15}}
    assert any("max_weight_step" in r for r in reoptimize.propose(CANDLES, cfg)["reasons"])


@pytest.mark.parametrize("mode,auto,auto_live,status", [("paper", True, False, "applied"),
                                                         ("paper", False, True, "proposed"),
                                                         ("live", True, False, "proposed"),
                                                         ("live", False, True, "applied")])
def test_reoptimize_auto_apply_policy(cfg, monkeypatch, mode, auto, auto_live, status):
    cfg["general"]["mode"], cfg["adapt"]["auto_apply_in_paper"] = mode, auto
    cfg["adapt"]["auto_apply_live"] = auto_live
    params = {"strategy": {"buy_threshold": 0.3, "sell_threshold": -0.15}}
    monkeypatch.setattr(reoptimize, "propose", lambda c, f: {
        "params": params, "accepted": True, "reasons": [], "oos_candidate": 1.2,
        "oos_current": 1.0, "mc": {"suspicious": False}})
    data = Path(cfg["general"]["data_dir"])
    data.mkdir(parents=True)
    with closing(db.connect(data / "newsbot.db")) as con:
        cfg["adapt"]["enabled"] = True
        assert reoptimize.due(con, cfg, NOW)
        msg = reoptimize.run(con, cfg, FakeExchange(wave(400)), NOW)
        row = con.execute("SELECT status, params_json FROM params_history").fetchone()
        assert row["status"] == status and json.loads(row["params_json"]) == params
        assert ("/aprobar 1" in msg) == (status == "proposed")
        assert ("/deshacer 1" in msg) == (status == "applied")
        assert not reoptimize.due(con, cfg, NOW.replace(day=10))  # cadencia de 14 días
