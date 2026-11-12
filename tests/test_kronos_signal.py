"""Componente "kronos": lote, caché por vela y nunca rompe el tick (sin cargar el modelo real)."""
import pandas as pd
import pytest

from newsbot.stocks import kronos_signal as ks


def _candles(n=100, start=100.0, step=0.1, t0=0):
    return [[t0 + i * 900_000, start + i * step, start + i * step + 1, start + i * step - 1,
             start + i * step, 1000.0] for i in range(n)]


class FakePredictor:
    def __init__(self, delta=2.0):
        self.calls, self.delta = [], delta

    def predict_batch(self, df_list, x_timestamp_list, y_timestamp_list, pred_len, **kw):
        self.calls.append(len(df_list))
        assert len({len(d) for d in df_list}) == 1  # misma longitud en el lote
        assert all(len(y) == pred_len for y in y_timestamp_list)
        return [pd.DataFrame({"close": [d["close"].iloc[-1] + self.delta] * pred_len}) for d in df_list]


@pytest.fixture
def fake(monkeypatch):
    p = FakePredictor()
    monkeypatch.setattr(ks, "_cache", {})
    monkeypatch.setattr(ks, "available", lambda: True)
    monkeypatch.setattr(ks, "_load", lambda size: p)
    return p


def test_batch_scores_and_cache(fake):
    series = {"A/EUR": _candles(), "B/EUR": _candles(80)}
    out = ks.scores(series)
    assert fake.calls == [2]  # un solo lote para todos
    # ATR = 2 (alto-bajo), subida prevista de 2 → 2 / (2·2) = 0.5
    assert out == pytest.approx({"A/EUR": 0.5, "B/EUR": 0.5})
    assert ks.scores(series) == out and fake.calls == [2]  # misma vela: de la caché
    series["A/EUR"] = _candles(101)
    ks.scores(series)
    assert fake.calls == [2, 1]  # solo recalcula el que tiene vela nueva


def test_short_history_and_failures_give_zero(fake, monkeypatch):
    assert ks.scores({"A/EUR": _candles(30)}) == {"A/EUR": 0.0}

    def boom(size):
        raise RuntimeError("sin modelo")
    monkeypatch.setattr(ks, "_load", boom)
    assert ks.scores({"A/EUR": _candles()}) == {"A/EUR": 0.0}
    monkeypatch.setattr(ks, "available", lambda: False)
    assert ks.scores({"A/EUR": _candles()}) == {"A/EUR": 0.0}


def test_score_is_clamped():
    c = _candles()
    assert ks._to_score(c, c[-1][4] + 100) == 1.0
    assert ks._to_score(c, c[-1][4] - 100) == -1.0
