"""CryptoPanic API v2: https://cryptopanic.com/api/{plan}/v2/posts/ (plan: growth, growth_weekly,
enterprise; el antiguo plan gratuito 'developer' devuelve 404 desde 2026). Token en CRYPTOPANIC_TOKEN.
"""
from datetime import datetime
from urllib.parse import urlencode

from newsbot.http import get_json
from newsbot.news.rss import Headline, clean

API = "https://cryptopanic.com/api/{plan}/v2/posts/?"


def fetch(token: str, plan: str = "growth", currencies: str = "BTC", get=get_json) -> list[Headline]:
    data = get(API.format(plan=plan) + urlencode(
        {"auth_token": token, "public": "true", "currencies": currencies, "kind": "news"}))
    if data.get("status") == "api_error":
        raise RuntimeError(f"CryptoPanic: {data.get('info')}")
    out = []
    for p in data.get("results", []):
        v = p.get("votes") or {}
        out.append(Headline(
            clean(p.get("title")), clean(p.get("description")), "cryptopanic",
            datetime.fromisoformat(p["published_at"].replace("Z", "+00:00")),
            p.get("original_url") or p.get("url", ""),
            (int(v.get("positive", 0)), int(v.get("negative", 0)), int(v.get("important", 0))),
        ))
    return out
