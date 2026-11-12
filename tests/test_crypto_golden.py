"""Foto fija de la cartera cripto: añadir la de bolsa no puede cambiar nada de lo que hace.

Ejecuta el camino completo en papel (decisión de las 09:05, un tick intradía que opera y otro que
no) y compara con tests/golden/crypto_paper.json: filas guardadas, journal, mensajes de Telegram
y respuestas de los comandos. Para regenerarla (solo si el cambio es intencionado):
GOLDEN_UPDATE=1 pytest tests/test_crypto_golden.py
"""
import json
import os
from datetime import timedelta
from pathlib import Path

from conftest import NOW, FakeExchange, uptrend

from newsbot import bot
from newsbot.bot import _connect, run_once
from newsbot.reporting import telegram

GOLDEN = Path(__file__).parent / "golden" / "crypto_paper.json"
VOLATILE = {"created", "config_hash", "ingested", "archived_at"}


def rows(con, table, order):
    return [{k: v for k, v in dict(r).items() if k not in VOLATILE}
            for r in con.execute(f"SELECT * FROM {table} ORDER BY {order}")]


def snapshot(cfg, monkeypatch) -> dict:
    sent = []
    monkeypatch.setattr(bot.telegram, "send", lambda text, *a, **k: sent.append(text))
    others = {"ETH/EUR": [3000 * 1.02**i for i in range(61)]}
    cfg["general"]["symbols"] = ["BTC/EUR", "ETH/EUR"]
    run_once(cfg, FakeExchange(uptrend(), others=others), NOW)
    run_once(cfg, FakeExchange(uptrend(), others=others), NOW + timedelta(minutes=5), intraday=True)
    falling = list(reversed(uptrend()))
    run_once(cfg, FakeExchange(falling, price=falling[-1] * 0.9, others=others),
             NOW + timedelta(hours=3), intraday=True)
    with _connect(cfg) as con:
        out = {t: rows(con, t, o) for t, o in [
            ("decisions", "day"), ("decisions_archive", "day"), ("orders", "id"),
            ("equity_daily", "day"), ("sentiment_runs", "id"), ("ticks", "id")]}
        for r in out["decisions"] + out["decisions_archive"]:
            r["inputs_json"] = json.loads(r["inputs_json"])
            r["inputs_json"].pop("params_id", None)
        out["commands"] = {c: telegram.handle(con, cfg, c, NOW + timedelta(hours=3, minutes=1))
                           for c in ("/estado", "/senales", "/moneda BTC", "/operaciones",
                                     "/rendimiento", "/ayuda", "/propuestas")}
    out["telegram"] = sent
    out["journal"] = (Path(cfg["general"]["data_dir"]) / "journal.csv").read_text(encoding="utf-8")
    return json.loads(json.dumps(out, ensure_ascii=False, default=str))


def test_crypto_paper_path_is_unchanged(cfg, monkeypatch):
    got = snapshot(cfg, monkeypatch)
    if os.environ.get("GOLDEN_UPDATE") or not GOLDEN.exists():
        GOLDEN.parent.mkdir(exist_ok=True)
        GOLDEN.write_text(json.dumps(got, ensure_ascii=False, indent=1), encoding="utf-8")
    want = json.loads(GOLDEN.read_text(encoding="utf-8"))
    for key in want:
        assert got[key] == want[key], f"cambió la parte '{key}' de la cartera cripto"
    assert set(got) == set(want)
