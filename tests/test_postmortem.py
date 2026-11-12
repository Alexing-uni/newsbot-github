"""Autopsia de pérdidas: hechos, menú de mejoras seguras, elección por reglas o Claude y propuesta."""
import json
from contextlib import closing
from datetime import UTC, datetime, timedelta

import pytest

from newsbot import config as config_mod
from newsbot.stocks import postmortem as pm
from newsbot.storage import db

NOW = datetime(2026, 11, 5, 17, 0, tzinfo=UTC)  # 12:00 en Nueva York


def cfg():
    from conftest import ROOT
    c = config_mod.load(ROOT / "config_usa.toml")
    c["general"]["symbols"] = ["NVDA/EUR", "AMD/EUR", "AAPL/EUR"]
    return c


def tick(con, sym, when, price, raw, contrib=None):
    with con:
        con.execute("INSERT INTO ticks(ts, created, symbol, price, signal, action, raw_json, "
                    "contributions_json) VALUES (?,?,?,?,0.3,'BUY',?,?)",
                    (int(when.timestamp()), when.isoformat(), sym, price, json.dumps(raw),
                     json.dumps(contrib or {})))


def seed(con, opened, market=(-0.004, -0.003), entry_signal=0.24):
    with con:
        con.execute("INSERT INTO proposals(created, expires, symbol, side, eur, qty, ref_price, stop_price, "
                    "reason, signal, urgent, status, thought) VALUES (?,?,?,?,?,?,?,?,?,?,0,'executed',?)",
                    (opened.isoformat(), opened.isoformat(), "NVDA/EUR", "buy", 10.0, 0.05, 200.0, 190.0,
                     "señal de compra | Bollinger +0.15, noticias +0.06", entry_signal,
                     "Compro por Bollinger +0.15. Objetivo +2 %."))
        con.execute("INSERT INTO headlines(day, source, title, summary, published) VALUES (?,?,?,?,?)",
                    (opened.date().isoformat(), "reuters", "Nvidia shares slide on export curbs", "",
                     (opened - timedelta(hours=2)).astimezone(UTC).isoformat()))
    raw = {"bollinger": 0.9, "news": 0.4, "intraday": 0.2, "_regime": "bull"}
    tick(con, "NVDA/EUR", opened, 200.0, raw, {"bollinger": 0.15, "news": 0.06})
    tick(con, "NVDA/EUR", NOW, 196.0, {"bollinger": 0.2, "news": -0.3, "intraday": -0.5})
    others = (("AMD/EUR", (100.0, 100 * (1 + market[0]))), ("AAPL/EUR", (50.0, 50 * (1 + market[1]))))
    for s, (a, b) in others:
        tick(con, s, opened, a, {"intraday": 0.1})
        tick(con, s, NOW, b, {"intraday": -0.1})


def test_facts_collect_the_why_of_the_trade(tmp_path):
    c, opened = cfg(), NOW - timedelta(hours=1)
    with closing(db.connect(tmp_path / "n.db")) as con:
        seed(con, opened)
        f = pm.facts(con, c, "NVDA/EUR", opened.isoformat(), 200.0, 196.0, 0.05, 0.03, "stop-loss", NOW)
    assert f["symbol"] == "NVDA" and f["pnl_eur"] == pytest.approx(-0.23) and f["ret_pct"] == -2.0
    assert f["buy_thought"].startswith("Compro por Bollinger") and f["buy_signal"] == 0.24
    assert f["contributions_at_entry"] == {"bollinger": 0.15, "news": 0.06}
    assert f["components_now"]["news"] == -0.3 and f["minutes_after_open"] == 85  # 10:55 en NY
    assert f["headlines_before_entry"] == ["[reuters] Nvidia shares slide on export curbs"]
    assert f["market_median_move_pct"] == pytest.approx(-0.35)


def test_menu_options_are_valid_bounded_overrides():
    c = cfg()
    entry = {"contributions_at_entry": {"bollinger": 0.15, "news": 0.06}}
    opts = {o["kind"]: o for o in pm.menu(c, entry)}
    assert set(opts) == {"threshold_up", "weight_down", "no_buy_open", "market_filter", "min_order_up",
                         "none"}
    for o in opts.values():
        eff = config_mod.apply(c, o["override"])  # todas validan (los pesos siguen sumando 1)
        assert abs(sum(eff["weights"].values()) - 1) < 1e-9
    w = opts["weight_down"]["override"]["weights"]
    assert opts["weight_down"]["comp"] == "bollinger" and w["bollinger"] == pytest.approx(0.10)
    assert opts["threshold_up"]["b"] == pytest.approx(c["strategy"]["buy_threshold"] + 0.03)
    c["strategy"]["buy_threshold"] = 0.44
    assert pm.option(c, "threshold_up") is None  # con tope
    c["strategy"]["min_market_intraday"] = 0.0
    assert pm.option(c, "market_filter") is None


def test_rules_pick_the_cause(tmp_path):
    c, opened = cfg(), NOW - timedelta(hours=1)
    with closing(db.connect(tmp_path / "n.db")) as con:
        seed(con, opened, market=(-0.03, -0.025))  # cae todo el mercado más que el valor/2
        f = pm.facts(con, c, "NVDA/EUR", opened.isoformat(), 200.0, 196.0, 0.05, 0.03, "stop-loss", NOW)
    kind, why = pm.rule_choice(f, pm.menu(c, f))
    assert kind == "market_filter" and "mercado" in why
    f2 = f | {"market_median_move_pct": 0.5, "buy_signal": c["strategy"]["buy_threshold"] + 0.01}
    assert pm.rule_choice(f2, pm.menu(c, f2))[0] == "threshold_up"
    f3 = f2 | {"minutes_after_open": 5}
    assert pm.rule_choice(f3, pm.menu(c, f3))[0] == "no_buy_open"
    f4 = f3 | {"fees_share_of_loss": 0.8}
    assert pm.rule_choice(f4, pm.menu(c, f4))[0] == "min_order_up"


def test_run_saves_a_proposal_the_user_can_accept(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    c, opened = cfg(), NOW - timedelta(hours=1)
    with closing(db.connect(tmp_path / "n.db")) as con:
        seed(con, opened, market=(-0.03, -0.025))
        out = pm.run(con, c, "NVDA/EUR", opened.isoformat(), 200.0, 196.0, 0.05, 0.03, "stop-loss", NOW)
        row = con.execute("SELECT * FROM params_history WHERE id = ?", (out["pid"],)).fetchone()
        saved = json.loads(db.KV(con).get("postmortems"))
    assert out["kind"] == "market_filter" and out["by"] == "reglas" and row["status"] == "proposed"
    assert json.loads(row["params_json"]) == {"strategy": {"min_market_intraday": -0.3}}
    note = json.loads(row["note"])
    assert note["origin"] == "postmortem" and note["symbol"] == "NVDA" and note["based_on"] is None
    assert saved[-1]["pid"] == out["pid"] and "mercado" in out["change"]


def test_claude_choice_is_validated_against_the_menu(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")

    class Msg:
        def __init__(self, text):
            self.content = [type("B", (), {"type": "text", "text": text})()]

    class Client:
        reply = ('{"diagnosis": "Compró en un repunte que se deshizo.", "kind": "no_buy_open", '
                 '"lesson": "Esperar."}')

        def __init__(self, timeout=None):
            self.messages = self

        def create(self, **kw):
            return Msg(Client.reply)

    import anthropic
    monkeypatch.setattr(anthropic, "Anthropic", Client)
    opts = pm.menu(cfg(), {"contributions_at_entry": {"news": 0.1}})
    want = ("no_buy_open", "Compró en un repunte que se deshizo.", "Esperar.")
    assert pm.claude_choice({"x": 1}, opts, "m") == want
    Client.reply = '{"diagnosis": "x", "kind": "vender todo"}'  # fuera del menú: no vale
    assert pm.claude_choice({"x": 1}, opts, "m") is None
