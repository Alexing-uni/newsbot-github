"""Comandos de Telegram en HTML: escape, alias, troceado, reintento en texto plano y escucha robusta.
Sin red: `post`/`get` son listas en memoria."""
import json
import re
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from newsbot.news.rss import Headline
from newsbot.reporting import telegram
from newsbot.storage import db

ENV = {"TELEGRAM_BOT_TOKEN": "T0K", "TELEGRAM_CHAT_ID": "42"}
NOW = datetime(2026, 11, 2, 8, 5, tzinfo=UTC)
EVIL = 'Bitcoin <script>alert(1)</script> & "quotes" crash'


def dbcon(cfg):
    data = Path(cfg["general"]["data_dir"])
    data.mkdir(parents=True, exist_ok=True)
    return closing(db.connect(data / "newsbot.db"))


def tick(sym, signal, action, held=False, news=0.5):
    raw = {"news": news, "trend": 0.4, "intraday": -0.2, "fng": 0.0, "votes": 0.0, "rsi": -0.3,
           "bollinger": 0.1, "momentum": 0.2, "volume": 0.0}
    return {"symbol": sym, "price": 90_000.0 if sym == "BTC/EUR" else 3_000.0, "signal": signal,
            "action": action, "reason": "señal <alta> & clara", "held": held, "raw": raw,
            "contributions": {"news": 0.4 * news, "trend": 0.12, "intraday": -0.03, "fng": 0.0}}


def seed(con, ticks=True):
    hs = [Headline("Bitcoin ETF inflows surge to record", "", "coindesk", NOW - timedelta(minutes=18),
                   "https://ex.com/a"),
          Headline(EVIL, "", "decrypt<b>", NOW - timedelta(hours=1), "http://insecure.example/x"),
          Headline("Ethereum upgrade approved", "", "cointelegraph", NOW - timedelta(hours=2),
                   "https://ex.com/e"),
          Headline("Old solana news", "", "decrypt", NOW - timedelta(hours=30), "https://ex.com/old")]
    db.save_headlines(con, "2026-11-02", NOW.isoformat(), hs, [1.0, 0.5, 0.8, 1.0])
    db.save_headlines(con, "2026-11-01", NOW.isoformat(), hs[:1], [1.0])  # mismo título, otro día
    if ticks:
        db.save_ticks(con, NOW - timedelta(minutes=20), [tick("BTC/EUR", 0.1, "HOLD")])
        db.save_ticks(con, NOW - timedelta(minutes=3), [tick("BTC/EUR", 0.31, "BUY"),
                                                        tick("ETH/EUR", -0.2, "SELL", news=-0.5)])
    with con:
        con.execute("INSERT INTO decisions(day, action, symbol, reason) VALUES "
                    "('2026-11-02', 'BUY', 'BTC/EUR', 'noticias <buenas>')")
        con.executemany("INSERT INTO orders(day, side, symbol, qty, price, eur, fee, status, created) "
                        "VALUES (?,?,?,?,?,?,?,?,?)",
                        [(f"2026-10-{d:02d}", s, "BTC/EUR", 0.0001, 90_000.0, 9.0, 0.02, st,
                          f"2026-10-{d:02d}T10:00:00+00:00")
                         for d, s, st in [(1, "buy", "filled"), (5, "sell", "filled"),
                                          (9, "buy", "cancelled")]])
        start = datetime(2026, 9, 20)
        con.executemany("INSERT INTO equity_daily(day, cash, qty, avg_entry, price, equity, peak, symbol) "
                        "VALUES (?,?,?,?,?,?,?,?)",
                        [((start + timedelta(days=i)).date().isoformat(), 1.0, 0.0001, 80_000.0,
                          90_000.0, eq, 12.0, "BTC/EUR")
                         for i, eq in enumerate([10.0] * 10 + [12.0] * 10 + [9.0] * 10 + [10.5] * 14)])


def h(con, cfg, text):
    return telegram.handle(con, cfg, text, NOW)


def test_estado_shows_news_ranking_and_escapes(cfg):
    with dbcon(cfg) as con:
        seed(con)
        out = h(con, cfg, "/estado")
    first = out.split("\n")[0]
    assert first == "<b>newsbot · PAPER</b> · último ciclo 09:02 (hace 3 min)"  # hora de Madrid, no UTC
    assert "Última decisión 2026-11-02: BUY BTC/EUR — noticias &lt;buenas&gt;" in out
    assert "Capital <b>10.50 €</b> · P&amp;L +0.50 €" in out and "Posición: BTC/EUR" in out
    assert out.index("1. BTC/EUR +0.310 BUY") < out.index("2. ETH/EUR -0.200 SELL")
    assert "<b>📰 Últimas noticias</b>" in out and out.count("ETF inflows") == 1  # sin duplicados
    assert '🟢 +1.00 · <a href="https://ex.com/a">Bitcoin ETF inflows surge to record</a>' in out
    assert "<i>coindesk · hace 18 min</i>" in out and "Old solana news" in out
    assert "<script>" not in out and "&lt;script&gt;" in out and "&quot;quotes&quot;" in out
    assert "decrypt&lt;b&gt;" in out and 'href="http://' not in out
    assert "/noticias BTC · /senales · /aprendizaje · /ayuda" in out


def test_estado_warns_when_stale_or_empty(cfg):
    with dbcon(cfg) as con:
        first = h(con, cfg, "/estado").split("\n")[0]
        assert "⚠️" in first and "Sin capital registrado" in h(con, cfg, "/estado")
        seed(con)
        late = telegram.handle(con, cfg, "/estado", NOW + timedelta(minutes=30)).split("\n")[0]
        assert late.endswith("(hace 33 min) ⚠️")
        kv = db.KV(con)
        kv.set("paused", "1")
        assert "🛑 Flags: paused" in h(con, cfg, "/estado")


def test_aliases_case_and_bot_suffix(cfg):
    with dbcon(cfg) as con:
        seed(con)
        ref = h(con, cfg, "/estado")
        assert h(con, cfg, "/STATUS@MiNewsBot") == ref == h(con, cfg, "  /Estado@x  ")
        assert h(con, cfg, "/news@bot eth") == h(con, cfg, "/NOTICIAS ETH")
        assert "Comandos" in h(con, cfg, "/start") and "Comandos" in h(con, cfg, "hola")


def test_noticias_window_coin_filter_and_limits(cfg):
    with dbcon(cfg) as con:
        seed(con)
        out = h(con, cfg, "/noticias")
        assert "Old solana news" not in out and "· 3 de 3 ·" in out  # solo 24 h
        eth = h(con, cfg, "/noticias eth")
        assert "<b>Noticias de ETH · últimas 24 h</b>" in eth and "Ethereum" in eth
        assert "Bitcoin" not in eth and "🟢 +1.00" in eth
        one = h(con, cfg, "/news BTC 1")
        assert "· 1 de 2 ·" in one and one.count("<i>") == 1
        assert "· 3 de 3 ·" in h(con, cfg, "/noticias 99")
        assert h(con, cfg, "/noticias DOGE").startswith("Moneda no soportada: BTC, ETH")
        # media ponderada por el peso guardado: (1·1 + 0,5·(-1) + 0,8·1) / 2,3
        assert f"{(1 - 0.5 + 0.8) / 2.3:+.2f}" in out
        assert "Sin titulares de SOL" in h(con, cfg, "/noticias SOL")


def test_senales(cfg):
    with dbcon(cfg) as con:
        assert h(con, cfg, "/senales") == "Todavía no hay ticks: espera al próximo ciclo (5 min)."
        seed(con)
        out = h(con, cfg, "/signals")
    assert "tick 02/11 09:02 (hace 3 min)"  # 08:02 UTC = 09:02 en Madrid in out
    assert out.count("<pre>") == 1 and out.count("</pre>") == 1
    assert "BTC    +0.310  BUY" in out and "ETH    -0.200  SELL</pre>" in out
    assert "<b>BTC/EUR</b>: news +0.200, trend +0.120, intraday -0.030" in out
    assert "compra ≥ +0.25" in out and "venta ≤ -0.15" in out


def test_moneda_detail(cfg):
    with dbcon(cfg) as con:
        seed(con)
        out = h(con, cfg, "/coin btc")
        assert "<b>BTC/EUR</b> · 90,000.0000" in out and "Señal <b>+0.310</b> → BUY" in out
        assert "Motivo: señal &lt;alta&gt; &amp; clara" in out
        assert "→ +0.200" in out and re.search(r"rsi\s*</code> -0\.300 · sombra", out)
        assert out.count("<i>") == 2 and "Ethereum" not in out  # sus titulares (2 de BTC)
        assert h(con, cfg, "/moneda DOGE").startswith("Moneda no soportada")
        assert h(con, cfg, "/moneda").startswith("Elige una moneda")  # + botones
        assert "Todavía no hay ticks de esta moneda" in h(con, cfg, "/moneda SOL")


def test_aprendizaje_uses_effective_cfg(cfg, monkeypatch):
    with dbcon(cfg) as con:
        assert "no válido" in h(con, cfg, "/aprendizaje 2h")
        learn = pytest.importorskip("newsbot.research.learn")
        calls = []
        monkeypatch.setattr(learn, "report", lambda c, k, n, hz: calls.append((k, n, hz)) or "<b>ok</b>")
        with con:
            con.execute("INSERT INTO params_history(created, params_json, status) VALUES "
                        "('x', ?, 'applied')", (json.dumps({"strategy": {"buy_threshold": 0.33}}),))
        assert h(con, cfg, "/learn 24H") == "<b>ok</b>"
        assert h(con, cfg, "/aprendizaje") == "<b>ok</b>"
    assert calls[0][0]["strategy"]["buy_threshold"] == 0.33 and calls[0][1:] == (NOW, "24h")
    assert calls[1][2] is None and cfg["strategy"]["buy_threshold"] != 0.33


def test_operaciones_and_rendimiento(cfg):
    with dbcon(cfg) as con:
        assert h(con, cfg, "/trades") == "Todavía no hay operaciones."
        assert "todavía no hay datos" in h(con, cfg, "/rendimiento")
        seed(con)
        ops = h(con, cfg, "/operaciones 2")
        assert "Últimas 2 operaciones" in ops and "2026-10-01" not in ops
        assert "2026-10-09 10:00 · <b>BUY</b> BTC/EUR 0.00010000 @ 90,000.0000 = 9.00 €" in ops
        assert "comisión 0.0200 € · cancelled" in ops
        assert "Últimas 3 operaciones" in h(con, cfg, "/operaciones 999")
        out = h(con, cfg, "/profit")
    assert "Capital 10.50 € · inicial 10.00 € · +0.50 € (+5.0%)" in out
    assert "7 d: +0.00 € (+0.0%) · 30 d: -1.50 € (-12.5%)" in out  # el 3-oct: 12,0
    assert "Máx. drawdown: -25.0% · ahora -12.5%" in out
    assert "Órdenes llenadas: 2 · comisiones: 0.0400 €" in out
    assert "Rentabilidad pasada no garantiza la futura." in out


def test_propuestas_aprobar_rechazar(cfg):
    with dbcon(cfg) as con:
        assert h(con, cfg, "/propuestas").startswith("No hay propuestas pendientes.")
        bad = {"weights": {"news": 0.9}}  # no suma 1
        with con:
            con.executemany("INSERT INTO params_history(created, params_json, status, note) VALUES "
                            "(?, ?, 'proposed', ?)",
                            [("2026-11-01T10:00", json.dumps({"strategy": {"buy_threshold": 0.3}}),
                              json.dumps({"origin": "aprendizaje <4h>"})),
                             ("2026-11-02T10:00", '{"strategy": {"sell_threshold": -0.2}}', None),
                             ("2026-11-02T11:00", json.dumps(bad), "texto libre")])
        out = h(con, cfg, "/proposals")
        assert ("#1 · 2026-11-01 · origen aprendizaje &lt;4h&gt;: "
                "<code>strategy.buy_threshold=0.3</code>") in out
        assert "#3" in out and "origen texto libre" in out and "/aprobar &lt;id&gt;" in out
        assert h(con, cfg, "/aprobar abc").startswith("Uso: /aprobar")
        assert h(con, cfg, "/aprobar 1 2").startswith("Uso")
        assert "No hay" in h(con, cfg, "/aprobar 99")
        assert "aplicada" in h(con, cfg, "/APPROVE 1") and "próximo tick" in h(con, cfg, "/aprobar 2")
        # las aplicadas se acumulan: la #2 no deshace el umbral de compra de la #1
        assert db.active_params(con) == (2, {"strategy": {"buy_threshold": 0.3, "sell_threshold": -0.2}})
        assert "no es válida" in h(con, cfg, "/aprobar 3") and db.active_params(con)[0] == 2
        assert "rechazada" in h(con, cfg, "/reject 3") and "No hay" in h(con, cfg, "/rechazar 3")
        assert con.execute("SELECT status FROM params_history WHERE id = 3").fetchone()[0] == "rejected"
        assert "No hay" in h(con, cfg, "/rechazar 1")  # ya aplicada


def test_pausa_reanudar_informe_ayuda(cfg):
    with dbcon(cfg) as con:
        kv = db.KV(con)
        assert "/reanudar" in h(con, cfg, "/pausa") and kv.get("paused") == "1"
        kv.set("kill_switch", "1")
        assert "CONFIRMAR" in h(con, cfg, "/reanudar") and kv.get("paused") == "1"
        assert "CONFIRMAR" in h(con, cfg, "/reanudar confirmar") and kv.get("kill_switch") == "1"
        assert "Reanudado" in h(con, cfg, "/RESUME CONFIRMAR")
        assert kv.get("paused") is None and kv.get("kill_switch") is None
        assert "todavía no hay datos" in h(con, cfg, "/informe")
        seed(con)
        assert "Informe semanal" in h(con, cfg, "/report") and "→" in h(con, cfg, "/informe")
        help_ = h(con, cfg, "/ayuda")
    assert "Comandos" in help_ and "/aprobar &lt;id&gt;" in help_
    for name, (*_, alias) in telegram.COMMANDS.items():
        assert f"/{name}" in help_ and f"(/{alias})" in help_
    assert len(help_.split("\n")) == len(telegram.COMMANDS) + 1


# --- envío ------------------------------------------------------------------------------------
def test_send_html_chunks_and_pre_blocks():
    sent = []
    text = "\n".join(f"<b>línea {i}</b> " + "x" * 90 for i in range(100))
    text += "\n<pre>" + "\n".join("fila " + "y" * 60 for _ in range(80)) + "</pre>"
    assert telegram.send(text, ENV, post=lambda u, p: sent.append(p), html=True, silent=True)
    assert len(sent) >= 3 and all(len(p["text"]) <= 3800 for p in sent)
    assert all(p["parse_mode"] == "HTML" and p["link_preview_options"] == {"is_disabled": True}
               and p["disable_notification"] for p in sent)
    for p in sent:  # cada trozo tiene sus etiquetas equilibradas
        assert p["text"].count("<pre>") == p["text"].count("</pre>")
        assert p["text"].count("<b>") == p["text"].count("</b>")
    assert "\n".join(p["text"] for p in sent).replace("</pre>\n<pre>", "\n") == text
    assert telegram.chunks("hola") == ["hola"] and len(telegram.chunks("z" * 9000)) == 3


def test_send_html_falls_back_to_plain_text():
    sent = []

    def post(u, p):
        sent.append(p)
        if "parse_mode" in p:
            raise OSError("400 can't parse entities")

    assert telegram.send("<b>a &amp; b</b> &lt;x&gt;", ENV, post=post, html=True) is True
    assert sent[1] == {"chat_id": "42", "text": "a & b <x>"}

    def down(u, p):
        raise OSError("down")

    assert telegram.send("<b>x</b>", ENV, post=down, html=True) is False


def test_poll_isolates_errors_and_ignores_non_text(cfg, monkeypatch):
    updates = {"result": [
        {"update_id": 10, "message": {"chat": {"id": 42}, "text": "/boom"}},
        {"update_id": 11, "message": {"chat": {"id": 42}, "photo": [{}]}},
        {"update_id": 12, "message": {"chat": {"id": 42}, "text": "/ayuda"}},
        {"update_id": 13, "edited_message": {"chat": {"id": 42}, "text": "/pausa"}},
    ]}
    real = telegram.handle

    def handle(con, cfg, text, now=None):
        if text == "/boom":
            raise RuntimeError("secreto")
        return real(con, cfg, text, now)

    monkeypatch.setattr(telegram, "handle", handle)
    urls, sent = [], []
    with dbcon(cfg) as con:
        n = telegram.poll(con, cfg, ENV, get=lambda u: urls.append(u) or updates,
                          post=lambda u, p: sent.append(p))
        assert n == 2 and db.KV(con).get("tg:offset") == "14" and db.KV(con).get("paused") is None
    assert "allowed_updates=%5B%22message%22%2C+%22callback_query%22%5D&offset=0&timeout=0" in urls[0]
    assert sent[0]["text"] == telegram.ERROR and "Comandos" in sent[1]["text"]
    assert all(p["parse_mode"] == "HTML" for p in sent)


def test_register_commands_payload():
    calls = []
    assert telegram.register_commands(ENV, post=lambda u, p: calls.append((u, p))) is True
    url, payload = calls[0]
    assert url == "https://api.telegram.org/botT0K/setMyCommands"
    assert payload["scope"] == {"type": "chat", "chat_id": 42}
    names = [c["command"] for c in payload["commands"]]
    assert names == list(telegram.COMMANDS) and all(re.fullmatch(r"[a-z0-9_]{1,32}", n) for n in names)
    assert all(0 < len(c["description"]) <= 256 for c in payload["commands"])
    assert telegram.register_commands({}) is False

    def boom(u, p):
        raise OSError("down")

    assert telegram.register_commands(ENV, post=boom) is False
