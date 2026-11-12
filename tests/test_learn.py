"""Aprendizaje a partir de los ticks: etiquetas a h vista, IC/acierto y propuestas semanales."""
import json
import math
import random
import shutil
from datetime import UTC, datetime, timedelta

import pytest

from newsbot import config as config_mod
from newsbot.research import learn
from newsbot.storage import db

T0 = datetime(2026, 8, 1, tzinfo=UTC)
NOW = datetime(2026, 9, 25, tzinfo=UTC)
H4 = timedelta(hours=4)


@pytest.fixture(scope="module")
def template(tmp_path_factory):
    """Las migraciones cuestan ~0,3 s en Windows: se aplican una vez y se copia el fichero."""
    path = tmp_path_factory.mktemp("learn") / "template.db"
    db.connect(path).close()
    return path


@pytest.fixture
def con(tmp_path, template):
    shutil.copy(template, tmp_path / "x.db")
    c = db.connect(tmp_path / "x.db")
    c.execute("PRAGMA synchronous = OFF")  # tests rápidos: save_ticks hace un commit por tick
    c.execute("PRAGMA journal_mode = MEMORY")
    yield c
    c.close()


def row(symbol, price, raw=None, signal=0.0):
    return {"symbol": symbol, "price": price, "signal": signal, "action": "HOLD", "reason": "",
            "held": False, "raw": raw or {}, "contributions": {}}


def tick(con, now, symbol, price, raw=None):
    db.save_ticks(con, now, [row(symbol, price, raw)])


def synth(con, n, symbols=("BTC/EUR", "ETH/EUR"), step=H4, seed=1):
    """`n` ticks por moneda cada 4 h. news = signo del retorno siguiente (predice), votes = el
    contrario, trend = ruido, fng = constante 0, signal = la mitad de news."""
    rnd = random.Random(seed)
    prices = dict.fromkeys(symbols, 100.0)
    for i in range(n):
        rows = []
        for sym in symbols:
            ret = rnd.choice((-1, 1)) * rnd.uniform(0.002, 0.02)
            nxt = math.copysign(1, ret)
            raw = {"news": nxt, "votes": -nxt, "trend": rnd.uniform(-1, 1), "fng": 0.0,
                   "intraday": rnd.uniform(-1, 1)}
            rows.append(row(sym, prices[sym], raw, signal=nxt / 2))
            prices[sym] *= math.exp(ret)
        db.save_ticks(con, T0 + i * step, rows)


# --- estadística --------------------------------------------------------------------------------
def test_ranks_spearman_wilson():
    assert learn._ranks([3, 1, 3, 2]) == [3.5, 1, 3.5, 2]
    assert learn.spearman(list(range(9)), list(range(9))) is None  # n < 10
    assert learn.spearman(list(range(12)), [x**3 for x in range(12)]) == pytest.approx(1)
    assert learn.spearman([1.0] * 12, list(range(12))) is None  # sin varianza
    lo, hi = learn.wilson(50, 100)
    assert lo < 0.5 < hi and hi - lo == pytest.approx(0.19, abs=0.01)
    assert learn.fisher_ci(0.1, 3) is None


# --- muestreo -----------------------------------------------------------------------------------
def test_non_overlapping_and_gaps(con, cfg):
    for m, p in ((0, 100), (30, 101), (60, 102), (90, 103), (120, 104), (213, 105)):
        tick(con, T0 + timedelta(minutes=m), "BTC/EUR", p, {"news": 0.5})
    # 0→60 y 60→120 valen; 120→(213 > 180+15 min de tolerancia) es hueco; 213 no tiene futuro
    ev = learn.evaluate(con, cfg, T0 + timedelta(days=1), "1h")
    assert ev["n"] == 2 and ev["n_symbols"] == 1 and ev["n_eff"] == 2
    assert ev["components"]["news"]["n"] == 2 and ev["components"]["rsi"]["n"] == 0
    # el futuro de 60 (t=120) aún no ha llegado: solo cuenta la primera muestra
    assert learn.evaluate(con, cfg, T0 + timedelta(minutes=110), "1h")["n"] == 1
    assert learn.evaluate(con, cfg, T0 + timedelta(days=1), "4h")["n"] == 0
    with pytest.raises(ValueError):
        learn.evaluate(con, cfg, NOW, "5m")


def test_empty_db(con, cfg):
    ev = learn.evaluate(con, cfg, NOW)
    assert ev["n"] == 0 and ev["n_eff"] == 0 and ev["components"]["news"]["ic"] is None
    assert "Aprendizaje" in learn.report(con, cfg, NOW)


def test_ic_hit_rate_and_pooling(con, cfg):
    synth(con, 151, symbols=("BTC/EUR", "ETH/EUR", "SOL/EUR", "XRP/EUR", "ADA/EUR"))
    ev = learn.evaluate(con, cfg, NOW)
    assert ev["n"] == 750 and ev["n_symbols"] == 5 and ev["n_eff"] == pytest.approx(300)
    c = ev["components"]
    assert c["news"]["ic"] > 0.5 and c["news"]["ic_ci"][0] > 0
    assert c["votes"]["ic_ci"][1] < 0
    assert c["trend"]["ic_ci"][0] < 0 < c["trend"]["ic_ci"][1]  # ruido: el IC incluye el 0
    assert c["fng"]["ic"] is None and c["fng"]["hit"] is None  # constante (y por debajo de min_abs)
    assert c["news"]["hit"] == 1 and c["news"]["hit_ci"][0] > 0.95
    assert c["news"]["ret_up"] > 0 > c["news"]["ret_down"]
    assert ev["fused"]["ic"] > 0.5 and ev["fused"]["n_up"] + ev["fused"]["n_down"] == 750


def test_report(con, cfg):
    synth(con, 21)
    txt = learn.report(con, cfg, NOW)
    assert "Aprendizaje" in txt and "horizonte 4h" in txt and "<pre>" in txt
    assert "Aún no hay datos suficientes para cambiar pesos (n_eff 40/400). Solo se mide." in txt
    assert "Faltan ≈30 días" in txt and "rsi·sombra" in txt and len(txt) < 3500
    assert "nunca" in learn.report(con, cfg, NOW, "24h")
    assert "desconocido" in learn.report(con, cfg, NOW, "7h")


# --- propuestas ---------------------------------------------------------------------------------
def test_not_due_does_not_evaluate(con, cfg, monkeypatch):
    db.KV(con).set("learn:last", (NOW - timedelta(days=3)).date().isoformat())

    def boom(*a, **k):
        raise AssertionError("no debe evaluar")

    monkeypatch.setattr(learn, "evaluate", boom)
    assert learn.maybe_propose(con, cfg, NOW, send=boom) is None
    assert learn.maybe_propose(con, cfg | {"learn": {"enabled": False}}, NOW, send=boom) is None


def test_insufficient_data(con, cfg):
    synth(con, 30)
    assert learn.maybe_propose(con, cfg, NOW, send=print) is None
    assert db.KV(con).get("learn:last") == NOW.date().isoformat()
    assert con.execute("SELECT count(*) FROM params_history").fetchone()[0] == 0


def test_propose_live_merges_override(con, cfg):
    cfg["general"]["mode"] = "live"
    cfg["learn"]["auto_apply_live"] = False  # con aprobación manual
    with con:
        con.execute("INSERT INTO params_history(created, params_json, status) VALUES "
                    "('x', ?, 'applied')", (json.dumps({"strategy": {"buy_threshold": 0.3}}),))
    synth(con, 260)
    sent = []
    msg = learn.maybe_propose(con, cfg, NOW, send=sent.append)
    assert sent == [msg] and "/aprobar 2" in msg and "/rechazar 2" in msg and "news" in msg
    row = con.execute("SELECT * FROM params_history WHERE id = 2").fetchone()
    assert row["status"] == "proposed" and json.loads(row["note"])["origin"] == "learn"
    override = json.loads(row["params_json"])
    assert set(override) == {"weights"}  # solo su sección: active_params acumula las aplicadas
    w = override["weights"]
    assert set(w) == config_mod.CORE_WEIGHTS | config_mod.SHADOW_WEIGHTS  # vector completo
    assert abs(sum(w.values()) - 1) < 1e-9 and min(w.values()) >= 0
    assert all(abs(w[k] - cfg["weights"].get(k, 0)) <= 0.05 + 1e-9 for k in w)
    assert w["news"] > cfg["weights"]["news"] and w["votes"] < cfg["weights"]["votes"]
    with con:
        con.execute("UPDATE params_history SET status = 'applied' WHERE id = 2")
    eff = config_mod.apply(cfg, db.active_params(con, "live")[1])
    assert eff["strategy"]["buy_threshold"] == 0.3 and eff["weights"] == w  # lo anterior no se pierde
    with con:
        con.execute("UPDATE params_history SET status = 'proposed' WHERE id = 2")
    assert learn.pending(con) == 2 and "Propuesta pendiente #2" in learn.report(con, cfg, NOW)
    assert db.active_params(con)[0] == 1  # en live no se aplica sola
    assert learn.maybe_propose(con, cfg, NOW, send=sent.append) is None  # hasta dentro de 7 días


def test_propose_paper_auto_applies(con, cfg):
    cfg["adapt"]["auto_apply_in_paper"] = True
    synth(con, 260)
    msg = learn.maybe_propose(con, cfg, NOW, send=lambda m: None)
    assert "aplicados solos #1" in msg and "/aprobar" not in msg and "/deshacer 1" in msg
    pid, override = db.active_params(con)
    assert pid == 1 and set(override) == {"weights"}


def test_new_weights_caps_and_sums():
    cur = {"news": 0.4, "trend": 0.3, "intraday": 0.15, "fng": 0.1, "votes": 0.05}
    comps = {k: {"ic_ci": None} for k in learn.COMPONENTS}
    comps["momentum"] = {"ic_ci": (0.1, 0.3)}
    comps["fng"] = {"ic_ci": (-0.3, -0.1)}
    s = learn.settings({})
    w = learn.new_weights(cur, {"n_eff": 1e9, "components": comps}, s)
    assert sum(w.values()) == pytest.approx(1, abs=1e-12) and w["momentum"] > 0.04
    assert w["fng"] == pytest.approx(0.05, abs=0.003) and w["rsi"] == 0.0
    assert all(abs(w[k] - cur.get(k, 0)) <= 0.05 + 1e-9 for k in w)
