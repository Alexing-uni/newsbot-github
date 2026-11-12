import html
import logging
import re
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urlparse

import feedparser

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Headline:
    title: str
    summary: str
    source: str
    published: datetime  # UTC
    url: str = ""
    votes: tuple[int, int, int] = (0, 0, 0)  # CryptoPanic: positive, negative, important


def clean(text: str | None) -> str:
    text = re.sub(r"<[^>]+>", " ", html.unescape(text or "")).replace("’", "'").replace("‘", "'")
    return " ".join(text.split())


AGENT = "newsbot/0.1 (bot personal de investigacion)"  # Reddit rechaza el user-agent por defecto
TIMEOUT = 15  # feedparser no tiene timeout: un feed colgado pararía el tick


def _download(url: str):
    """Bytes del feed (con timeout) y sus cabeceras; lo que no es http(s) (XML en los tests) se pasa
    tal cual a feedparser."""
    if not url.startswith(("http://", "https://")):
        return url, {}
    req = urllib.request.Request(url, headers={"User-Agent": AGENT})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return r.read(), {"content-type": r.headers.get("Content-Type", "")}


def _google(feed: str, e) -> str | None:
    """Medio que publica una noticia de Google News ("reuters"), o None si no viene de ahí."""
    pub = clean((e.get("source") or {}).get("title"))
    if feed.startswith("gn_") or (pub and urlparse(e.get("link", "")).hostname == "news.google.com"):
        return pub.lower() or None
    return None


def fetch(feeds: dict[str, str]) -> list[Headline]:
    """Todas las entradas con fecha de los feeds; el filtrado se hace en news.ingest.

    Google News: la fuente es el medio (para que source_weights y watch.min_sources cuenten medios),
    se quita el " - Medio" final del título y el resumen (solo repite el título)."""
    out: list[Headline] = []

    def get(url):
        try:
            return _download(url)
        except Exception as exc:  # un feed caído no para el bot
            return exc

    with ThreadPoolExecutor(max_workers=min(8, len(feeds) or 1)) as pool:  # descargas en paralelo
        downloads = dict(zip(feeds, pool.map(get, feeds.values()), strict=True))
    for source in feeds:
        if isinstance(got := downloads[source], Exception):
            log.warning("Feed %s falló (%s)", source, got)
            continue
        data, headers = got
        parsed = feedparser.parse(data, agent=AGENT, response_headers=headers or None)
        if not parsed.entries and (parsed.get("bozo") or parsed.get("status", 200) >= 400):
            log.warning("Feed %s sin entradas (status %s, %s)", source, parsed.get("status"),
                        parsed.get("bozo_exception"))
        for e in parsed.entries:
            ts = e.get("published_parsed") or e.get("updated_parsed")
            if not ts:
                continue
            title, summary, src = clean(e.get("title")), clean(e.get("summary")), source
            if pub := _google(source, e):
                head, sep, tail = title.rpartition(" - ")
                title, summary, src = (head if sep and tail.lower() == pub else title), "", pub
            out.append(Headline(title, summary, src, datetime(*ts[:6], tzinfo=UTC), e.get("link", "")))
    return out
