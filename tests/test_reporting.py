"""Telegram, informe semanal y exportación fiscal FIFO."""
import csv
from contextlib import closing
from pathlib import Path

import pytest
from conftest import NOW, FakeExchange, uptrend

from newsbot.bot import run_once
from newsbot.reporting import report, tax, telegram
from newsbot.storage import db

ENV = {"TELEGRAM_BOT_TOKEN": "T0K", "TELEGRAM_CHAT_ID": "42"}


def dbcon(cfg):
    data = Path(cfg["general"]["data_dir"])
    data.mkdir(parents=True, exist_ok=True)
    return closing(db.connect(data / "newsbot.db"))


# --- Telegram ---------------------------------------------------------------------------------
def test_send_needs_both_env_vars():
    sent = []
    assert telegram.send("hola", {}, post=lambda u, p: sent.append(p)) is False and not sent
    assert telegram.send("hola", ENV, post=lambda u, p: sent.append((u, p))) is True
    assert sent[0][0] == "https://api.telegram.org/botT0K/sendMessage"
    assert sent[0][1] == {"chat_id": "42", "text": "hola"}


def test_send_failure_does_not_raise():
    def boom(u, p):
        raise OSError("down")

    assert telegram.send("x", ENV, post=boom) is False


def test_commands(cfg):
    run_once(cfg, FakeExchange(uptrend()), NOW)
    with dbcon(cfg) as con:
        kv = db.KV(con)
        assert "Última decisión 2026-11-02: BUY" in telegram.handle(con, cfg, "/status")
        telegram.handle(con, cfg, "/pause")
        assert kv.get("paused") == "1"
        kv.set("kill_switch", "1")
        assert "CONFIRMAR" in telegram.handle(con, cfg, "/resume")
        assert kv.get("paused") == "1"  # sin confirmación no hace nada
        telegram.handle(con, cfg, "/resume CONFIRMAR")
        assert kv.get("paused") is None and kv.get("kill_switch") is None
        assert "Informe semanal" in telegram.handle(con, cfg, "/report")
        assert "Comandos" in telegram.handle(con, cfg, "/hola")


def test_approve_applies_only_proposed(cfg):
    with dbcon(cfg) as con:
        with con:
            con.execute("INSERT INTO params_history(created, params_json, status) "
                        "VALUES ('x', '{}', 'proposed')")
        assert "aplicada" in telegram.handle(con, cfg, "/approve 1")
        assert db.active_params(con)[0] == 1
        assert "No hay" in telegram.handle(con, cfg, "/approve 1")


def test_poll_only_obeys_own_chat_and_advances_offset(cfg):
    updates = {"result": [
        {"update_id": 7, "message": {"chat": {"id": 999}, "text": "/pause"}},
        {"update_id": 8, "message": {"chat": {"id": 42}, "text": "/status"}},
    ]}
    urls, sent = [], []
    with dbcon(cfg) as con:
        n = telegram.poll(con, cfg, ENV, get=lambda u: urls.append(u) or updates,
                          post=lambda u, p: sent.append(p))
        assert n == 1 and db.KV(con).get("paused") is None
        assert db.KV(con).get("tg:offset") == "9" and urls[0].endswith("offset=0&timeout=0")
        assert len(sent) == 1
        assert telegram.poll(con, cfg, {}) == 0


def test_daily_message_and_weekly_report(cfg):
    d = run_once(cfg, FakeExchange(uptrend()), NOW)
    text = report.daily("2026-11-02", "paper", "BTC/EUR", d, 90_000.0, 10.0, 10.0, [], 37,
                        [("BTC/EUR", d)], None)
    assert "BUY" in text and "alternative.me" in text and "(sin titulares)" in text
    with dbcon(cfg) as con:
        assert "Órdenes: 0" in report.weekly(con, 10.0)  # la compra es del primer día
    with closing(db.connect(Path(cfg["general"]["data_dir"]) / "empty.db")) as con:
        assert "todavía no hay datos" in report.weekly(con, 10.0)


# --- fiscal -----------------------------------------------------------------------------------
def test_fifo_matches_oldest_lots_and_includes_fees():
    orders = [
        {"day": "2026-01-01", "side": "buy", "qty": 1.0, "eur": 100.0, "fee": 1.0},
        {"day": "2026-02-01", "side": "buy", "qty": 1.0, "eur": 200.0, "fee": 2.0},
        {"day": "2026-03-01", "side": "sell", "qty": 1.5, "eur": 450.0, "fee": 3.0},
    ]
    rows = tax.fifo(orders)
    assert [r["fecha_adquisicion"] for r in rows] == ["2026-01-01", "2026-02-01"]
    assert rows[0]["valor_adquisicion_eur"] == 101.0
    assert rows[0]["valor_transmision_eur"] == pytest.approx(298.0)  # (450 - 3) / 1,5
    assert rows[1]["cantidad"] == 0.5 and rows[1]["valor_adquisicion_eur"] == 101.0
    assert sum(r["ganancia_perdida_eur"] for r in rows) == pytest.approx(447 - 101 - 101)


def test_fifo_sell_without_buy_raises():
    with pytest.raises(ValueError, match="sin compras"):
        tax.fifo([{"day": "2026-01-01", "side": "sell", "qty": 1, "eur": 1, "fee": 0}])


def test_tax_export_paper_csv(cfg):
    run_once(cfg, FakeExchange(uptrend()), NOW)
    falling = uptrend() + [50_000 * 1.01**60 * 0.9]
    run_once(cfg, FakeExchange(falling), NOW.replace(day=3))
    out = Path(cfg["general"]["data_dir"]) / "fiscal.csv"
    with dbcon(cfg) as con:
        rows = tax.export(con, 2026, "paper", out)
        assert tax.export(con, 2026, "live", out) == []
        tax.export(con, 2026, "paper", out)
    assert len(rows) == 1 and rows[0]["ganancia_perdida_eur"] < 0  # stop-loss: pérdida
    with out.open(encoding="utf-8") as f:
        assert list(csv.DictReader(f))[0]["fecha_transmision"] == "2026-11-03"
