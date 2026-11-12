"""Panel de /estado (cuenta atrás, memoria, noticias) y botón 🔄 Actualizar."""
from datetime import UTC, datetime, timedelta

from conftest import NOW

from newsbot import bot
from newsbot.bot import _connect
from newsbot.news.rss import Headline
from newsbot.reporting import telegram
from newsbot.research import learn
from newsbot.storage import db

ENV = {"TELEGRAM_BOT_TOKEN": "T0K", "TELEGRAM_CHAT_ID": "42"}
T = NOW.astimezone(UTC)


def headline(title, minutes=10, source="coindesk"):
    return Headline(title, "", source, T - timedelta(minutes=minutes),
                    "https://x.test/" + "-".join(title.lower().split()))


def tick(con, when, symbol="BTC/EUR"):
    db.save_ticks(con, when, [{"symbol": symbol, "price": 1.0, "signal": 0.1, "action": "HOLD",
                               "reason": "r", "held": False, "raw": {"news": 0.1},
                               "contributions": {"news": 0.04}}])


def test_noticias_with_headlines_without_keywords(cfg):
    """Regresión: titulares sin palabras clave (sentimiento None) rompían la media."""
    with _connect(cfg) as con:
        db.save_headlines(con, "d", "x", [headline("Bitcoin price today"),
                                          headline("Bitcoin rally to record", 20)], [1.0, 0.5])
        out = telegram.handle(con, cfg, "/noticias", T)
        assert "(1 con señal)" in out and "⚪  —" in out and "🟢" in out
        db.save_headlines(con, "d", "x", [headline("Ethereum update today")], [1.0])
        assert "sentimiento medio — (0 con señal)" in telegram.handle(con, cfg, "/noticias ETH", T)


def test_countdown_states():
    assert telegram._countdown(None, 300).startswith("⏳ Primera")
    assert telegram._countdown(100, 300) == "⏳ Próxima actualización en 3 min 20 s"
    assert telegram._countdown(330, 300) == "⏳ Actualizando ahora…"
    assert "no actualiza" in telegram._countdown(3600, 300)


def test_estado_has_countdown_memory_and_news(cfg):
    with _connect(cfg) as con:
        tick(con, NOW - timedelta(minutes=2))
        db.save_headlines(con, "d", "x", [headline("Bitcoin rally", source="reddit_bitcoin")], [0.3])
        out = telegram.handle(con, cfg, "/estado", T)
    assert "⏳ Próxima actualización en 3 min 00 s" in out
    assert "🧠 Memoria ░░░░░░░░░░ 0% (0/400 muestras a 4h)" in out
    assert "recogiendo datos · faltan ≈" in out and "1 titulares (1 de foros) · 1 lecturas" in out
    assert "📰 Últimas noticias</b> · 1 en 24 h" in out


def test_progress_bar_and_phases():
    assert learn.bar(0) == "░" * 10 and learn.bar(0.5) == "▓" * 5 + "░" * 5 and learn.bar(2) == "▓" * 10
    base = {"n_eff": 400, "min_n_eff": 400, "pct": 1.0, "days_left": 0, "horizon": "4h",
            "ticks": 5, "headlines": 2, "forums": 0}
    assert "ninguna señal predice" in learn.progress_lines(base | {"significant": []})[1]
    assert "trend, rsi" in learn.progress_lines(base | {"significant": ["trend", "rsi"]})[1]


def test_refresh_button_only_on_queries():
    assert telegram._refresh_button("/estado")["inline_keyboard"][0][0]["callback_data"] == "r:/estado"
    assert telegram._refresh_button("/NEWS  btc 5")["inline_keyboard"][0][0]["callback_data"] == \
        "r:/NEWS btc 5"
    assert telegram._refresh_button("/aprobar 3") is None and telegram._refresh_button("hola") is None
    long = telegram._refresh_button("/noticias " + "ñ" * 80)["inline_keyboard"][0][0]["callback_data"]
    assert len(long.encode()) <= 64


def poll_with(cfg, updates):
    posts = []
    with _connect(cfg) as con:
        n = telegram.poll(con, cfg, ENV, get=lambda u: {"result": updates},
                          post=lambda url, p: posts.append((url.rsplit("/", 1)[1], p)))
    return n, posts


def test_refresh_edits_the_same_message(cfg):
    cq = {"id": "c1", "data": "r:/estado", "message": {"message_id": 7, "chat": {"id": 42}}}
    n, posts = poll_with(cfg, [{"update_id": 1, "callback_query": cq}])
    assert n == 1 and [m for m, _ in posts] == ["answerCallbackQuery", "editMessageText"]
    edit = posts[1][1]
    assert edit["message_id"] == 7 and "newsbot" in edit["text"] and "actualizado" in edit["text"]
    assert edit["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == "r:/estado"


def test_refresh_ignores_other_chats_and_actions(cfg):
    other = {"id": "c", "data": "r:/estado", "message": {"message_id": 1, "chat": {"id": 9}}}
    action = {"id": "c", "data": "r:/pausa", "message": {"message_id": 1, "chat": {"id": 42}}}
    n, posts = poll_with(cfg, [{"update_id": 1, "callback_query": other},
                               {"update_id": 2, "callback_query": action}])
    assert n == 0 and [m for m, _ in posts] == ["answerCallbackQuery"] * 2
    with _connect(cfg) as con:
        assert db.KV(con).get("paused") is None


def test_command_reply_carries_refresh_button(cfg):
    now = int(datetime.now(UTC).timestamp())
    _, posts = poll_with(cfg, [{"update_id": 1, "message": {"chat": {"id": 42}, "date": now,
                                                            "text": "/senales"}}])
    assert posts[0][1]["reply_markup"]["inline_keyboard"][0][0]["text"] == "🔄 Actualizar"


def test_listener_retries_fast_after_timeout(cfg, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    waits = []

    class Stop:
        def is_set(self):
            return len(waits) >= 2

        def wait(self, s):
            waits.append(s)

    errors = iter([TimeoutError(), RuntimeError()])

    def boom(*a, **k):
        raise next(errors)

    monkeypatch.setattr(bot.telegram, "poll", boom)
    bot.listen_telegram(cfg, Stop())
    assert waits == [3, 30]


def test_same_link_with_edited_title_is_shown_once(cfg):
    with _connect(cfg) as con:
        db.save_headlines(con, "d", "x", [
            Headline("Binance Hack Leads to Outflows", "", "decrypt", T, "https://d.test/1"),
            Headline("The Binance Hack Leads to Outflows", "", "decrypt", T, "https://d.test/1"),
            Headline("Other bitcoin story", "", "decrypt", T, "")], [1.0, 1.0, 1.0])
        out = telegram.handle(con, cfg, "/noticias", T)
    assert out.count("https://d.test/1") == 1 and "Other bitcoin story" in out and "2 de 2" in out


def test_moneda_skips_internal_fields_and_shows_gap(cfg):
    """Regresión: _regime (texto) en raw rompía /moneda con un error de formato."""
    with _connect(cfg) as con:
        db.save_ticks(con, NOW, [{"symbol": "BTC/EUR", "price": 1.0, "signal": -0.25,
                                  "action": "HOLD", "reason": "r", "held": False,
                                  "contributions": {"news": -0.06},
                                  "raw": {"news": -0.15, "rsi": 0.2, "_regime": "bull",
                                          "_atr": 0.025, "_backfill": True}}])
        for cmd in ("/moneda BTC", "/coin btc", "/COIN BTC/EUR", "/coin@MiBot btc"):
            out = telegram.handle(con, cfg, cmd, T)
            assert "_regime" not in out and "rsi" in out and "sombra" in out, cmd
            assert "🎯 Para comprar BTC necesita señal ≥ +0.56" in out


def test_moneda_without_coin_offers_buttons(cfg):
    with _connect(cfg) as con:
        assert telegram.handle(con, cfg, "/coin", T).startswith("Elige una moneda")
    kb = telegram._markup("/moneda", cfg)["inline_keyboard"]
    assert [b["callback_data"] for b in kb[0]] == ["r:/moneda BTC"]  # cfg de tests: solo BTC
    assert telegram._markup("/moneda btc", cfg)["inline_keyboard"][0][0]["text"] == "🔄 Actualizar"
    assert telegram._markup("/pausa", cfg) is None


def test_coin_button_edits_message_with_coin_detail(cfg):
    cq = {"id": "c", "data": "r:/moneda BTC", "message": {"message_id": 3, "chat": {"id": 42}}}
    n, posts = poll_with(cfg, [{"update_id": 1, "callback_query": cq}])
    assert n == 1 and posts[1][0] == "editMessageText" and "<b>BTC" in posts[1][1]["text"]


def test_why_reads_telegram_description():
    import io as _io
    import urllib.error
    err = urllib.error.HTTPError("https://api.telegram.org/botSECRET/x", 400, "Bad Request", {},
                                 _io.BytesIO(b'{"ok":false,"description":"Bad Request: query is too old"}'))
    why = telegram._why(err)
    assert why == "HTTPError 400: Bad Request: query is too old" and "SECRET" not in why


def test_times_are_shown_in_local_time_with_dst(cfg):
    winter = datetime(2026, 11, 2, 16, 0, tzinfo=UTC).timestamp()   # UTC+1
    summer = datetime(2026, 7, 2, 16, 0, tzinfo=UTC).timestamp()    # UTC+2 (horario de verano)
    assert f"{telegram._local(cfg, winter):%H:%M}" == "17:00"
    assert f"{telegram._local(cfg, summer):%H:%M}" == "18:00"
