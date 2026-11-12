"""Eventos con fecha (newsbot/stocks/events.py): Claude simulado, sin red."""
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from conftest import NOW, ROOT

from newsbot import config as config_mod
from newsbot.news import ingest
from newsbot.news.rss import Headline
from newsbot.stocks import events
from newsbot.storage import db

MAD = ZoneInfo("Europe/Madrid")
MODEL = "claude-haiku-5-5"


class FakeClient:
    """Devuelve los textos en orden y guarda los argumentos de cada llamada."""

    def __init__(self, *texts, stop="end_turn", error=None):
        self.texts, self.stop, self.error, self.calls = list(texts), stop, error, []
        self.messages = self

    def create(self, **kw):
        self.calls.append(kw)
        if self.error:
            raise self.error
        return SimpleNamespace(stop_reason=self.stop, usage=None,
                               content=[SimpleNamespace(type="text", text=self.texts.pop(0))])

    def prompt(self, i=-1):
        return self.calls[i]["messages"][0]["content"]


@pytest.fixture
def bolsa(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setattr(events.anthropic, "Anthropic", _no_network)
    return {"general": {"symbols": ["TTWO/EUR", "WMT/EUR", "NVDA/EUR"], "timezone": "Europe/Madrid"},
            "market": {"TTWO": {"name": "Take-Two Interactive (GTA)"}, "WMT": {"name": "Walmart"},
                       "NVDA": {"name": "Nvidia (AI chips)"}},
            "news": {"assets": {"TTWO": ["take-two", "rockstar", "gta vi"], "WMT": ["walmart"],
                                "NVDA": ["nvidia"]}},
            "sentiment": {"llm_model": MODEL},
            "events": {"enabled": True, "refresh_hours": 4, "lookahead_days": 30, "pre_days": 7,
                       "post_days": 3, "llm_max_calls_day": 8}}


@pytest.fixture
def con():
    c = db.connect(":memory:")
    yield c
    c.close()


def _no_network():
    raise AssertionError("los tests no crean clientes reales de Anthropic")


def hl(title, hours=1.0, summary="", source="reuters"):
    return Headline(title, summary, source, NOW.astimezone(UTC) - timedelta(hours=hours))


def answer(*evs, obsolete=()):
    return json.dumps({"events": list(evs), "obsolete": list(obsolete)})


def ev(code="TTWO", date="2026-11-19", kind="game_release", effect="up", confidence=0.8, **kw):
    return {"code": code, "kind": kind, "title": kw.pop("title", "Lanzamiento de GTA VI"), "date": date,
            "effect": effect, "confidence": confidence, "why": kw.pop("why", "fecha confirmada"), **kw}


def at(day, hour=10, month=11):
    return datetime(2026, month, day, hour, 0, tzinfo=MAD)


def put(con, code, day, effect="up", conf=1.0, kind="product_launch", title="t", why=""):
    with con:
        return con.execute("INSERT INTO events(code, kind, title, date, effect, confidence, why, source, "
                           "created) VALUES (?,?,?,?,?,?,?,?,?)",
                           (code, kind, title, day, effect, conf, why, "test", NOW.isoformat())).lastrowid


def rows(con):
    return [dict(r) for r in con.execute("SELECT * FROM events ORDER BY date, code, kind")]


HS = [hl("Rockstar confirms GTA VI release date: November 19"), hl("Nvidia sets earnings date", 5)]


# --- refresh ------------------------------------------------------------------------------------
def test_refresh_parses_the_answer_and_saves_events(bolsa, con):
    raw = "Aquí tienes:\n```json\n" + answer(
        ev(), ev("WMT", confidence=0.3, why="ventas de consolas y del juego"),
        {"code": "NVDA", "kind": "earnings", "title": "Resultados del 3T", "date": "2026-11-18",
         "confidence": 0.7, "why": "presenta resultados"}) + "\n```"
    client = FakeClient(raw)
    assert events.refresh(con, bolsa, NOW, HS, client=client) == 3
    got = {(r["code"], r["kind"]): r for r in rows(con)}
    assert got["TTWO", "game_release"]["effect"] == "up" and got["TTWO", "game_release"]["confidence"] == 0.8
    assert got["NVDA", "earnings"]["effect"] == "mixed"  # resultados sin efecto: mixed por defecto
    assert {r["source"] for r in got.values()} == {f"llm:{MODEL}"}
    kw = client.calls[0]
    assert (kw["model"], kw["max_tokens"], kw["output_config"]) == (MODEL, 4096, {"effort": "low"})
    assert "temperature" not in kw
    p = client.prompt()
    assert "Hoy es lunes 2026-11-02" in p and "el 2026-12-02 (30 días)" in p
    assert "- TTWO: Take-Two Interactive (GTA)" in p and "- NVDA: Nvidia (AI chips)" in p
    assert "- [lun 2026-11-02] [reuters] Rockstar confirms GTA VI release date: November 19" in p
    assert "compra el rumor, vende la noticia" in p and "Eventos ya registrados" in p and "ninguno" in p
    kv = db.KV(con)
    assert kv.get("events:calls:2026-11-02") == "1" and kv.get("events:last") == NOW.isoformat()


def test_invalid_json_is_retried_once(bolsa, con):
    client = FakeClient("lo siento, no tengo nada", answer(ev()))
    assert events.refresh(con, bolsa, NOW, HS, client=client) == 1
    assert len(client.calls) == 2 and client.prompt(1).endswith("Tu respuesta anterior no era JSON válido.")
    assert db.KV(con).get("events:calls:2026-11-02") == "2"  # el reintento también cuenta


def test_invalid_json_twice_gives_zero(bolsa, con):
    client = FakeClient('{"events": [', "nada")
    assert events.refresh(con, bolsa, NOW, HS, client=client) == 0
    assert len(client.calls) == 2 and rows(con) == []


def test_no_retry_without_budget(bolsa, con):
    db.KV(con).set("events:calls:2026-11-02", "7")
    client = FakeClient("no es JSON", answer(ev()))
    assert events.refresh(con, bolsa, NOW, HS, client=client) == 0
    assert len(client.calls) == 1 and db.KV(con).get("events:calls:2026-11-02") == "8"


def test_unknown_codes_and_impossible_or_out_of_range_dates_are_dropped(bolsa, con):
    client = FakeClient(answer(
        ev("XXXX"),                                   # fuera del universo
        ev(date="2026-11-01"),                        # ya pasó
        ev(date="2026-02-30"),                        # no existe
        ev(date="2026-12-03"),                        # más allá de lookahead_days
        ev(confidence="alta"),                        # confianza no numérica
        {"code": "TTWO", "kind": "keynote", "title": "sin fecha"},
        ev("ttwo/eur", date="2026-12-02", kind="concert", confidence=1.7),  # se normaliza
        ev("WMT", date="2026-11-02", kind="regulatory", effect="sideways")))
    assert events.refresh(con, bolsa, NOW, HS, client=client) == 2
    assert [(r["code"], r["kind"], r["date"], r["effect"], r["confidence"]) for r in rows(con)] == [
        ("WMT", "regulatory", "2026-11-02", "mixed", 0.8), ("TTWO", "other", "2026-12-02", "up", 1.0)]


def test_upsert_dedupes_and_counts_only_changes(bolsa, con):
    first = answer(ev(), ev(confidence=0.4), ev(confidence=0.6, kind="product_launch"),
                   ev(confidence=0.5, kind="product_launch"))
    assert events.refresh(con, bolsa, NOW, HS, client=FakeClient(first)) == 2  # duplicados: el más seguro
    assert {(r["kind"], r["confidence"]) for r in rows(con)} == {("game_release", 0.8),
                                                                 ("product_launch", 0.6)}
    later = NOW + timedelta(hours=5)
    assert events.refresh(con, bolsa, later, HS, client=FakeClient(answer(ev()))) == 0  # igual: no cuenta
    assert events.refresh(con, bolsa, later + timedelta(hours=5),
                          HS, client=FakeClient(answer(ev(confidence=0.9, why="otra razón")))) == 1
    r = [r for r in rows(con) if r["kind"] == "game_release"]
    assert len(r) == 1 and (r[0]["confidence"], r[0]["why"], r[0]["created"]) == (
        0.9, "otra razón", NOW.isoformat())
    assert len(rows(con)) == 2


def test_postponed_events_are_replaced(bolsa, con):
    old = put(con, "TTWO", "2026-11-19", kind="game_release", title="Lanzamiento de GTA VI")
    past = put(con, "TTWO", "2026-10-31", kind="keynote")
    client = FakeClient(answer(ev(date="2026-11-26", why="retrasado una semana"), obsolete=[old, past, 999]))
    assert events.refresh(con, bolsa, NOW, HS, client=client) == 2
    assert f"- {old} · TTWO · game_release · 2026-11-19 · Lanzamiento de GTA VI" in client.prompt()
    assert "2026-10-31" not in client.prompt()  # solo se enseñan los futuros
    assert [(r["id"] == past, r["date"]) for r in rows(con)] == [(True, "2026-10-31"), (False, "2026-11-26")]


# --- cuándo toca --------------------------------------------------------------------------------
def test_due_respects_refresh_hours_and_budget(bolsa, con):
    assert events.due(con, bolsa, NOW)
    assert events.refresh(con, bolsa, NOW, HS, client=FakeClient(answer())) == 0
    assert not events.due(con, bolsa, NOW + timedelta(hours=3, minutes=59))
    assert events.due(con, bolsa, NOW + timedelta(hours=4))
    db.KV(con).set("events:calls:2026-11-02", "8")
    client = FakeClient(answer(ev()))
    assert not events.due(con, bolsa, NOW + timedelta(hours=5))
    assert events.refresh(con, bolsa, NOW + timedelta(hours=5), HS, client=client) == 0
    assert client.calls == [] and events.due(con, bolsa, at(3, 9))  # día nuevo, presupuesto nuevo
    assert not events.due(con, {**bolsa, "events": {**bolsa["events"], "enabled": False}}, at(3, 9))
    assert not events.due(con, {k: v for k, v in bolsa.items() if k != "events"}, at(3, 9))


def test_without_api_key_nothing_happens(bolsa, con, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    made: list = []
    monkeypatch.setattr(events.anthropic, "Anthropic", lambda: made.append(1))
    client = FakeClient(answer(ev()))
    assert not events.due(con, bolsa, NOW)
    assert events.refresh(con, bolsa, NOW, HS, client=client) == 0
    assert events.refresh(con, bolsa, NOW, HS) == 0
    assert client.calls == made == [] and db.KV(con).get("events:last") is None  # ni cliente real


@pytest.mark.parametrize("client", [FakeClient(error=RuntimeError("caída")),
                                    FakeClient(answer(ev()), stop="refusal")])
def test_errors_never_break_the_tick(bolsa, con, client):
    assert events.refresh(con, bolsa, NOW, HS, client=client) == 0
    assert rows(con) == [] and db.KV(con).get("events:calls:2026-11-02") == "1"


def test_default_client_is_the_anthropic_sdk(bolsa, con, monkeypatch):
    fake = FakeClient(answer(ev()))
    monkeypatch.setattr(events.anthropic, "Anthropic", lambda: fake)
    assert events.refresh(con, bolsa, NOW, HS) == 1 and len(fake.calls) == 1


# --- titulares ----------------------------------------------------------------------------------
def test_headline_selection(bolsa, con):
    hs = [hl("GTA VI release date set", hours=80),           # más de 72 h
          hl("Weather turns cold in Madrid"),                # ni palabra clave ni empresa
          hl("Walmart opens a new store", hours=2),          # empresa del universo
          hl("Quarterly results season starts", hours=3),    # palabra clave
          hl("Rockstar teases trailer", hours=-1),           # del futuro (reloj desfasado)
          hl("Walmart opens a new store", hours=4)]          # repetido
    client = FakeClient(answer())
    events.refresh(con, bolsa, NOW, hs, client=client)
    news = client.prompt().split("Titulares (")[1].splitlines()[1:]
    assert news == ["- [lun 2026-11-02] [reuters] Walmart opens a new store",
                    "- [lun 2026-11-02] [reuters] Quarterly results season starts"]


def test_keyword_headlines_go_first_and_at_most_80(bolsa, con):
    hs = [hl(f"Walmart store news number {i}", hours=i / 10) for i in range(100)]
    hs.append(hl("Nvidia GTC keynote set for March 16", hours=60, summary="x" * 500))
    client = FakeClient(answer())
    events.refresh(con, bolsa, NOW, hs, client=client)
    news = [x for x in client.prompt().split("Titulares (")[1].splitlines() if x.startswith("- [")]
    assert len(news) == 80 and news[-1].startswith("- [vie 2026-10-30] [reuters] Nvidia GTC keynote")
    assert news[-1].endswith(": " + "x" * 300)  # resumen recortado


def test_no_headlines_no_call(bolsa, con):
    client = FakeClient(answer(ev()))
    assert events.refresh(con, bolsa, NOW, [hl("Weather turns cold")], client=client) == 0
    assert client.calls == [] and not events.due(con, bolsa, NOW + timedelta(hours=1))


def test_headline_rows_from_the_database_work_too(bolsa, con):
    db.save_headlines(con, "2026-11-02", NOW.isoformat(), HS, [1.0, 1.0])
    client = FakeClient(answer(ev()))
    assert events.refresh(con, bolsa, NOW, con.execute("SELECT * FROM headlines").fetchall(),
                          client=client) == 1
    assert "Rockstar confirms GTA VI release date" in client.prompt()


def test_keywords():
    assert {"keynote", "unveil", "launch event", "release date", "earnings", "results", "investor day", "gtc",
            "wwdc", "presentación", "lanzamiento", "resultados", "gta 6", "gta vi"} <= set(events.KEYWORDS)
    p = ingest.pattern(events.KEYWORDS)
    assert p.search("Nvidia GTC 2027 keynote") and p.search("GTA VI se retrasa")
    assert not p.search("Gold rises")


def test_real_config_prompt_lists_the_whole_universe(con, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    cfg = config_mod.load(ROOT / "config_usa.toml")  # la cartera activa (Europa está en cierre, sin eventos)
    client = FakeClient(answer())
    assert events.refresh(con, cfg, NOW, HS, client=client) == 0
    assert client.calls[0]["model"] == cfg["sentiment"]["llm_model"]
    assert all(f"- {config_mod.asset(s)}: {cfg['market'][config_mod.asset(s)]['name']}" in client.prompt()
               for s in cfg["general"]["symbols"])


# --- score --------------------------------------------------------------------------------------
def test_score_ramps_up_before_and_reverses_after(bolsa, con):
    put(con, "TTWO", "2026-11-19", conf=0.8)

    def s(now):
        return events.score(con, bolsa, "TTWO", now)

    assert s(at(2)) == 0.0 and s(at(11)) == 0.0               # d = 17, d = 8: aún lejos
    assert s(at(12)) == pytest.approx(0.8 / 7)                # d = 7
    assert s(at(15)) == pytest.approx(0.8 * 4 / 7)            # d = 4
    assert s(at(18)) == pytest.approx(0.8)                    # víspera
    assert s(at(19)) == pytest.approx(-0.4)                   # el día: vende la noticia
    assert s(datetime(2026, 11, 18, 23, 30, tzinfo=UTC)) == pytest.approx(-0.4)  # ya es 19 en Madrid
    assert s(at(22)) == pytest.approx(-0.4) and s(at(23)) == 0.0  # post_days = 3
    assert events.score(con, bolsa, "TTWO/EUR", at(18)) == pytest.approx(0.8)


def test_score_signs_mixed_duplicates_and_clamp(bolsa, con):
    put(con, "NVDA", "2026-11-05", effect="mixed", kind="earnings")
    put(con, "WMT", "2026-11-05", effect="down", conf=0.5)
    assert events.score(con, bolsa, "NVDA", at(2)) == 0.0
    assert events.score(con, bolsa, "WMT", at(2)) == pytest.approx(-0.5 * (1 - 2 / 7))
    assert events.score(con, bolsa, "WMT", at(6)) == pytest.approx(0.25)  # bajista: rebote después
    put(con, "TTWO", "2026-11-03", conf=0.6, kind="product_launch")
    put(con, "TTWO", "2026-11-03", conf=0.8, kind="game_release")  # el mismo evento con otro tipo
    assert events.score(con, bolsa, "TTWO", at(2)) == pytest.approx(0.8)
    put(con, "TTWO", "2026-11-04", conf=1.0)
    assert events.score(con, bolsa, "TTWO", at(2)) == 1.0  # 0.8 + 6/7, acotado
    off = {**bolsa, "events": {**bolsa["events"], "enabled": False}}
    assert events.score(con, off, "TTWO", at(2)) == 0.0


# --- /eventos -----------------------------------------------------------------------------------
def test_lines_format_and_escape(bolsa, con):
    put(con, "TTWO", "2026-11-19", conf=0.8, kind="game_release", title="Lanzamiento de GTA VI",
        why="Fecha confirmada <oficial> & cerrada")
    put(con, "WMT", "2026-11-19", conf=0.3, kind="game_release", title="<b>AT&T</b>")
    put(con, "NVDA", "2026-10-31", effect="mixed", conf=0.5, kind="earnings", title="Resultados")
    put(con, "NVDA", "2026-11-02", effect="down", conf=0.25, kind="regulatory", title="Veto")
    put(con, "NVDA", "2026-10-20", title="viejo")
    put(con, "TTWO", "2026-12-20", title="lejano")
    put(con, "XXXX", "2026-11-10", title="fuera del universo")
    assert events.lines(con, bolsa, NOW) == [
        "📅 31/10 (hace 2 d) · NVDA · Resultados · ↔️ 50 %",
        "📅 02/11 (hoy) · NVDA · Veto · ⬇️ 25 %",
        "📅 19/11 (en 17 d) · TTWO · Lanzamiento de GTA VI · ⬆️ 80 %\n"
        "   <i>Fecha confirmada &lt;oficial&gt; &amp; cerrada</i>",
        "📅 19/11 (en 17 d) · WMT · &lt;b&gt;AT&amp;T&lt;/b&gt; · ⬆️ 30 %"]
    assert [r["date"] for r in events.upcoming(con, bolsa, NOW, days=10)] == ["2026-10-31", "2026-11-02"]


def test_lines_when_empty_or_disabled(bolsa, con):
    assert events.lines(con, bolsa, NOW) == ["No hay eventos próximos detectados (se revisan cada 4 h)."]
    assert events.upcoming(con, bolsa, NOW) == []
    off = {**bolsa, "events": {**bolsa["events"], "enabled": False}}
    assert events.lines(con, off, NOW) == ["Los eventos están desactivados ([events] enabled = false)."]
