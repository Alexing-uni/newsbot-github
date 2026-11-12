"""Componentes técnicos de la bolsa (stocks/technical.py) sobre velas sintéticas."""
import pytest

from newsbot.stocks import technical

DAY = 86_400_000


def bars(closes, spread=0.01):
    return [[i * DAY, c, c * (1 + spread), c * (1 - spread), c, 1.0] for i, c in enumerate(closes)]


def test_breakout_channel_and_breaks():
    flat = [100.0] * 20
    assert technical.breakout_score(bars(flat + [102.0])) == 1.0   # cierra sobre el máximo (101)
    assert technical.breakout_score(bars(flat + [98.0])) == -1.0   # bajo el mínimo (99)
    assert technical.breakout_score(bars(flat + [100.0])) == pytest.approx(0.0)
    assert technical.breakout_score(bars(flat + [100.5])) == pytest.approx(0.5)
    assert technical.breakout_score(bars(flat)) == 0.0  # pocas velas


def test_macd_follows_momentum():
    up = [100 * 1.01**i for i in range(60)]
    accel = [100.0] * 40 + [100 * 1.02**i for i in range(1, 21)]
    drop = [100.0] * 40 + [100 * 0.98**i for i in range(1, 21)]
    turn = up[:40] + [up[39] * 0.99**i for i in range(1, 11)]  # sube y se da la vuelta
    assert technical.macd_score(up) > 0 and technical.macd_score(drop) < -0.5
    assert technical.macd_score(accel) > technical.macd_score(up) > 0 > technical.macd_score(turn)
    assert technical.macd_score([100.0] * 60) == 0.0
    assert technical.macd_score(up[:30]) == 0.0  # hacen falta slow + signal cierres
    assert -1 <= technical.macd_score([100.0] * 59 + [200.0]) <= 1


def test_high52_distance_to_the_max():
    def score(last):
        return technical.high52_score(bars([100.0] * 99 + [last], spread=0.0))

    assert score(100.0) == 1.0
    assert score(95.0) == pytest.approx(0.5)
    assert score(90.0) == pytest.approx(0.0)
    assert score(80.0) == pytest.approx(-1.0) and score(50.0) == -1.0
    old_top = bars([200.0] + [100.0] * 260, spread=0.0)  # el máximo de hace > 252 sesiones no cuenta
    assert technical.high52_score(old_top) == 1.0
    assert technical.high52_score(bars([100.0] * 10)) == 0.0


def test_features_keys_and_bounds():
    f = technical.features(bars([100 * 1.01**i for i in range(260)]))
    assert set(f) == {"breakout", "macd", "high52", "dip"}
    assert all(-1 <= v <= 1 for v in f.values()) and f["breakout"] == 1.0 and f["macd"] > 0
