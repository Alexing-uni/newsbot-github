"""Telegram con dos carteras (cripto + bolsa en papel): enrutado por palabra, pistas de comandos con
"bolsa", mercado cerrado, botones de cambio de cartera, prefijo de los avisos y resúmenes cruzados.
Sin red: bases de datos en tmp_path y `post`/`get` en memoria."""
import copy
import json
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from newsbot import portfolio
from newsbot.bot import _connect
from newsbot.news.rss import Headline
from newsbot.reporting import telegram
from newsbot.research import learn
from newsbot.storage import db

ENV = {"TELEGRAM_BOT_TOKEN": "T0K", "TELEGRAM_CHAT_ID": "42"}
MAD = ZoneInfo("Europe/Madrid")
OPEN = datetime(2026, 11, 2, 10, 0, tzinfo=MAD).astimezone(UTC)      # lunes, mercado abierto
SATURDAY = datetime(2026, 11, 7, 12, 0, tzinfo=MAD).astimezone(UTC)  # mercado cerrado


def tick(sym, signal, action):
    return {"symbol": sym, "price": 100.0, "signal": signal, "action": action, "reason": "r",
            "held": False, "raw": {"news": signal}, "contributions": {"news": signal / 2}}


def equity(con, eq):
    with con:
        con.execute("INSERT INTO equity_daily(day, cash, qty, avg_entry, price, equity, peak, symbol) "
                    "VALUES ('2026-11-02', ?, 0, 0, 0, ?, ?, NULL)", (eq, eq, eq))


@pytest.fixture
def bolsa(cfg, tmp_path):
    b = copy.deepcopy(cfg)
    b["general"] |= {"portfolio": "bolsa", "label": "📈 Bolsa", "exchange": "yahoo", "tick_minutes": 15,
                     "symbols": ["RHM/EUR", "GOLD/EUR"], "data_dir": str(tmp_path / "bolsa")}
    b["news"]["assets"] = {"RHM": ["rheinmetall"], "GOLD": ["gold"]}
    b["news"]["fng"] = False
    b["market"] = {"RHM": {"ticker": "RHM.DE", "isin": "DE0007030033", "name": "Rheinmetall"},
                   "GOLD": {"ticker": "4GLD.DE", "isin": "DE000A0S9GB0", "name": "Xetra-Gold"}}
    b["session"] = {"tz": "Europe/Madrid", "days": [0, 1, 2, 3, 4], "open": "09:20", "close": "17:30"}
    b["sentiment"]["fallback"] = "none"
    with _connect(cfg) as con:  # cripto
        db.save_ticks(con, OPEN - timedelta(minutes=2), [tick("BTC/EUR", 0.2, "HOLD")])
        equity(con, 10.0)
    with _connect(b) as con:
        db.save_ticks(con, OPEN - timedelta(minutes=4), [tick("RHM/EUR", 0.3, "BUY"),
                                                         tick("GOLD/EUR", -0.1, "HOLD")])
        equity(con, 10.5)
        db.save_headlines(con, "2026-11-02", OPEN.isoformat(), [
            Headline("Rheinmetall wins record order, shares surge", "", "reuters",
                     OPEN - timedelta(minutes=30), "https://x.test/rhm"),
            Headline("Gold rally to record high", "", "reuters", OPEN - timedelta(hours=1),
                     "https://x.test/gold")], [1.0, 1.0])
    portfolio.register([cfg, b])
    return b


def h(cfg, text, now=OPEN):
    with _connect(cfg) as con:
        return telegram.handle(con, cfg, text, now)


def test_estado_routes_to_the_stocks_db(cfg, bolsa):
    out = h(cfg, "/estado bolsa")
    assert out.split("\n")[0] == "<b>📈 Bolsa · PAPER</b> · último ciclo 09:56 (hace 4 min)"
    assert "1. RHM/EUR +0.300 BUY" in out and "BTC/EUR" not in out
    assert "Capital <b>10.50 €</b>" in out and "Próxima actualización en 11 min 00 s" in out
    assert "<i>/noticias bolsa RHM · /senales bolsa · /aprendizaje bolsa · /ayuda bolsa</i>" in out
    assert "🪙 Cripto: 10.00 € (papel) · BTC +0.200 · /estado\n" in out
    assert out.index("🪙 Cripto") < out.index("📰 Últimas noticias")
    assert "⚪  —   · " in out and "🟢" not in out  # bolsa: el léxico no puntúa
    assert h(cfg, "/ESTADO Bolsa") == out


def test_estado_cripto_is_the_main_portfolio(cfg, bolsa):
    out = h(cfg, "/estado")
    assert h(cfg, "/estado cripto") == out
    assert out.startswith("<b>newsbot · PAPER</b>") and "1. BTC/EUR +0.200 HOLD" in out
    assert "📈 Bolsa: 10.50 € (papel) · RHM +0.300 · /estado bolsa" in out
    assert "<i>/noticias BTC · /senales · /aprendizaje · /ayuda</i>" in out


def test_peer_line_skips_missing_db(cfg, bolsa, tmp_path):
    bolsa["general"]["data_dir"] = str(tmp_path / "nada")
    assert "📈 Bolsa:" not in h(cfg, "/estado")
    assert not (tmp_path / "nada").exists()  # solo lectura: no crea nada


def test_closed_market_replaces_countdown(cfg, bolsa):
    out = h(cfg, "/estado bolsa", SATURDAY)
    first, second = out.split("\n")[:2]
    assert "⚠️" not in first and second == "🔒 Mercado cerrado · abre lun 09:20"
    assert "no actualiza" not in out and "Próxima actualización" not in out
    assert "⚠️" in h(cfg, "/estado", SATURDAY).split("\n")[0]  # cripto sí avisa: no cierra


def test_moneda_routing_in_any_position(cfg, bolsa):
    a, b = h(cfg, "/moneda bolsa RHM"), h(cfg, "/moneda RHM bolsa")
    assert a == b and a.startswith("<b>RHM/EUR</b>") and "Rheinmetall wins" in a
    assert "⚪  —  " in a
    assert h(cfg, "/moneda bolsa BTC") == "Valor no soportado: RHM, GOLD"
    assert h(cfg, "/moneda bolsa") == "Elige un valor (o escribe /moneda bolsa RHM):"
    assert h(cfg, "/moneda cripto BTC").startswith("<b>BTC/EUR</b>")
    assert h(cfg, "/moneda RHM") == "Moneda no soportada: BTC, ETH, SOL, XRP, ADA"


def test_noticias_bolsa_without_lexicon_scores(cfg, bolsa):
    out = h(cfg, "/noticias bolsa")
    assert "sentimiento medio — (sin puntuar: en bolsa solo puntúa Claude)" in out
    assert out.count("⚪  —   · ") == 2 and "🟢" not in out
    assert "Gold rally" in h(cfg, "/noticias GOLD bolsa") and "Rheinmetall" not in h(cfg, "/news bolsa gold")


def test_hints_carry_the_portfolio_word(cfg, bolsa):
    assert h(cfg, "/aprobar bolsa") == "Uso: /aprobar bolsa &lt;id&gt; (número de /propuestas bolsa)"
    assert h(cfg, "/aprobar") == "Uso: /aprobar &lt;id&gt; (número de /propuestas)"
    assert h(cfg, "/deshacer bolsa x").startswith("Uso: /deshacer bolsa &lt;id&gt;")
    assert h(cfg, "/reanudar bolsa").endswith("Envía: /reanudar bolsa CONFIRMAR")
    assert h(cfg, "/pausa bolsa") == "Pausado: no habrá decisiones hasta /reanudar bolsa."
    with _connect(bolsa) as con:  # la pausa fue a la base de bolsa, no a la de cripto
        assert db.KV(con).get("paused") == "1"
    with _connect(cfg) as con:
        assert db.KV(con).get("paused") is None
    with _connect(bolsa) as con:
        with con:
            con.execute("INSERT INTO params_history(created, params_json, status, note) VALUES "
                        "('2026-11-01', ?, 'proposed', ?)", (json.dumps({"weights": {}}),
                                                             json.dumps({"origin": "learn"})))
    assert "<i>/aprobar bolsa &lt;id&gt; · /rechazar bolsa &lt;id&gt;</i>" in h(cfg, "/propuestas bolsa")


def test_senales_no_ticks_uses_tick_minutes(cfg, bolsa, tmp_path):
    bolsa["general"]["data_dir"] = str(tmp_path / "vacia")
    assert h(cfg, "/senales bolsa") == "Todavía no hay ticks: espera al próximo ciclo (15 min)."


def test_markup_buttons(cfg, bolsa):
    row = telegram._markup("/estado", cfg)["inline_keyboard"][0]
    assert [(b["text"], b["callback_data"]) for b in row] == [
        ("🔄 Actualizar", "r:/estado"), ("📈 Ver bolsa", "r:/estado bolsa")]
    row = telegram._markup("/estado bolsa", cfg)["inline_keyboard"][0]
    assert [(b["text"], b["callback_data"]) for b in row] == [
        ("🔄 Actualizar", "r:/estado bolsa"), ("🪙 Ver cripto", "r:/estado cripto")]
    coins = telegram._markup("/moneda bolsa", cfg)["inline_keyboard"][0]
    assert [(b["text"], b["callback_data"]) for b in coins] == [
        ("RHM", "r:/moneda bolsa RHM"), ("GOLD", "r:/moneda bolsa GOLD")]
    assert telegram._markup("/moneda", cfg)["inline_keyboard"][0][0]["callback_data"] == "r:/moneda BTC"
    assert len(telegram._markup("/senales bolsa", cfg)["inline_keyboard"][0]) == 1  # solo /estado cambia
    assert telegram._markup("/pausa bolsa", cfg) is None


def test_refresh_callback_edits_with_stocks_content(cfg, bolsa):
    posts = []
    cq = {"id": "c1", "data": "r:/estado bolsa", "message": {"message_id": 7, "chat": {"id": 42}}}
    with _connect(cfg) as con:
        n = telegram.poll(con, cfg, ENV, get=lambda u: {"result": [{"update_id": 1, "callback_query": cq}]},
                          post=lambda url, p: posts.append((url.rsplit("/", 1)[1], p)))
    assert n == 1 and [m for m, _ in posts] == ["answerCallbackQuery", "editMessageText"]
    edit = posts[1][1]
    assert edit["text"].startswith("<b>📈 Bolsa · PAPER</b>") and "RHM/EUR" in edit["text"]
    assert [b["callback_data"] for b in edit["reply_markup"]["inline_keyboard"][0]] == [
        "r:/estado bolsa", "r:/estado cripto"]


def test_send_prefixes_the_label(bolsa):
    sent = []
    post = lambda u, p: sent.append(p["text"])  # noqa: E731
    telegram.send("hola", ENV, post=post)
    with portfolio.active(bolsa):
        telegram.send("Compra RHM", ENV, post=post)
        telegram.send("<b>x</b>", ENV, post=post, html=True)
    telegram.send("adiós", ENV, post=post)
    assert sent == ["hola", "📈 Bolsa · Compra RHM", "📈 Bolsa · <b>x</b>", "adiós"]


def test_ayuda_extra_line_only_with_portfolios(cfg, bolsa):
    out = h(cfg, "/ayuda")
    assert out.endswith("\nAñade «bolsa» a cualquier comando para la cartera de bolsa "
                        "(p. ej. /estado bolsa).")
    portfolio.REGISTRY.clear()
    assert h(cfg, "/ayuda") == out.rsplit("\n", 1)[0]


def test_empty_registry_is_unchanged(cfg, bolsa):
    portfolio.REGISTRY.clear()
    plain = h(cfg, "/estado")
    assert h(cfg, "/estado bolsa") == plain  # sin carteras "bolsa" es un argumento más (ignorado)
    assert "Bolsa" not in plain and "Cripto" not in plain
    assert h(cfg, "/moneda bolsa").startswith("Moneda no soportada")
    assert h(cfg, "/moneda cripto").startswith("Moneda no soportada")
    assert len(telegram._markup("/estado", cfg)["inline_keyboard"][0]) == 1
    assert telegram._markup("/moneda", cfg)["inline_keyboard"][0][0]["callback_data"] == "r:/moneda BTC"


def test_learn_days_left_scales_with_session(bolsa):
    always = {k: v for k, v in bolsa.items() if k != "session"}  # misma cartera, abierta 24/7
    with _connect(bolsa) as con:
        stocks, ref = (learn.progress(con, c, OPEN)["days_left"] for c in (bolsa, always))
    assert ref == 34 and stocks == 138  # 400 / (6 × 2 × 0.243): el mercado abre ~24 % de la semana


def test_learn_hints_inside_the_stocks_portfolio(bolsa):
    with _connect(bolsa) as con:
        with con:
            pid = con.execute("INSERT INTO params_history(created, params_json, status, note) VALUES "
                              "('2026-11-01', '{}', 'proposed', ?)",
                              (json.dumps({"origin": "learn"}),)).lastrowid
        with portfolio.active(bolsa):
            txt = learn.report(con, bolsa, OPEN)
        plain = learn.report(con, bolsa, OPEN)
    assert f"/aprobar bolsa {pid} o /rechazar bolsa {pid}." in txt
    assert f"/aprobar {pid} o /rechazar {pid}." in plain
