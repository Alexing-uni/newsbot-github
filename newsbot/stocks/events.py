"""Eventos con fecha (presentaciones, lanzamientos, resultados...) de la cartera de bolsa.

Cada `refresh_hours` Claude lee los titulares y apunta en la tabla events los eventos programados de
los valores del universo. `score` los convierte en el componente "events" de la señal: sube en los
`pre_days` días previos (compra el rumor) y se da la vuelta `post_days` días después (vende la noticia).
"""
import html
import logging
import os
import sqlite3
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import anthropic
from pydantic import BaseModel, Field, ValidationError, ValidationInfo, field_validator

from newsbot.config import asset
from newsbot.news import ingest
from newsbot.storage import db

log = logging.getLogger(__name__)

# Titulares que probablemente anuncian un evento con fecha: van primero en la lista para Claude
KEYWORDS = ["keynote", "unveil", "unveils", "unveiling", "launch event", "launch date", "release date",
            "set to launch", "set to release", "pre-order", "pre-orders", "preorder", "earnings",
            "earnings date", "earnings call", "results", "quarterly results", "guidance", "investor day",
            "capital markets day", "analyst day", "shareholder meeting", "annual general meeting", "gtc",
            "wwdc", "computex", "ces", "showcase", "summit", "deadline", "delay", "delayed", "postponed",
            "presentación", "presentará", "lanzamiento", "fecha de lanzamiento", "estreno", "resultados",
            "junta de accionistas", "cumbre", "retraso", "aplazado", "aplaza", "gta 6", "gta vi",
            "grand theft auto"]
KINDS = ("keynote", "product_launch", "game_release", "earnings", "investor_day", "regulatory", "other")
EFFECTS = {"up": 1, "down": -1, "mixed": 0}
ARROWS = {"up": "⬆️", "down": "⬇️", "mixed": "↔️"}
WINDOW_HOURS = 72    # titulares que se revisan
MAX_HEADLINES = 80   # como mucho, por llamada
DAYS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
DEFAULTS = {"enabled": False, "refresh_hours": 4, "lookahead_days": 30, "pre_days": 7, "post_days": 3,
            "llm_max_calls_day": 8}

PROMPT = """Eres un analista de eventos bursátiles. Hoy es {weekday} {today} (hora de Madrid).
Lee los titulares y apunta los EVENTOS PROGRAMADOS que puedan mover alguno de estos valores entre hoy y \
el {until} ({days} días):
{universe}

Reglas:
- Solo eventos con fecha concreta o claramente implícita ("el próximo martes" o "mañana" se cuentan desde \
la fecha del titular). No inventes ni aproximes fechas: "a finales de año", "en primavera" o "pronto" no \
valen. Sin fecha no hay evento.
- Solo eventos de hoy en adelante y como mucho hasta el {until}.
- Usa solo los códigos de la lista, una entrada por valor. Incluye también a los beneficiarios indirectos \
con menos confianza (p. ej. el lanzamiento de un gran videojuego: la editora y, si procede, el minorista \
que lo vende).
- effect: efecto probable sobre el precio DE ESE VALOR en los días previos al evento: "up", "down" o \
"mixed".
- Resultados (earnings): "mixed" salvo que la guía o las expectativas estén claramente sesgadas.
- Los eventos de producto (keynotes, presentaciones, lanzamientos) suelen ser "compra el rumor, vende la \
noticia": suben antes y se dan la vuelta después.
- confidence (0 a 1): lo segura que es la fecha y el efecto.
- title: corto y en español (p. ej. "Lanzamiento de GTA VI"); why: menos de 20 palabras, en español.
- Si según los titulares un evento ya registrado se ha retrasado, adelantado o cancelado, pon su id en \
"obsolete" (y, si tiene fecha nueva dentro del plazo, añádelo como evento nuevo).
"""

FORMAT = """Responde SOLO con JSON válido, sin texto extra:
{"events": [{"code": "<código>", "kind": "keynote|product_launch|game_release|earnings|investor_day|\
regulatory|other", "title": "<texto>", "date": "YYYY-MM-DD", "effect": "up|down|mixed", \
"confidence": <0 a 1>, "why": "<texto>"}], "obsolete": [<ids>]}
Si no hay ninguno: {"events": [], "obsolete": []}"""

RETRY = "\n\nTu respuesta anterior no era JSON válido."


class Event(BaseModel):
    """Un evento tal como lo devuelve Claude; lo dudoso se normaliza en vez de descartarlo."""
    code: str
    kind: str = "other"
    title: str = ""
    date: date
    effect: str = "mixed"  # sin efecto: "mixed" (aporta 0), lo normal en resultados
    confidence: float = 0.5
    why: str = ""

    @field_validator("code", mode="before")
    @classmethod
    def _code(cls, v):
        return asset(str(v).strip().upper())

    @field_validator("kind", "effect", mode="before")
    @classmethod
    def _choice(cls, v, info: ValidationInfo):
        v, kind = str(v or "").strip().lower(), info.field_name == "kind"
        return v if v in (KINDS if kind else EFFECTS) else "other" if kind else "mixed"

    @field_validator("confidence", mode="before")
    @classmethod
    def _confidence(cls, v):
        return min(1.0, max(0.0, float(v)))

    @field_validator("title", "why", mode="before")
    @classmethod
    def _text(cls, v):
        return " ".join(str(v or "").split())[:200]


class Answer(BaseModel):
    events: list[dict] = Field(default_factory=list)  # cada uno se valida aparte: uno malo no tira el resto
    obsolete: list[int] = Field(default_factory=list)


def _settings(cfg: dict) -> dict:
    return DEFAULTS | (cfg.get("events") or {})


def _tz(cfg: dict) -> ZoneInfo:
    return ZoneInfo(cfg["general"].get("timezone") or (cfg.get("session") or {}).get("tz") or "Europe/Madrid")


def _today(cfg: dict, now: datetime) -> date:
    """Fecha de Madrid (general.timezone): los días hasta el evento se cuentan en hora local."""
    return now.astimezone(_tz(cfg)).date()


def _universe(cfg: dict) -> list[str]:
    return [asset(s) for s in cfg["general"]["symbols"]]


def _when(v) -> datetime | None:
    try:
        d = v if isinstance(v, datetime) else datetime.fromisoformat(str(v))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=UTC)


def _get(h, key: str):
    """Campo de un titular: Headline (atributos) o fila de la tabla headlines."""
    if hasattr(h, key):
        return getattr(h, key)
    try:
        return h[key]
    except (KeyError, IndexError, TypeError):
        return None


def _calls_key(cfg: dict, now: datetime) -> str:
    return f"events:calls:{_today(cfg, now).isoformat()}"


def _calls(kv: db.KV, cfg: dict, now: datetime) -> int:
    return int(kv.get(_calls_key(cfg, now)) or 0)


def due(con: sqlite3.Connection, cfg: dict, now: datetime) -> bool:
    """Toca buscar eventos: activados, con ANTHROPIC_API_KEY, pasadas `refresh_hours` desde la última
    búsqueda (KV events:last) y con presupuesto hoy (KV events:calls:<día> < llm_max_calls_day)."""
    s = _settings(cfg)
    if not s["enabled"] or not os.environ.get("ANTHROPIC_API_KEY"):
        return False
    kv = db.KV(con)
    last = _when(kv.get("events:last") or "")
    if last and timedelta(0) <= now - last < timedelta(hours=s["refresh_hours"]):
        return False
    return _calls(kv, cfg, now) < s["llm_max_calls_day"]


def _pick(cfg: dict, now: datetime, headlines) -> list[tuple[str, str, str, datetime]]:
    """Titulares de las últimas 72 h que mencionan KEYWORDS (primero) o alguna empresa del universo
    (palabras de news.assets), sin repetir título; como mucho MAX_HEADLINES, del más reciente al más
    antiguo. Devuelve (título, resumen, fuente, publicado)."""
    since, kw = now - timedelta(hours=WINDOW_HOURS), ingest.pattern(KEYWORDS)
    words = sorted({w for c in _universe(cfg) for w in cfg["news"].get("assets", {}).get(c, [])})
    company = ingest.pattern(words) if words else None
    seen, ranked = set(), []
    for h in headlines:
        title, summary = (_get(h, "title") or "").strip(), _get(h, "summary") or ""
        pub = _when(_get(h, "published") or "")
        if not title or pub is None or not since <= pub <= now or title.lower() in seen:
            continue
        text = f"{title} {summary}"
        hit = bool(kw.search(text))
        if hit or (company and company.search(text)):
            seen.add(title.lower())
            ranked.append((not hit, -pub.timestamp(), (title, summary, _get(h, "source") or "?", pub)))
    picked = [x[2] for x in sorted(ranked, key=lambda x: x[:2])[:MAX_HEADLINES]]
    return sorted(picked, key=lambda x: x[3], reverse=True)


def _prompt(cfg: dict, now: datetime, picked: list, known: list) -> str:
    s, today, tz = _settings(cfg), _today(cfg, now), _tz(cfg)
    market = cfg.get("market") or {}
    universe = "\n".join(f"- {c}: {(market.get(c) or {}).get('name') or c}" for c in _universe(cfg))
    head = PROMPT.format(weekday=DAYS[today.weekday()], today=today.isoformat(), days=s["lookahead_days"],
                         until=(today + timedelta(days=s["lookahead_days"])).isoformat(), universe=universe)
    from newsbot.stocks import history  # reacciones medidas en eventos pasados del mismo tipo
    if past := history.prompt_lines(cfg):
        head += ("\n\nReacciones históricas medidas (pocas muestras: úsalas para calibrar la confianza, no "
                 "como regla):\n" + past)
    reg = "\n".join(f"- {r['id']} · {r['code']} · {r['kind']} · {r['date']} · {r['title']}" for r in known)
    news = []
    for title, summary, source, pub in picked:
        d = pub.astimezone(tz).date()
        news.append(f"- [{DAYS[d.weekday()][:3]} {d.isoformat()}] [{source}] {title}"
                    + (f": {summary[:300]}" if summary else ""))
    return (head + FORMAT + "\n\nEventos ya registrados (id · código · tipo · fecha · título):\n"
            + (reg or "ninguno") + "\n\nTitulares ([fecha de publicación] [fuente]):\n" + "\n".join(news))


def _ask(client, model: str, prompt: str, kv: db.KV, cfg: dict, now: datetime) -> str:
    # cuenta antes de llamar: un fallo de red también gasta presupuesto (no se insiste cada tick)
    kv.set(_calls_key(cfg, now), str(_calls(kv, cfg, now) + 1))
    # Sin temperature: la API la rechaza en los modelos actuales.
    msg = client.messages.create(model=model, max_tokens=4096, output_config={"effort": "low"},
                                 messages=[{"role": "user", "content": prompt}])
    if msg.stop_reason == "refusal":
        raise RuntimeError("el modelo rechazó la petición")
    return "".join(b.text for b in msg.content if b.type == "text")


def _parse(raw: str) -> Answer:
    return Answer.model_validate_json(raw[raw.index("{"): raw.rindex("}") + 1])


def _save(con: sqlite3.Connection, cfg: dict, now: datetime, ans: Answer, known_ids: set[int],
          source: str) -> int:
    """Valida cada evento (código del universo, fecha posible entre hoy y lookahead_days) y hace upsert
    por (code, kind, date). Devuelve cuántos son nuevos, han cambiado o se han retirado."""
    s, today, universe = _settings(cfg), _today(cfg, now), set(_universe(cfg))
    until, best = today + timedelta(days=s["lookahead_days"]), {}
    for item in ans.events:
        try:
            ev = Event.model_validate(item)
        except (ValueError, TypeError) as exc:  # fecha imposible, confianza no numérica...
            log.info("Eventos: descarto %r (%s)", item, type(exc).__name__)
            continue
        if ev.code not in universe or not today <= ev.date <= until:
            log.info("Eventos: descarto %s %s (%s)", ev.code, ev.date, "código desconocido"
                     if ev.code not in universe else "fecha fuera de plazo")
            continue
        key = (ev.code, ev.kind, ev.date.isoformat())
        if key not in best or ev.confidence > best[key].confidence:
            best[key] = ev
    n, gone = 0, [i for i in dict.fromkeys(ans.obsolete) if i in known_ids]
    with con:
        if gone:  # retrasados o cancelados (solo los que se le enseñaron, y futuros)
            n += con.execute(f"DELETE FROM events WHERE id IN ({','.join('?' * len(gone))}) AND date >= ?",
                             (*gone, today.isoformat())).rowcount
        for (code, kind, day), ev in best.items():
            values = (ev.title, ev.effect, ev.confidence, ev.why)
            old = con.execute("SELECT title, effect, confidence, why FROM events WHERE code = ? AND kind = ? "
                              "AND date = ?", (code, kind, day)).fetchone()
            if old is not None and tuple(old) == values:
                continue
            con.execute("INSERT INTO events(code, kind, date, title, effect, confidence, why, source, "
                        "created) VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(code, kind, date) DO UPDATE SET "
                        "title = excluded.title, effect = excluded.effect, confidence = excluded.confidence, "
                        "why = excluded.why, source = excluded.source",
                        (code, kind, day, *values, source, now.isoformat()))
            n += 1
    return n


def refresh(con: sqlite3.Connection, cfg: dict, now: datetime, headlines, client=None) -> int:
    """Si toca (`due`), una llamada a Claude con los titulares para apuntar eventos con fecha.
    `headlines`: Headline o filas de la tabla headlines. `client`: cliente de Anthropic (tests).
    Devuelve el nº de eventos nuevos o actualizados; cualquier error se registra y da 0 (nunca rompe
    el tick)."""
    try:
        if not due(con, cfg, now):
            return 0
        s, kv, today = _settings(cfg), db.KV(con), _today(cfg, now)
        kv.set("events:last", now.isoformat())
        picked = _pick(cfg, now, headlines)
        if not picked:
            log.info("Eventos: ningún titular que revisar")
            return 0
        known = con.execute("SELECT id, code, kind, date, title FROM events WHERE date >= ? "
                            "ORDER BY date, code LIMIT 40", (today.isoformat(),)).fetchall()
        prompt, model = _prompt(cfg, now, picked, known), cfg["sentiment"]["llm_model"]
        client = client or anthropic.Anthropic()
        try:
            ans = _parse(_ask(client, model, prompt, kv, cfg, now))
        except (ValueError, ValidationError):  # un reintento si el JSON no es válido (y queda presupuesto)
            if _calls(kv, cfg, now) >= s["llm_max_calls_day"]:
                raise
            ans = _parse(_ask(client, model, prompt + RETRY, kv, cfg, now))
        n = _save(con, cfg, now, ans, {r["id"] for r in known}, f"llm:{model}")
        log.info("Eventos: %s titulares revisados, %s eventos nuevos o actualizados", len(picked), n)
        return n
    except Exception as exc:
        log.warning("Eventos: la búsqueda falló (%s: %s)", type(exc).__name__, exc)
        return 0


def score(con: sqlite3.Connection, cfg: dict, code: str, now: datetime) -> float:
    """Componente "events" del valor en [-1, 1]. Por evento, con d = días hasta él (fecha de Madrid):
    0 < d ≤ pre_days → rampa (1 − (d−1)/pre_days) × signo × confianza (compra el rumor);
    −post_days ≤ d ≤ 0 → −0.5 × signo × confianza (vende la noticia); "mixed" → 0. El mismo evento
    apuntado con dos tipos (misma fecha y efecto) cuenta una vez. Suma acotada a [−1, 1]."""
    s = _settings(cfg)
    if not s["enabled"]:
        return 0.0
    today, pre = _today(cfg, now), s["pre_days"]
    rows = con.execute("SELECT date, effect, confidence FROM events WHERE code = ? AND date BETWEEN ? AND ?",
                       (asset(code), (today - timedelta(days=s["post_days"])).isoformat(),
                        (today + timedelta(days=pre)).isoformat())).fetchall()
    best: dict[tuple, float] = {}
    for r in rows:
        sign = EFFECTS.get(r["effect"] or "mixed", 0)
        if not sign:
            continue
        d = (date.fromisoformat(r["date"]) - today).days
        v = (1 - (d - 1) / pre if d > 0 else -0.5) * sign * min(1.0, max(0.0, r["confidence"] or 0.0))
        best[(r["date"], r["effect"])] = max(best.get((r["date"], r["effect"]), 0.0), v, key=abs)
    return max(-1.0, min(1.0, sum(best.values())))


def upcoming(con: sqlite3.Connection, cfg: dict, now: datetime, days: int | None = None) -> list[sqlite3.Row]:
    """Eventos del universo desde hace `post_days` (aún cuentan: vende la noticia) hasta dentro de
    `days` (por defecto lookahead_days), ordenados por fecha."""
    s, today, universe = _settings(cfg), _today(cfg, now), set(_universe(cfg))
    days = s["lookahead_days"] if days is None else days
    rows = con.execute("SELECT * FROM events WHERE date BETWEEN ? AND ? ORDER BY date, code, kind",
                       ((today - timedelta(days=s["post_days"])).isoformat(),
                        (today + timedelta(days=days)).isoformat())).fetchall()
    return [r for r in rows if r["code"] in universe]


def lines(con: sqlite3.Connection, cfg: dict, now: datetime) -> list[str]:
    """HTML para /eventos: "📅 19/11 (en 16 d) · TTWO · Lanzamiento de GTA VI · ⬆️ 80 %" y el porqué
    en cursiva debajo."""
    s, today = _settings(cfg), _today(cfg, now)
    if not s["enabled"]:
        return ["Los eventos están desactivados ([events] enabled = false)."]
    out = []
    for r in upcoming(con, cfg, now):
        day = date.fromisoformat(r["date"])
        d = (day - today).days
        when = "hoy" if d == 0 else f"en {d} d" if d > 0 else f"hace {-d} d"
        line = (f"📅 {day:%d/%m} ({when}) · {_e(r['code'])} · {_e(r['title'] or r['kind'])} · "
                f"{ARROWS.get(r['effect'], ARROWS['mixed'])} {round((r['confidence'] or 0) * 100)} %")
        out.append(line + (f"\n   <i>{_e(r['why'])}</i>" if r["why"] else ""))
    return out or [f"No hay eventos próximos detectados (se revisan cada {s['refresh_hours']:g} h)."]


def _e(x) -> str:
    return html.escape("" if x is None else str(x), quote=True)
