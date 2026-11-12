"""Noticias para la cartera de bolsa: Google News, timeout de los feeds, exclusiones y CryptoPanic."""
import io
import logging

from conftest import NOW, RSS

from newsbot.news import cryptopanic, ingest, rss

GN = """<?xml version="1.0"?><rss version="2.0"><channel><title>g</title>
<item><title>Rheinmetall wins ‘huge’ Bundeswehr order - Reuters</title>
<link>https://news.google.com/rss/articles/abc</link>
<description>&lt;a href="x"&gt;Rheinmetall wins order&lt;/a&gt; Reuters</description>
<source url="https://www.reuters.com">Reuters</source>
<pubDate>Mon, 02 Nov 2026 06:00:00 GMT</pubDate></item>
<item><title>Rheinmetall shares: what's next - ad-hoc-news.de</title>
<link>https://news.google.com/rss/articles/def</link>
<source url="https://www.ad-hoc-news.de">ad-hoc-news.de</source>
<pubDate>Mon, 02 Nov 2026 07:00:00 GMT</pubDate></item>
</channel></rss>"""


def test_google_news_uses_publisher_and_strips_title():
    hs = rss.fetch({"gn_defense": GN})
    assert [h.source for h in hs] == ["reuters", "ad-hoc-news.de"]
    assert hs[0].title == "Rheinmetall wins 'huge' Bundeswehr order"  # sin " - Reuters", comillas rectas
    assert hs[0].summary == "" and hs[0].url == "https://news.google.com/rss/articles/abc"
    # sin prefijo gn_ también se detecta (enlace de news.google.com con <source>)
    assert [h.source for h in rss.fetch({"otro": GN})] == ["reuters", "ad-hoc-news.de"]


def test_regular_feeds_are_unchanged():
    hs = rss.fetch({"a": RSS})
    assert {h.source for h in hs} == {"a"}
    assert hs[0].title == "Bitcoin ETF inflows surge to record" and hs[0].summary == "Big day"
    other = GN.replace("https://news.google.com/rss/articles", "https://example.com")
    assert {h.source for h in rss.fetch({"x": other})} == {"x"}  # <source> fuera de Google: se ignora


class Resp(io.BytesIO):
    headers = {"Content-Type": "application/rss+xml; charset=utf-8"}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_http_feeds_are_fetched_with_timeout(monkeypatch, caplog):
    seen = []

    def urlopen(req, timeout):
        seen.append((req.full_url, req.get_header("User-agent"), timeout))
        if "down" in req.full_url:
            raise TimeoutError("timed out")
        return Resp(GN.encode())
    monkeypatch.setattr(rss.urllib.request, "urlopen", urlopen)
    with caplog.at_level(logging.WARNING):
        hs = rss.fetch({"down": "https://down.example/rss", "gn_x": "https://news.google.com/rss/search?q=x",
                        "inline": RSS})
    assert seen == [("https://down.example/rss", rss.AGENT, 15),
                    ("https://news.google.com/rss/search?q=x", rss.AGENT, 15)]  # el XML en línea no
    assert "down" in caplog.text  # un feed colgado se avisa y se salta
    assert {h.source for h in hs} == {"reuters", "ad-hoc-news.de", "inline"}


def test_exclude_phrases_and_zero_weight_sources(cfg):
    cfg["news"]["feeds"] = {"a": RSS, "gn_x": GN}
    cfg["news"]["assets"]["BTC"] += ["rheinmetall"]
    assert {h.source for h in ingest.collect(cfg, NOW)} == {"a", "reuters", "ad-hoc-news.de"}
    cfg["news"]["source_weights"]["ad-hoc-news.de"] = 0
    cfg["news"]["exclude"] = ["ETF INFLOWS"]  # sin distinguir mayúsculas
    assert [h.source for h in ingest.collect(cfg, NOW)] == ["reuters"]


def test_cryptopanic_only_with_plan(cfg, monkeypatch):
    monkeypatch.setenv("CRYPTOPANIC_TOKEN", "t")
    calls = []
    monkeypatch.setattr(cryptopanic, "fetch", lambda *a: calls.append(a) or [])
    ingest.collect(cfg, NOW)
    assert calls and calls[0][1] == "growth"  # cripto: igual que antes
    calls.clear()
    del cfg["news"]["cryptopanic_plan"]  # bolsa: no hay plan -> no se llama
    ingest.collect(cfg, NOW)
    assert calls == []
