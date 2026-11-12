"""Telegram de la cartera de bolsa (general.engine = "stocks"): propuestas con ✅/❌ y respuestas
"sí"/"no", send_get_id/edit_message, /estado con varias posiciones, /eventos y el informe de learn.
Sin red: `post`/`get` en memoria y newsbot.stocks (engine, events) sustituido por módulos falsos."""
import copy
import json
import random
import sys
import time
import types
from contextlib import closing
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from newsbot import portfolio
from newsbot.bot import _connect
from newsbot.reporting import telegram
from newsbot.research import learn
from newsbot.storage import db

ENV = {"TELEGRAM_BOT_TOKEN": "T0K", "TELEGRAM_CHAT_ID": "42"}
MAD = ZoneInfo("Europe/Madrid")
OPEN = datetime(2026, 11, 2, 10, 0, tzinfo=MAD).astimezone(UTC)  # lunes, mercado abierto
BOUGHT = "✅ Comprado 0.0150 RHM a 1001.20 € (15.02 €) · stop en 921.10 €"
REJECTED = "❌ Propuesta descartada."
ERR = "📈 Bolsa · " + telegram.ERROR


def stocks_cfg(cfg, tmp_path, word="bolsa", label="📈 Bolsa"):
    b = copy.deepcopy(cfg)
    b["general"] |= {"portfolio": word, "label": label, "engine": "stocks", "exchange": "yahoo",
                     "tick_minutes": 15, "symbols": ["RHM/EUR", "NVDA/EUR"],
                     "data_dir": str(tmp_path / word)}
    b["news"]["assets"] = {"RHM": ["rheinmetall"], "NVDA": ["nvidia"]}
    b["session"] = {"tz": "Europe/Madrid", "days": [0, 1, 2, 3, 4], "open": "09:20", "close": "17:30"}
    b["sentiment"]["fallback"] = "none"
    return b


@pytest.fixture
def bolsa(cfg, tmp_path):
    b = stocks_cfg(cfg, tmp_path)
    portfolio.register([cfg, b])
    return b


@pytest.fixture
def fake(monkeypatch):
    """newsbot.stocks falso: registra las llamadas (cartera, id, acepta, cartera activa)."""
    calls = types.SimpleNamespace(confirm=[], positions=[], events=[])
    engine = types.ModuleType("newsbot.stocks.engine")
    events = types.ModuleType("newsbot.stocks.events")

    def confirm(c, pid, accept, now=None, exchange=None, broker=None):
        calls.confirm.append((portfolio.word(c), pid, accept, portfolio.CURRENT.get()))
        return BOUGHT if accept else REJECTED

    def positions_lines(con, c, now):
        calls.positions.append(portfolio.word(c))
        return ["<b>Posiciones</b> (1/3)", "RHM/EUR 0.0150 · entrada 1000.00 €", "Caja 35.00 €"]

    def lines(con, c, now):
        calls.events.append((portfolio.word(c), portfolio.CURRENT.get()))
        return ["<b>Eventos</b>", "NVDA · GTC · 2026-11-10 ↑"]

    engine.confirm, engine.positions_lines = confirm, positions_lines
    engine.proposal_text = lambda row, c: f"<b>Comprar</b> {row['symbol']} #{row['id']}"
    events.lines = lines
    pkg = types.ModuleType("newsbot.stocks")
    pkg.__path__ = []
    pkg.engine, pkg.events = engine, events
    for name, mod in (("newsbot.stocks", pkg), ("newsbot.stocks.engine", engine),
                      ("newsbot.stocks.events", events)):
        monkeypatch.setitem(sys.modules, name, mod)
    calls.engine, calls.events_mod = engine, events
    return calls


class Bot:
    """`post` de Telegram en memoria: guarda (método, payload) y responde con un message_id."""

    def __init__(self):
        self.calls = []

    def __call__(self, url, payload):
        self.calls.append((url.rsplit("/", 1)[1], payload))
        return {"ok": True, "result": {"message_id": 100 + len(self.calls)}}

    def of(self, method):
        return [p for m, p in self.calls if m == method]


def poll(cfg, *updates, bot=None):
    bot = bot or Bot()
    with _connect(cfg) as con:
        n = telegram.poll(con, cfg, ENV, get=lambda u: {"result": list(updates)}, post=bot)
    return n, bot


def press(data, chat=42, text="📈 Bolsa · Comprar RHM/EUR #1", uid=1):
    return {"update_id": uid, "callback_query": {
        "id": f"cq{uid}", "data": data, "message": {"message_id": 7, "chat": {"id": chat}, "text": text}}}


def say(text, ago=0, uid=1, reply_to=None):
    msg = {"chat": {"id": 42}, "text": text, "date": int(time.time()) - ago}
    if reply_to is not None:
        msg["reply_to_message"] = {"message_id": reply_to}
    return {"update_id": uid, "message": msg}


def propose(c, symbol="RHM/EUR", created=-60, expires=600, msg_id=7, sent=None) -> int:
    """Propuesta pendiente; `sent` = segundos desde ahora en que te llegó (por defecto, al crearla).
    msg_id=None: nunca se envió."""
    now = datetime.now(UTC)
    sent_at = None if msg_id is None else (now + timedelta(seconds=created if sent is None else sent))
    with _connect(c) as con, con:
        return con.execute(
            "INSERT INTO proposals(created, expires, symbol, side, eur, qty, ref_price, msg_id, sent_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            ((now + timedelta(seconds=created)).astimezone(MAD).isoformat(),
             (now + timedelta(seconds=expires)).astimezone(MAD).isoformat(),
             symbol, "buy", 15.0, 0.015, 1000.0, msg_id,
             sent_at.isoformat() if sent_at else None)).lastrowid


def h(cfg, text, now=OPEN):
    with _connect(cfg) as con:
        return telegram.handle(con, cfg, text, now)


# --- send_get_id / edit_message ------------------------------------------------------------------
def test_send_get_id_returns_last_chunk_id_with_buttons_and_label(bolsa):
    bot, kb = Bot(), {"inline_keyboard": [[{"text": "✅", "callback_data": "o:bolsa:1:y"}]]}
    with portfolio.active(bolsa):
        mid = telegram.send_get_id("<b>Comprar</b> RHM\n" + "x" * 2000 + "\n" + "y" * 2000, ENV,
                                   post=bot, markup=kb)
    sent = bot.of("sendMessage")
    assert mid == 102 and len(sent) == 2
    assert sent[0]["text"].startswith("📈 Bolsa · <b>Comprar</b> RHM") and "reply_markup" not in sent[0]
    assert sent[1]["reply_markup"] == kb
    assert all(p["parse_mode"] == "HTML" and p["chat_id"] == "42" for p in sent)
    assert telegram.send_get_id("hola", ENV, post=lambda u, p: None) is None  # sin message_id
    assert telegram.send_get_id("hola", {}, post=bot) is None

    def down(u, p):
        raise OSError("down")

    assert telegram.send_get_id("hola", ENV, post=down) is None

    def no_html(u, p):
        if "parse_mode" in p:
            raise OSError("400 can't parse entities")
        return {"ok": True, "result": {"message_id": 9}}

    assert telegram.send_get_id("<b>a</b>", ENV, post=no_html) == 9  # reintento en texto plano


def test_send_keeps_its_payloads():
    bot = Bot()
    assert telegram.send("hola", ENV, post=bot) is True
    assert bot.calls == [("sendMessage", {"chat_id": "42", "text": "hola"})]


class TgError(Exception):
    def __init__(self, description):
        self.code, self.body = 400, json.dumps({"description": description}).encode()

    def read(self):
        return self.body


def test_edit_message_payload(bolsa):
    bot = Bot()
    assert telegram.edit_message(7, "<b>Comprar</b> RHM\n\n✅ hecho", ENV, post=bot) is True
    assert bot.calls == [("editMessageText", {
        "chat_id": "42", "message_id": 7, "reply_markup": {"inline_keyboard": []},
        "text": "<b>Comprar</b> RHM\n\n✅ hecho", "parse_mode": "HTML",
        "link_preview_options": {"is_disabled": True}})]
    kb = {"inline_keyboard": [[{"text": "x", "callback_data": "o:bolsa:1:n"}]]}
    with portfolio.active(bolsa):
        assert telegram.edit_message(8, "a &amp; b", ENV, post=bot, markup=kb)
    assert bot.calls[1][1]["text"] == "📈 Bolsa · a &amp; b" and bot.calls[1][1]["reply_markup"] == kb
    assert telegram.edit_message(None, "x", ENV, post=bot) is False
    assert telegram.edit_message(7, "x", {}, post=bot) is False and len(bot.calls) == 2

    def same(u, p):
        raise TgError("Bad Request: message is not modified")

    assert telegram.edit_message(7, "x", ENV, post=same) is True
    seen = []

    def no_html(u, p):
        seen.append(p)
        if "parse_mode" in p:
            raise TgError("Bad Request: can't parse entities")

    assert telegram.edit_message(7, "<b>a &amp; b</b>", ENV, post=no_html) is True
    assert seen[1] == {"chat_id": "42", "message_id": 7, "reply_markup": {"inline_keyboard": []},
                       "text": "a & b"}

    def down(u, p):
        raise OSError("down")

    assert telegram.edit_message(7, "x", ENV, post=down) is False


# --- botones ✅/❌ ----------------------------------------------------------------------------
def test_callback_accept_confirms_and_edits(cfg, bolsa, fake):
    pid = propose(bolsa, msg_id=7)
    n, bot = poll(cfg, press(f"o:bolsa:{pid}:y"))
    assert n == 1 and fake.confirm == [("bolsa", pid, True, "bolsa")]  # dentro de su cartera
    assert [m for m, _ in bot.calls] == ["answerCallbackQuery", "editMessageText"]
    assert bot.calls[0][1] == {"callback_query_id": "cq1", "text": "Procesando…"}  # se contesta al instante
    edit = bot.calls[1][1]
    assert edit["message_id"] == 7 and edit["reply_markup"] == {"inline_keyboard": []}  # sin botones
    assert edit["text"] == f"📈 Bolsa · <b>Comprar</b> RHM/EUR #{pid}\n\n{BOUGHT}"
    assert portfolio.CURRENT.get() == ""


def test_callback_reject(cfg, bolsa, fake):
    pid = propose(bolsa)
    n, bot = poll(cfg, press(f"o:BOLSA:{pid}:n"))
    assert n == 1 and fake.confirm == [("bolsa", pid, False, "bolsa")]
    assert bot.of("answerCallbackQuery")[0]["text"] == "Descartando…"
    assert bot.of("editMessageText")[0]["text"].endswith(f"\n\n{REJECTED}")


def test_callback_from_another_chat_is_ignored(cfg, bolsa, fake):
    n, bot = poll(cfg, press("o:bolsa:1:y", chat=666))
    assert n == 0 and bot.calls == [] and fake.confirm == []


def test_callback_unknown_word_or_bad_data(cfg, bolsa, fake):
    for data in ("o:acciones:1:y", "o::1:y", "o:cripto:1:y", "o:bolsa:x:y", "o:bolsa:1:s"):
        n, bot = poll(cfg, press(data))
        assert n == 1 and bot.calls == [("answerCallbackQuery", {"callback_query_id": "cq1",
                                                                 "text": "Propuesta desconocida"})], data
    assert fake.confirm == []  # "" y "cripto" son la principal, que no es de bolsa


def test_callback_exception_does_not_stop_the_listener(cfg, bolsa, fake, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("secreto")

    monkeypatch.setattr(fake.engine, "confirm", boom)
    n, bot = poll(cfg, press("o:bolsa:1:y"), {"update_id": 2, "message": {"chat": {"id": 42},
                                                                          "text": "/ayuda"}})
    assert n == 2
    assert bot.of("answerCallbackQuery") == [{"callback_query_id": "cq1", "text": "Procesando…"}]
    texts = [p["text"] for p in bot.of("sendMessage")]
    assert texts[0] == ERR and "secreto" not in texts[0] and "Comandos" in texts[1]
    assert bot.of("editMessageText") == []
    with _connect(cfg) as con:
        assert db.KV(con).get("tg:offset") == "3"


def test_callback_edit_falls_back_to_the_original_text(cfg, bolsa, fake, monkeypatch):
    def broken(row, c):
        raise KeyError("x")

    monkeypatch.setattr(fake.engine, "proposal_text", broken)
    n, bot = poll(cfg, press("o:bolsa:1:y", text="📈 Bolsa · Comprar RHM <x> & co"))
    assert bot.of("editMessageText")[0]["text"] == f"📈 Bolsa · Comprar RHM &lt;x&gt; &amp; co\n\n{BOUGHT}"


def test_refresh_callbacks_are_unchanged(cfg, bolsa, fake):
    n, bot = poll(cfg, press("r:/eventos bolsa"))
    assert n == 1 and [m for m, _ in bot.calls] == ["answerCallbackQuery", "editMessageText"]
    assert "NVDA · GTC" in bot.calls[1][1]["text"] and fake.confirm == []


# --- respuestas de texto ------------------------------------------------------------------------
@pytest.mark.parametrize("word", ["sí", "Si", "yes", "✅", "¡Sí!", "✅\ufe0f"])
def test_text_yes_accepts_the_only_pending(cfg, bolsa, fake, word):
    pid = propose(bolsa, created=-60, msg_id=6)
    n, bot = poll(cfg, say(word))
    assert n == 1 and fake.confirm == [("bolsa", pid, True, "bolsa")]
    edit, reply = bot.of("editMessageText"), bot.of("sendMessage")
    assert edit[0]["message_id"] == 6 and edit[0]["reply_markup"] == {"inline_keyboard": []}
    assert edit[0]["text"] == f"📈 Bolsa · <b>Comprar</b> RHM/EUR #{pid}\n\n{BOUGHT}"
    assert [p["text"] for p in reply] == [f"📈 Bolsa · {BOUGHT}"] and reply[0]["parse_mode"] == "HTML"


@pytest.mark.parametrize("word", ["no", "❌"])
def test_text_no_rejects(cfg, bolsa, fake, word):
    pid = propose(bolsa)
    n, bot = poll(cfg, say(word))
    assert fake.confirm == [("bolsa", pid, False, "bolsa")]
    assert [p["text"] for p in bot.of("sendMessage")] == [f"📈 Bolsa · {REJECTED}"]


@pytest.mark.parametrize("word", ["ok", "OK", "vale", "dale", "y", "n"])
def test_ambiguous_words_never_confirm(cfg, bolsa, fake, word):
    propose(bolsa)
    poll(cfg, say(word))
    assert fake.confirm == []  # "ok" a otro aviso nunca compra nada


def test_text_answer_ignores_expired_unsent_and_later_proposals(cfg, bolsa, fake):
    pid = propose(bolsa, created=-120)
    propose(bolsa, "NVDA/EUR", created=-90, expires=-1, msg_id=7)  # caducada
    propose(bolsa, "NVDA/EUR", created=-200, msg_id=None)  # nunca se envió
    propose(bolsa, "IFX/EUR", created=-200, sent=0, msg_id=8)  # creada antes, pero te llegó después
    n, bot = poll(cfg, say("sí", ago=30))
    assert fake.confirm == [("bolsa", pid, True, "bolsa")]


def test_several_pending_need_the_button_or_a_reply(cfg, bolsa, fake):
    stop = propose(bolsa, "LDO/EUR", created=-100, msg_id=10)
    propose(bolsa, "ASML/EUR", created=-100, msg_id=11)
    n, bot = poll(cfg, say("sí"))
    assert fake.confirm == [] and "Hay 2 propuestas pendientes" in bot.of("sendMessage")[0]["text"]
    n, bot = poll(cfg, say("sí", uid=2, reply_to=10))  # respondiendo al mensaje del stop-loss
    assert fake.confirm == [("bolsa", stop, True, "bolsa")]


def test_reply_to_a_non_pending_message_does_nothing(cfg, bolsa, fake):
    propose(bolsa, msg_id=6)
    n, bot = poll(cfg, say("sí", reply_to=99))
    assert fake.confirm == [] and "no es una propuesta pendiente" in bot.of("sendMessage")[0]["text"]


def test_text_answer_across_portfolios_needs_a_single_pending(cfg, tmp_path, fake):
    a, b = stocks_cfg(cfg, tmp_path), stocks_cfg(cfg, tmp_path, "usa", "🇺🇸 USA")
    portfolio.register([cfg, a, b])
    pid = propose(b, "NVDA/EUR", created=-100, msg_id=21)
    n, bot = poll(cfg, say("sí"))
    assert fake.confirm == [("usa", pid, True, "usa")]
    assert bot.of("sendMessage")[0]["text"] == f"🇺🇸 USA · {BOUGHT}"


def test_text_answer_without_pending(cfg, bolsa, fake):
    n, bot = poll(cfg, say("sí"))
    assert n == 1 and [p["text"] for p in bot.of("sendMessage")] == [telegram.NO_PENDING]
    assert fake.confirm == []


def test_text_answer_too_old_is_not_applied(cfg, bolsa, fake):
    propose(bolsa, created=-900, expires=600)
    n, bot = poll(cfg, say("sí", ago=600))
    assert n == 1 and fake.confirm == []
    assert "más de 5 minutos" in bot.of("sendMessage")[0]["text"]


def test_text_answer_exception_is_isolated(cfg, bolsa, fake, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("secreto")

    monkeypatch.setattr(fake.engine, "confirm", boom)
    propose(bolsa)
    n, bot = poll(cfg, say("sí"), say("/ayuda", uid=2))
    assert n == 2 and [p["text"] for p in bot.of("sendMessage")][0] == ERR


def test_yes_without_stocks_portfolio_is_unchanged(cfg, tmp_path, fake):
    single = stocks_cfg(cfg, tmp_path)
    single["general"]["engine"] = "single"
    portfolio.register([cfg, single])
    n, bot = poll(cfg, say("ok"))
    assert bot.of("sendMessage")[0]["text"].startswith("Comando no reconocido.")
    portfolio.REGISTRY.clear()
    n, bot = poll(cfg, say("sí"))
    assert bot.of("sendMessage")[0]["text"].startswith("Comando no reconocido.") and fake.confirm == []


# --- /estado, /eventos, /ayuda y menú ------------------------------------------------------------
def test_estado_bolsa_lists_positions(cfg, bolsa, fake):
    with _connect(bolsa) as con, con:
        con.execute("INSERT INTO equity_daily(day, cash, qty, avg_entry, price, equity, peak, symbol) "
                    "VALUES ('2026-11-02', 35, 0, 0, 0, 50.2, 50.2, NULL)")
    with _connect(cfg) as con, con:
        con.execute("INSERT INTO equity_daily(day, cash, qty, avg_entry, price, equity, peak, symbol) "
                    "VALUES ('2026-11-02', 10, 0, 0, 0, 10, 10, NULL)")
    out = h(cfg, "/estado bolsa")
    assert out.startswith("<b>📈 Bolsa · PAPER</b>") and fake.positions == ["bolsa"]
    lines = out.split("\n")
    i = lines.index("<b>Posiciones</b> (1/3)")
    assert lines[i - 1].startswith("Capital <b>50.20 €</b>") and lines[i + 2] == "Caja 35.00 €"
    assert "Posición:" not in out
    crypto = h(cfg, "/estado")
    assert "Posición: ninguna" in crypto and "<b>Posiciones</b>" not in crypto  # cripto, como antes
    assert fake.positions == ["bolsa"]


def test_estado_bolsa_survives_a_broken_engine(cfg, bolsa, fake, monkeypatch):
    def boom(*a):
        raise RuntimeError("x")

    monkeypatch.setattr(fake.engine, "positions_lines", boom)
    out = h(cfg, "/estado bolsa")
    assert "Posiciones: no disponibles" in out and "Sin capital registrado" in out


def test_eventos_routes_to_the_stocks_portfolio(cfg, bolsa, fake):
    want = "<b>Eventos</b>\nNVDA · GTC · 2026-11-10 ↑"
    assert h(cfg, "/eventos") == want and h(cfg, "/events bolsa") == want
    assert h(cfg, "/eventos cripto") == want  # cripto no tiene eventos: va a la de bolsa
    assert h(bolsa, "/eventos") == want  # desde la propia cartera de bolsa
    assert fake.events == [("bolsa", "bolsa")] * 3 + [("bolsa", "")]  # h() no activa la cartera
    fake.events_mod.lines = lambda con, c, now: []
    assert h(cfg, "/eventos") == "Sin eventos próximos."
    portfolio.register([cfg])
    assert h(cfg, "/eventos") == "No hay ninguna cartera de bolsa."


def test_ayuda_and_menu_with_a_stocks_portfolio(cfg, bolsa):
    out = h(cfg, "/ayuda").split("\n")
    names = [x.split()[0] for x in out[1:] if x.startswith("/")]
    stocks = list(telegram.STOCKS_COMMANDS)
    assert names[-4:] == [*(f"/{n}" for n in stocks), "/ayuda"]
    assert len(names) == len(telegram.COMMANDS) + len(stocks)
    assert "(/events)" in out[names.index("/eventos") + 1]
    assert out[-2] == "Añade «bolsa» a cualquier comando para la cartera de bolsa (p. ej. /estado bolsa)."
    assert "«sí» o «no»" in out[-1] and "✅" in out[-1]
    calls = []
    telegram.register_commands(ENV, post=lambda u, p: calls.append(p))
    menu = [c["command"] for c in calls[0]["commands"]]
    assert menu == [*list(telegram.COMMANDS)[:-1], *stocks, "ayuda"]
    portfolio.register([cfg])  # solo cripto: ayuda y menú como antes
    plain = h(cfg, "/ayuda")
    assert "/eventos" not in plain and len(plain.split("\n")) == len(telegram.COMMANDS) + 1
    calls.clear()
    telegram.register_commands(ENV, post=lambda u, p: calls.append(p))
    assert [c["command"] for c in calls[0]["commands"]] == list(telegram.COMMANDS)


def test_eventos_has_a_refresh_button(cfg, bolsa):
    assert telegram._markup("/eventos", cfg) == {
        "inline_keyboard": [[{"text": "🔄 Actualizar", "callback_data": "r:/eventos"}]]}


# --- learn: componentes de bolsa en el informe --------------------------------------------------
def _ticks(con, comps, n=40, symbols=("RHM/EUR", "NVDA/EUR")):
    rnd, t0 = random.Random(3), datetime(2026, 8, 1, tzinfo=UTC)
    prices = dict.fromkeys(symbols, 100.0)
    for i in range(n):
        rows = []
        for s in symbols:
            raw = {"news": rnd.uniform(-1, 1), "trend": rnd.uniform(-1, 1)} | {
                c: rnd.uniform(-1, 1) for c in comps}
            rows.append({"symbol": s, "price": prices[s], "signal": 0.1, "action": "HOLD", "reason": "",
                         "held": False, "raw": raw, "contributions": {}})
            prices[s] *= 1 + rnd.uniform(-0.02, 0.02)
        db.save_ticks(con, t0 + i * timedelta(hours=4), rows)


def test_learn_report_hides_empty_stocks_components(cfg, tmp_path, monkeypatch):
    now = datetime(2026, 9, 25, tzinfo=UTC)
    assert set(learn.STOCKS_ONLY) <= set(learn.COMPONENTS)
    with closing(db.connect(tmp_path / "c.db")) as con:
        _ticks(con, ())
        txt = learn.report(con, cfg, now)
        assert "rsi·sombra" in txt and not any(c in txt for c in learn.STOCKS_ONLY)
        with monkeypatch.context() as m:
            m.setattr(learn, "COMPONENTS", learn.COMPONENTS[:9])  # el informe de antes
            assert learn.report(con, cfg, now) == txt
    with closing(db.connect(tmp_path / "s.db")) as con:
        _ticks(con, ("breakout", "macd"))
        w = cfg["weights"] | {"events": 0.15, "news": 0.25}
        txt = learn.report(con, cfg | {"weights": w}, now)
        assert "breakout·sombra" in txt and "macd·sombra" in txt and "high52" not in txt
        assert "\nevents " in txt and "events·sombra" not in txt  # con peso aunque sin datos
        ev = learn.evaluate(con, cfg, now)
        assert ev["components"]["breakout"]["n"] > 0 and ev["components"]["high52"]["n"] == 0


def test_disabled_main_portfolio_routes_commands_to_stocks(cfg, bolsa, fake):
    cfg["general"]["enabled"] = False  # solo Trading 212: la cripto está apagada
    out = h(cfg, "/estado")
    assert out.startswith("<b>📈 Bolsa") and "🪙 Cripto" not in out
    assert h(cfg, "/estado cripto").startswith("<b>newsbot")  # sigue consultable con su palabra
    kb = telegram._markup("/estado", cfg)
    assert not any("cripto" in b["text"] for row in kb["inline_keyboard"] for b in row)


def test_estado_bolsa_live_shows_the_broker_balance(cfg, bolsa, fake, monkeypatch):
    bolsa["general"]["mode"] = "live"
    balance = {"cash": 50.0, "positions": 0, "value": 0.0, "ts": OPEN.timestamp()}
    monkeypatch.setattr(fake.engine, "broker_balance", lambda cfg, con, now, **k: balance, raising=False)
    out = h(cfg, "/estado bolsa")
    assert "💶 Trading 212: <b>50.00 €</b> disponibles" in out and "Sin capital registrado" not in out


def test_reanudar_bolsa_resets_the_peak(cfg, bolsa):
    with _connect(bolsa) as con, con:
        con.execute("INSERT INTO equity_daily(day, cash, qty, avg_entry, price, equity, peak) VALUES "
                    "('2026-11-03', 30, 0, 0, 0, 35.0, 50.0)")
        db.KV(con).set("kill_switch", "1")
    assert "Reanudado" in h(cfg, "/reanudar bolsa CONFIRMAR")
    with _connect(bolsa) as con:
        assert db.KV(con).get("peak") == "35.0" and db.KV(con).get("kill_switch") is None


def test_an_old_button_never_confirms_another_message_s_proposal(cfg, bolsa, fake):
    """Un ✅ de un mensaje viejo (la prueba en papel, otra base de datos) con el mismo número de
    propuesta no confirma la propuesta real: el botón tiene que ser el de su propio mensaje."""
    pid = propose(bolsa, msg_id=50)
    n, bot = poll(cfg, press(f"o:bolsa:{pid}:y"))  # el botón está en el mensaje 7, no en el 50
    assert n == 1 and fake.confirm == []
    assert bot.of("answerCallbackQuery")[0]["text"].startswith("Botón de un mensaje antiguo")
    assert "botón antiguo" in bot.of("editMessageText")[0]["text"]


def test_improvement_buttons_apply_or_reject_the_params_proposal(cfg, bolsa, fake):
    with _connect(cfg) as con, con:
        a = con.execute("INSERT INTO params_history(created, params_json, status, note) VALUES (?,?,?,?)",
                        ("2026-11-05T15:00:00+00:00", json.dumps({"risk": {"min_order_eur": 2.0}}),
                         "proposed", json.dumps({"origin": "postmortem"}))).lastrowid
        b = con.execute("INSERT INTO params_history(created, params_json, status, note) VALUES (?,?,?,?)",
                        ("2026-11-05T15:01:00+00:00", json.dumps({"risk": {"min_order_eur": 3.0}}),
                         "proposed", json.dumps({"origin": "postmortem"}))).lastrowid
    n, bot = poll(cfg, press(f"m::{a}:y", text="🔬 Autopsia"))
    assert n == 1 and bot.of("answerCallbackQuery")[0]["text"] == "Aplicando…"
    edit = bot.of("editMessageText")[0]
    assert f"✅ Propuesta {a} aplicada" in edit["text"] and edit["reply_markup"] == {"inline_keyboard": []}
    n, bot = poll(cfg, press(f"m::{b}:n", uid=2, text="🔬 Autopsia"))
    assert f"❌ Propuesta {b} rechazada" in bot.of("editMessageText")[0]["text"]
    with _connect(cfg) as con:
        st = dict(con.execute("SELECT id, status FROM params_history").fetchall())
    assert st == {a: "applied", b: "rejected"}
    n, bot = poll(cfg, press(f"m:otra:{a}:y", uid=3))
    assert bot.of("answerCallbackQuery")[0]["text"] == "Mejora desconocida"


# --- vista conjunta: cripto apagada, bolsa + usa activas -----------------------------------------
@pytest.fixture
def both(cfg, tmp_path):
    b = stocks_cfg(cfg, tmp_path)
    u = stocks_cfg(cfg, tmp_path, word="usa", label="🇺🇸 USA")
    cfg["general"]["enabled"] = False  # después de copiar: bolsa y usa siguen activas
    u["general"]["symbols"] = ["NVDA/EUR", "XOM/EUR"]
    u["news"]["assets"] = {"NVDA": ["nvidia"], "XOM": ["exxon"]}
    u["session"] = {"tz": "America/New_York", "days": [0, 1, 2, 3, 4], "open": "09:30", "close": "16:00"}
    for c, eq in ((b, 20.0), (u, 29.45)):
        with _connect(c) as con, con:
            con.execute("INSERT INTO equity_daily(day, cash, qty, avg_entry, price, equity, peak) VALUES "
                        "('2026-11-02', ?, 0, 0, 0, ?, ?)", (eq, eq, eq))
    portfolio.register([cfg, b, u])
    return b, u


def test_estado_without_word_shows_every_portfolio(cfg, both, fake):
    out = h(cfg, "/estado")
    assert out.startswith("🌐 <b>Todas las carteras</b> · capital <b>49.45 €</b>")
    assert "<b>📈 Bolsa · PAPER</b>" in out and "<b>🇺🇸 USA · PAPER</b>" in out
    assert out.index("📈 Bolsa ·") < out.index("🇺🇸 USA ·") and "🪙 Cripto" not in out
    assert fake.positions == ["bolsa", "usa"]
    assert h(cfg, "/estado usa").startswith("<b>🇺🇸 USA")  # con palabra, solo esa
    kb = telegram._markup("/estado", cfg)["inline_keyboard"][0]
    assert [b["callback_data"] for b in kb] == ["r:/estado", "r:/estado bolsa", "r:/estado usa"]
    kb = telegram._markup("/estado usa", cfg)["inline_keyboard"][0]
    assert [b["callback_data"] for b in kb][-2:] == ["r:/estado bolsa", "r:/estado"]


def test_pausa_and_reanudar_without_word_change_every_portfolio(cfg, both):
    out = h(cfg, "/pausa")
    assert "<b>📈 Bolsa</b>\nPausado" in out and "<b>🇺🇸 USA</b>\nPausado" in out
    for c in both:
        with _connect(c) as con:
            assert db.KV(con).get("paused") == "1"
    assert "TODAS las carteras" in h(cfg, "/reanudar")
    assert h(cfg, "/reanudar CONFIRMAR").count("Reanudado") == 2
    for c in both:
        with _connect(c) as con:
            assert db.KV(con).get("paused") is None


def test_symbol_commands_go_where_the_symbol_is(cfg, both):
    assert h(cfg, "/moneda XOM").startswith("<b>🇺🇸 USA</b>\n<b>XOM</b>")
    nvda = h(cfg, "/moneda NVDA")
    assert "<b>📈 Bolsa</b>" in nvda and "<b>🇺🇸 USA</b>" in nvda
    assert h(cfg, "/moneda FOO") == "Valor no soportado: RHM, NVDA, XOM"
    assert h(cfg, "/moneda").startswith("Elige un valor")
    rows = telegram._markup("/moneda", cfg)["inline_keyboard"]
    assert [b["callback_data"] for b in rows[0]] == ["r:/moneda RHM", "r:/moneda NVDA", "r:/moneda XOM"]


def test_ids_go_to_the_only_portfolio_that_has_them(cfg, both):
    b, u = both

    def proposed(c):
        with _connect(c) as con, con:
            return con.execute("INSERT INTO params_history(created, params_json, status, note) "
                               "VALUES ('2026-11-05', ?, 'proposed', '{}')",
                               (json.dumps({"risk": {"min_order_eur": 2.0}}),)).lastrowid

    pid = proposed(u)
    assert h(cfg, f"/rechazar {pid}") == f"Propuesta {pid} rechazada."
    with _connect(u) as con:
        assert con.execute("SELECT status FROM params_history WHERE id = ?", (pid,)).fetchone()[0] \
            == "rejected"
    assert "en ninguna cartera" in h(cfg, "/aprobar 99")
    assert [proposed(u), proposed(b), proposed(b)] == [2, 1, 2]  # el #2 está pendiente en las dos
    out = h(cfg, "/aprobar 2")
    assert "existe en varias carteras" in out and "/aprobar bolsa" in out and "/aprobar usa" in out


def test_order_buttons_work_for_both_portfolios(cfg, both, fake):
    b, u = both
    pb, pu = propose(b), propose(u, symbol="NVDA/EUR", msg_id=8)
    poll(cfg, press(f"o:bolsa:{pb}:y"))
    upd = press(f"o:usa:{pu}:n", uid=2, text="🇺🇸 USA · Comprar NVDA/EUR")
    upd["callback_query"]["message"]["message_id"] = 8
    poll(cfg, upd)
    assert [(w, pid, ok) for w, pid, ok, _ in fake.confirm] == [("bolsa", pb, True), ("usa", pu, False)]
