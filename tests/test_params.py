"""Overrides de parámetros (reoptimize + learn), su aprobación y las protecciones del tick."""
import json
from datetime import UTC, datetime, timedelta

from conftest import NOW, FakeExchange, uptrend

from newsbot import bot
from newsbot.bot import _connect, run_once
from newsbot.reporting import telegram
from newsbot.storage import db

W = {"news": 0.35, "trend": 0.30, "intraday": 0.15, "fng": 0.10, "votes": 0.05, "rsi": 0.05,
     "bollinger": 0.0, "momentum": 0.0, "volume": 0.0}


def add(con, params, status="applied", note=None):
    with con:
        return con.execute("INSERT INTO params_history(created, params_json, status, note) "
                           "VALUES ('x', ?, ?, ?)", (json.dumps(params), status,
                                                     json.dumps(note or {}))).lastrowid


def test_applied_overrides_accumulate_and_live_skips_auto(cfg):
    with _connect(cfg) as con:
        add(con, {"weights": W}, note={"origin": "learn"})
        add(con, {"strategy": {"buy_threshold": 0.3}}, note={"origin": "reoptimize"})
        add(con, {"strategy": {"sell_threshold": -0.2}}, note={"auto": True})  # papel, sola
        pid, override = db.active_params(con)
        assert pid == 3 and override == {"weights": W, "strategy": {
            "buy_threshold": 0.3, "sell_threshold": -0.2}}
        assert db.active_params(con, "live") == (2, {"weights": W, "strategy": {"buy_threshold": 0.3}})


def test_invalid_override_falls_back_to_config_and_alerts_once(cfg, monkeypatch):
    sent = []
    monkeypatch.setattr(bot.telegram, "send", lambda text: sent.append(text))
    with _connect(cfg) as con:
        add(con, {"weights": {"news": 0.9}})  # ya no suma 1
    d = run_once(cfg, FakeExchange(uptrend()), NOW)
    run_once(cfg, FakeExchange(uptrend()), NOW + timedelta(minutes=5), intraday=True)
    assert d is not None  # el bot sigue decidiendo (y los stops actuando) con config.toml
    assert sum("ya no son válidos" in s for s in sent) == 1


def test_stale_proposal_is_refused_and_approval_marks_manual(cfg):
    cfg["general"]["mode"] = "live"
    with _connect(cfg) as con:
        add(con, {"strategy": {"buy_threshold": 0.3}})
        stale = add(con, {"weights": W}, "proposed", {"origin": "learn", "based_on": None})
        fresh = add(con, {"weights": W}, "proposed", {"origin": "learn", "based_on": 1, "auto": True})
        assert "obsoleta" in telegram.handle(con, cfg, f"/aprobar {stale}")
        assert "aplicada" in telegram.handle(con, cfg, f"/aprobar {fresh}")
        note = json.loads(con.execute("SELECT note FROM params_history WHERE id = ?",
                                      (fresh,)).fetchone()[0])
        assert note["auto"] is False  # aprobada a mano: cuenta en live
        assert db.active_params(con, "live") == (fresh, {"strategy": {"buy_threshold": 0.3},
                                                         "weights": W})


def test_old_state_changing_commands_are_not_executed(cfg):
    env = {"TELEGRAM_BOT_TOKEN": "T", "TELEGRAM_CHAT_ID": "42"}
    old = int((datetime.now(UTC) - timedelta(minutes=30)).timestamp())
    new = int(datetime.now(UTC).timestamp())
    updates = {"result": [
        {"update_id": 1, "message": {"chat": {"id": 42}, "date": old, "text": "/pausa"}},
        {"update_id": 2, "message": {"chat": {"id": 42}, "date": old, "text": "/estado"}},
        {"update_id": 3, "message": {"chat": {"id": 42}, "date": new, "text": "/pause"}},
    ]}
    posts = []
    with _connect(cfg) as con:
        telegram.poll(con, cfg, env, get=lambda u: updates, post=lambda u, p: posts.append(p))
        assert "más de 5 minutos" in posts[0]["text"] and "newsbot" in posts[1]["text"]
        assert "Pausado" in posts[2]["text"] and db.KV(con).get("paused") == "1"


def test_pause_received_during_tick_blocks_the_order(cfg, monkeypatch):
    real = bot.decide

    def decide_then_pause(**kw):  # /pausa llega mientras el tick calcula
        with _connect(cfg) as con:
            db.KV(con).set("paused", "1")
        return real(**kw)

    monkeypatch.setattr(bot, "decide", decide_then_pause)
    assert run_once(cfg, FakeExchange(uptrend()), NOW) is None
    with _connect(cfg) as con:
        assert con.execute("SELECT count(*) FROM orders").fetchone()[0] == 0
        assert not db.has_decision(con, NOW.date().isoformat())


def test_learn_step_prunes_old_ticks_once_a_day(cfg):
    with _connect(cfg) as con:
        row = {"symbol": "BTC/EUR", "price": 1.0, "signal": 0.0, "action": "HOLD", "reason": "",
               "held": False, "raw": {}, "contributions": {}}
        db.save_ticks(con, NOW - timedelta(days=90), [row])
        db.save_ticks(con, NOW - timedelta(days=1), [row])
    bot.learn_step(cfg, NOW)
    with _connect(cfg) as con:
        assert con.execute("SELECT count(*) FROM ticks").fetchone()[0] == 1
        assert db.KV(con).get("ticks:pruned") == NOW.date().isoformat()


def test_auto_applied_in_live_counts_in_live_but_paper_ones_do_not(cfg):
    with _connect(cfg) as con:
        add(con, {"strategy": {"buy_threshold": 0.3}}, note={"auto": True, "mode": "live"})
        add(con, {"strategy": {"sell_threshold": -0.2}}, note={"auto": True, "mode": "paper"})
        add(con, {"strategy": {"buy_threshold": 0.35}}, note={"auto": True})  # antiguas: papel
        assert db.active_params(con, "live") == (1, {"strategy": {"buy_threshold": 0.3}})


def test_learn_auto_applies_in_live_when_enabled(cfg):
    from test_learn import NOW as LEARN_NOW
    from test_learn import synth

    from newsbot.research import learn
    cfg["general"]["mode"] = "live"
    cfg["learn"]["auto_apply_live"] = True
    with _connect(cfg) as con:
        synth(con, 260)
        msg = learn.maybe_propose(con, cfg, LEARN_NOW, send=lambda m: None)
        assert "aplicados solos #1" in msg and "/deshacer 1" in msg
        pid, override = db.active_params(con, "live")
        assert pid == 1 and set(override) == {"weights"}  # rige en live sin /aprobar


def test_deshacer_reverts_an_applied_change(cfg):
    with _connect(cfg) as con:
        add(con, {"strategy": {"buy_threshold": 0.3}}, note={"auto": True, "mode": "paper"})
        two = add(con, {"strategy": {"sell_threshold": -0.2}})
        assert "Cambios en vigor" in telegram.handle(con, cfg, "/propuestas")
        out = telegram.handle(con, cfg, f"/deshacer {two}")
        assert "revertido" in out and "#1" in out
        assert db.active_params(con) == (1, {"strategy": {"buy_threshold": 0.3}})
        assert "No hay ningún cambio aplicado" in telegram.handle(con, cfg, f"/undo {two}")
        assert telegram.handle(con, cfg, "/deshacer x").startswith("Uso")


def test_reoptimize_refuses_thresholds_outside_hard_bounds(cfg, monkeypatch):
    from contextlib import closing
    from pathlib import Path

    from conftest import wave

    from newsbot.research import reoptimize
    cfg["general"]["mode"], cfg["adapt"]["auto_apply_live"] = "live", True
    monkeypatch.setattr(reoptimize, "propose", lambda c, f: {
        "params": {"strategy": {"buy_threshold": 0.10, "sell_threshold": -0.15}}, "accepted": True,
        "reasons": [], "oos_candidate": 1.5, "oos_current": 1.0, "mc": None})
    monkeypatch.setattr(reoptimize.telegram, "send", lambda m: None)
    data = Path(cfg["general"]["data_dir"])
    data.mkdir(parents=True, exist_ok=True)
    with closing(db.connect(data / "newsbot.db")) as con:
        msg = reoptimize.run(con, cfg, FakeExchange(wave(400)), NOW)
        assert con.execute("SELECT status FROM params_history").fetchone()[0] == "rejected"
    assert "fuera de los límites" in msg
