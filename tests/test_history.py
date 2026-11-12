"""Historial de eventos (estudio de elecciones) y su uso en el prompt de eventos."""
import json
from datetime import UTC, datetime

from newsbot.stocks import events, history


def chart(closes_by_day):
    days = sorted(closes_by_day)
    ts = [int(datetime(d.year, d.month, d.day, 8, tzinfo=UTC).timestamp()) for d in days]
    c = [closes_by_day[d] for d in days]
    return {"chart": {"result": [{"meta": {"currency": "EUR", "exchangeTimezoneName": "Europe/Berlin"},
                                  "timestamp": ts, "indicators": {"quote": [
                                      {"open": c, "high": c, "low": c, "close": c,
                                       "volume": [1] * len(c)}]}}]}}


def series(start, jump):
    """Cierres diarios de 2016 a 2024 que saltan `jump` el día siguiente a cada elección."""
    from datetime import timedelta
    out, d, px = {}, history.EVENTS["elecciones EE. UU."]["presidenciales 2016"].replace(day=1), start
    elections = set(history.EVENTS["elecciones EE. UU."].values())
    while d.year < 2025:
        if d.weekday() < 5:
            out[d] = px
            if d in elections:
                px *= 1 + jump
        d += timedelta(days=1)
    return out


def test_study_measures_reaction_vs_benchmark(cfg, tmp_path):
    bolsa = {"general": {"data_dir": str(tmp_path), "timezone": "Europe/Madrid"},
             "market": {"RHM": {"ticker": "RHM.DE"}, "SP500": {"ticker": "SXR8.DE"}}}
    data = {"RHM.DE": chart(series(100.0, 0.05)), "SXR8.DE": chart(series(100.0, 0.01))}
    out = history.study(bolsa, fetch=lambda url: data[url.split("/chart/")[1].split("?")[0]])
    rhm = out["elecciones EE. UU."]["RHM"]
    assert len(rhm) == 5 and all(abs(r[2] - 0.04) < 1e-3 for r in rhm.values())
    history.save(bolsa, out)
    line = history.prompt_lines(bolsa)
    assert "RHM +4.0% (5/5 por encima)" in line and "SP500" not in line


def test_events_prompt_includes_history(tmp_path):
    cfg = {"general": {"data_dir": str(tmp_path), "timezone": "Europe/Madrid", "symbols": ["RHM/EUR"]},
           "market": {"RHM": {"name": "Rheinmetall"}}, "events": {"lookahead_days": 30, "pre_days": 7,
                                                                  "post_days": 3}, "news": {"assets": {}}}
    (tmp_path / history.FILE).write_text(json.dumps({"events": {"elecciones EE. UU.": {
        "RHM": {"a": [0.05, 0.06, 0.03], "b": [0.02, 0.0, 0.01], "c": [0.01, 0.02, 0.02]}}}}),
        encoding="utf-8")
    prompt = events._prompt(cfg, datetime(2026, 11, 3, 9, tzinfo=UTC), [], [])
    assert "Reacciones históricas" in prompt and "RHM +2.0% (3/3 por encima)" in prompt
