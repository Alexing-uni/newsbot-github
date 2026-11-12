"""Tamaño de las compras de bolsa: riesgo por ATR, topes por posición/sector/orden y caja mínima."""
import pytest
from conftest import ROOT

from newsbot import config as config_mod
from newsbot.stocks import sizing


@pytest.fixture
def scfg():
    return config_mod.load(ROOT / "config_bolsa.toml")


def size(cfg, equity=1000.0, cash=None, signal=0.25, atr=0.04, held=(), group="chips", group_eur=0.0):
    return sizing.order_eur(equity, equity if cash is None else cash, signal, 0.25, atr, list(held), group,
                            group_eur, cfg)


def test_stop_pct_is_atr_based_and_clamped(scfg):
    assert sizing.stop_pct(0.02, scfg) == pytest.approx(0.05)  # 2,5 × ATR
    assert sizing.stop_pct(0.004, scfg) == 0.03 and sizing.stop_pct(0.2, scfg) == 0.12


def test_risk_based_size_and_strength(scfg):
    scfg["execution"]["max_order_eur"] = 1e6
    # ATR 4 % -> stop 10 % -> 2 % de 1000 € / 10 % = 200 € (< 35 % = 350 €); señal en el umbral = mitad
    assert size(scfg) == (pytest.approx(100.0), "tamaño por riesgo")
    assert size(scfg, signal=1.0)[0] == pytest.approx(200.0)
    # el más volátil, más pequeño: ATR 4,8 % -> stop 12 % -> 166,67 €
    assert size(scfg, signal=1.0, atr=0.048)[0] == pytest.approx(1000 * 0.02 / 0.12)


def test_position_cap(scfg):
    scfg["execution"]["max_order_eur"] = 1e6
    assert size(scfg, signal=1.0, atr=0.01) == (pytest.approx(350.0), "tope por posición")


def test_max_order_eur(scfg):
    assert size(scfg, signal=1.0) == (25.0, "tope por orden")


def test_cash_floor(scfg):
    assert size(scfg, equity=100, cash=30, signal=1.0) == (pytest.approx(10.0), "caja mínima")
    eur, why = size(scfg, equity=100, cash=20.5, signal=1.0)
    assert eur == 0 and "caja mínima" in why and "mínimo 1.00 €" in why


def test_group_limits(scfg):
    eur, why = size(scfg, equity=100, signal=1.0, group_eur=45.0)
    assert eur == pytest.approx(5.0) and why == "tope del sector chips"
    eur, why = size(scfg, held=["chips", "chips"])
    assert eur == 0 and why == "sector chips lleno (2 valores)"
    assert size(scfg, held=["chips", "defensa"])[0] > 0


def test_max_positions(scfg):
    assert size(scfg, held=["oro", "defensa", "consumo"]) == (0.0, "máximo de posiciones (3)")


def test_min_order(scfg):
    eur, why = size(scfg, equity=4.0)  # 2 % de 4 € / 10 % = 0,80 € × 0,5 = 0,40 € < 1 €
    assert eur == 0 and "< mínimo 1.00 €" in why
    assert size(scfg, equity=0.0) == (0.0, "sin capital")


def test_capital_tiers_change_limits():
    from newsbot import config as config_mod
    usa = config_mod.load(ROOT / "config_usa.toml")
    small, big = sizing.with_tier(usa, 20.0), sizing.with_tier(usa, 5000.0)
    first, last = usa["portfolio"]["tiers"][0], usa["portfolio"]["tiers"][-1]  # hasta 50 €; sin tope
    assert small["portfolio"]["max_positions"] == first["max_positions"]
    assert small["risk"]["take_profit_pct"] == first["take_profit_pct"] and small["_tier"] == first["name"]
    assert big["portfolio"]["max_positions"] == last["max_positions"]
    assert big["risk"]["take_profit_pct"] == 0.0
    assert first["max_positions"] != last["max_positions"]
    assert sizing.with_tier({"portfolio": {"max_positions": 3}}, 20.0) == {"portfolio": {"max_positions": 3}}
