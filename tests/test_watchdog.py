"""Vigilante: arranca si no hay bucle, reinicia si no late, respeta data/stop."""
import time
from contextlib import closing
from pathlib import Path

import pytest

from newsbot.bot import _connect
from newsbot.storage import db
from newsbot.watchdog import check

NOW = 1_800_000_000.0


@pytest.fixture
def calls():
    return {"kill": [], "start": 0, "notify": []}


def run(cfg, calls, pids, beat=None, quiet=False):
    if beat is not None:
        with _connect(cfg) as con:
            db.KV(con).set("loop:heartbeat", str(int(NOW - beat)))

    def start():
        calls["start"] += 1

    return check(cfg, NOW, pids=lambda: pids, kill=calls["kill"].extend, start=start,
                 notify=calls["notify"].append, quiet=quiet)


def test_alive_and_beating_does_nothing(cfg, calls):
    assert run(cfg, calls, [11, 12], beat=60) == "ok"
    assert calls == {"kill": [], "start": 0, "notify": []}


def test_not_running_starts_and_notifies(cfg, calls):
    assert run(cfg, calls, []) == "started"
    assert calls["start"] == 1 and "no estaba en marcha" in calls["notify"][0]


def test_quiet_start_at_login_does_not_notify(cfg, calls):
    assert run(cfg, calls, [], quiet=True) == "started" and calls["notify"] == []


def test_hung_loop_is_killed_and_restarted(cfg, calls):
    data = Path(cfg["general"]["data_dir"])
    data.mkdir(parents=True, exist_ok=True)
    lock = data / ".lock"
    lock.touch()
    assert run(cfg, calls, [11, 12], beat=45 * 60) == "restarted"
    assert calls["kill"] == [11, 12] and calls["start"] == 1 and not lock.exists()
    assert "45 min" in calls["notify"][0]


def test_slow_tick_is_not_a_hang(cfg, calls):
    assert run(cfg, calls, [11], beat=12 * 60) == "ok"  # < 20 min: un tick lento, no un cuelgue


def test_stop_file_means_hands_off(cfg, calls):
    data = Path(cfg["general"]["data_dir"])
    data.mkdir(parents=True, exist_ok=True)
    (data / "stop").touch()
    assert run(cfg, calls, []) == "stopped" and calls["start"] == 0


def test_loop_writes_heartbeat(cfg, monkeypatch):
    from newsbot import bot
    monkeypatch.setattr(bot, "run_once", lambda *a, **k: None)
    monkeypatch.setattr(bot, "learn_step", lambda cfg: None)
    bot._ticks(cfg, 1, lambda s: None, 300)
    with _connect(cfg) as con:
        assert abs(int(db.KV(con).get("loop:heartbeat")) - time.time()) < 60


def test_hung_stocks_thread_restarts_the_process(cfg, calls, tmp_path, monkeypatch):
    from newsbot import watchdog
    bolsa = {"general": {"data_dir": str(tmp_path / "bolsa"), "tick_minutes": 15}}
    (tmp_path / "bolsa").mkdir()
    with closing(db.connect(tmp_path / "bolsa" / "newsbot.db")) as con:
        db.KV(con).set("loop:heartbeat", str(int(NOW - 2 * 3600)))  # el hilo de bolsa lleva 2 h sin latir
    monkeypatch.setattr(watchdog, "extra_cfgs", lambda cfg, path=None: [bolsa])
    monkeypatch.setattr(watchdog, "extra_dirs", lambda cfg, path=None: [tmp_path / "bolsa"])
    assert run(cfg, calls, [11], beat=60) == "restarted"


def test_start_clears_stale_locks(cfg, calls):
    data = Path(cfg["general"]["data_dir"])
    data.mkdir(parents=True, exist_ok=True)
    (data / ".lock").touch()
    assert run(cfg, calls, []) == "started" and not (data / ".lock").exists()
