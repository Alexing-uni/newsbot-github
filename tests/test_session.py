"""Horario de mercado (features/session.py) y contrato de carteras (portfolio.py)."""
from datetime import UTC, datetime

import pytest

from newsbot import portfolio
from newsbot.features import session

S = {"session": {"tz": "Europe/Madrid", "days": [0, 1, 2, 3, 4], "open": "09:20", "close": "17:30"}}


@pytest.mark.parametrize("utc,expected", [
    (datetime(2026, 11, 2, 8, 19, tzinfo=UTC), False),   # lunes 09:19 en Madrid
    (datetime(2026, 11, 2, 8, 20, tzinfo=UTC), True),    # 09:20
    (datetime(2026, 11, 2, 16, 29, tzinfo=UTC), True),   # 17:29
    (datetime(2026, 11, 2, 16, 30, tzinfo=UTC), False),  # 17:30: cerrado
    (datetime(2026, 11, 7, 10, 0, tzinfo=UTC), False),   # sábado
    (datetime(2026, 7, 6, 7, 20, tzinfo=UTC), True),     # julio (UTC+2): 09:20 en Madrid
])
def test_is_open(utc, expected):
    assert session.is_open(S, utc) is expected
    assert session.is_open({}, utc) is True  # sin [session]: cripto, siempre abierto


def test_next_open_and_fraction():
    friday_night = datetime(2026, 11, 6, 20, 0, tzinfo=UTC)
    assert session.next_open(S, friday_night).isoformat() == "2026-11-09T09:20:00+01:00"
    assert session.next_open(S, datetime(2026, 11, 2, 9, 0, tzinfo=UTC)) is None  # abierto
    assert session.fraction(S) == pytest.approx((8 + 10 / 60) * 5 / 168)
    assert session.fraction({}) == 1.0


def test_validate_rejects_bad_sessions():
    session.validate(S["session"])
    with pytest.raises(ValueError):
        session.validate(S["session"] | {"days": [7]})
    with pytest.raises(ValueError):
        session.validate(S["session"] | {"open": "18:00"})


def test_portfolio_context_and_commands():
    bolsa = {"general": {"portfolio": "bolsa", "label": "📈 Bolsa"}}
    cripto = {"general": {}}
    assert portfolio.cmd("aprobar", 3) == "/aprobar 3"
    with portfolio.active(bolsa):
        assert portfolio.cmd("aprobar", 3) == "/aprobar bolsa 3"
        assert portfolio.LABEL.get() == "📈 Bolsa"
    with portfolio.active(cripto):
        assert portfolio.cmd("deshacer", 1) == "/deshacer 1" and portfolio.LABEL.get() == ""
    assert portfolio.CURRENT.get() == "" and portfolio.word(cripto) == "cripto"
