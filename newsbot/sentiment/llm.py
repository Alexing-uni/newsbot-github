import hashlib
import re

import anthropic
from pydantic import BaseModel, Field, ValidationError

from newsbot.news.rss import Headline
from newsbot.sentiment import Sentiment

FORMAT = """Responde SOLO con JSON válido, sin texto extra:
{"score": <número entre -1 y 1>, "confidence": <número entre 0 y 1>, \
"drivers": [<máx. 3 factores clave>], "reason": "<menos de 25 palabras>"}"""

PROMPT = """Eres un analista de sentimiento de mercado. Lee los titulares de las últimas 24 h \
y estima su impacto probable en el precio de {asset} en el próximo día.
Cada titular lleva un peso (fiabilidad de la fuente × recencia): da más importancia a los de más peso.
No des recomendaciones de compra o venta.
"""

# solo con descripción del activo (bolsa): el prompt de cripto no cambia
GUIDE = ("Horizonte: los próximos 1-3 días. Un mismo titular puede mover activos distintos en "
         "direcciones opuestas (p. ej. una escalada bélica suele subir defensa y oro y bajar el "
         "mercado).\n")

# idea: TradingAgents (arXiv 2412.20138), investigadores alcista y bajista que debaten
SIDE = """Eres un analista {side}. Con estos titulares, da el mejor argumento {side} posible para el \
precio de {asset} en el próximo día, en menos de 120 palabras.

Titulares:
{headlines}"""

VERDICT = PROMPT + """
Has escuchado a dos analistas. Pondera sus argumentos con los titulares.
Argumento alcista: {bull}
Argumento bajista: {bear}
"""

# idea: Glasserman y Lin (2023), anonimizar entidades reduce el "efecto distracción"
ENTITIES = ["ethereum", "ether", "eth", "solana", "sol", "xrp", "ripple", "cardano", "ada",
            "dogecoin", "doge", "binance", "bnb", "coinbase", "kraken", "okx", "bybit", "tether",
            "usdt", "usdc", "circle", "blackrock", "microstrategy", "strategy", "tesla", "ftx"]
NAMES = {"BTC": "Bitcoin", "ETH": "Ethereum", "SOL": "Solana", "XRP": "XRP", "ADA": "Cardano"}
_ENTITY_RE = re.compile(r"\b(" + "|".join(ENTITIES) + r")\b", re.IGNORECASE)


class LLMSentiment(BaseModel):
    score: float = Field(ge=-1, le=1)
    confidence: float = Field(ge=0, le=1)
    drivers: list[str] = Field(default_factory=list, max_length=3)
    reason: str = ""


def anonymize_text(text: str, asset_re: re.Pattern) -> str:
    """Si el titular no trata del activo operado, cambia entidades concretas por placeholders."""
    if asset_re.search(text):
        return text
    names: dict[str, str] = {}
    return _ENTITY_RE.sub(lambda m: names.setdefault(m.group(0).lower(),
                                                     f"Entidad {chr(65 + len(names))}"), text)


# $ por millón de tokens (entrada, salida), para estimar el gasto en /estado
PRICES = {"claude-haiku-5-5": (0.10, 0.50)}


def _text(client, model: str, prompt: str, usage: dict | None = None) -> str:
    # Sin temperature: la API la rechaza en los modelos actuales; la caché da el determinismo.
    msg = client.messages.create(model=model, max_tokens=2048, output_config={"effort": "low"},
                                 messages=[{"role": "user", "content": prompt}])
    if usage is not None:
        u = getattr(msg, "usage", None)
        usage["calls"] = usage.get("calls", 0) + 1
        usage["in"] = usage.get("in", 0) + (getattr(u, "input_tokens", 0) or 0)
        usage["out"] = usage.get("out", 0) + (getattr(u, "output_tokens", 0) or 0)
    if msg.stop_reason == "refusal":
        raise RuntimeError("el modelo rechazó la petición")
    return "".join(b.text for b in msg.content if b.type == "text")


def _parse(raw: str) -> LLMSentiment:
    return LLMSentiment.model_validate_json(raw[raw.index("{"): raw.rindex("}") + 1])


def analyze(headlines: list[Headline], model: str, weights: list[float], *, debate: bool = False,
            anonymize: bool = True, cache=None, asset: str = "BTC",
            asset_words: tuple[str, ...] = ("bitcoin", "btc"), usage: dict | None = None,
            description: str | None = None) -> Sentiment:
    """Sentimiento de los titulares sobre `asset`; los que no lo mencionan se anonimizan.
    `description` ([market.CODE].name, bolsa): qué es el activo y qué lo mueve; añade la guía de
    horizonte. Sin ella el prompt es el de siempre (cripto)."""
    name = description or NAMES.get(asset, asset)
    extra = GUIDE if description else ""
    asset_re = re.compile(r"\b(" + "|".join(map(re.escape, asset_words)) + r")\b", re.IGNORECASE)
    lines = []
    for h, w in zip(headlines, weights, strict=True):
        text = f"{h.title}: {h.summary[:300]}"
        lines.append(f"- (peso {w:.2f}) [{h.source}] "
                     f"{anonymize_text(text, asset_re) if anonymize else text}")
    body = "\n".join(lines)
    key = "llm:" + hashlib.sha256((f"{model}|{debate}|{asset}|{body}"
                                   + (f"|{description}" if description else "")).encode()).hexdigest()
    method = f"llm:{model}" + (":debate" if debate else "")
    if cache is not None and (hit := cache.get(key)):
        r = LLMSentiment.model_validate_json(hit)
        return Sentiment(r.score, r.confidence, r.reason[:200], method)

    client = anthropic.Anthropic()
    if debate:  # triplica el coste: 2 argumentos + veredicto
        bull = _text(client, model, SIDE.format(side="alcista", asset=name, headlines=body), usage)
        bear = _text(client, model, SIDE.format(side="bajista", asset=name, headlines=body), usage)
        prompt = (VERDICT.format(asset=name, bull=bull, bear=bear) + extra + FORMAT
                  + "\n\nTitulares:\n" + body)
    else:
        prompt = PROMPT.format(asset=name) + extra + FORMAT + "\n\nTitulares:\n" + body
    try:
        r = _parse(_text(client, model, prompt, usage))
    except (ValueError, ValidationError):  # un reintento si el JSON no es válido
        r = _parse(_text(client, model, prompt + "\n\nTu respuesta anterior no era JSON válido.",
                         usage))
    if cache is not None:
        cache.set(key, r.model_dump_json())
    return Sentiment(r.score, r.confidence, r.reason[:200], method)
