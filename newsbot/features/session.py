"""Horario de mercado para las carteras que no cotizan 24/7 (bolsa).

Sin sección [session] el mercado está siempre abierto (cripto). Con ella:
[session] tz = "Europe/Madrid", days = [0, 1, 2, 3, 4], open = "09:20", close = "17:30"
(lunes = 0). Los festivos no se listan: el adaptador de datos dice si cada valor ha cotizado hoy.
"""
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo


def _parse(hhmm: str) -> time:
    h, m = hhmm.split(":")
    return time(int(h), int(m))


def validate(s: dict) -> None:
    ZoneInfo(s["tz"])
    if not s["days"] or any(d not in range(7) for d in s["days"]):
        raise ValueError("session.days debe ser una lista de 0 (lunes) a 6 (domingo)")
    if _parse(s["open"]) >= _parse(s["close"]):
        raise ValueError("session.open debe ser anterior a session.close")


def is_open(cfg: dict, now: datetime) -> bool:
    s = cfg.get("session")
    if not s:
        return True
    local = now.astimezone(ZoneInfo(s["tz"]))
    return local.weekday() in s["days"] and _parse(s["open"]) <= local.time() < _parse(s["close"])


def last_open(cfg: dict, now: datetime) -> datetime | None:
    """Apertura de la sesión en curso (None si el mercado está cerrado o no hay horario)."""
    s = cfg.get("session")
    if not s or not is_open(cfg, now):
        return None
    tz = ZoneInfo(s["tz"])
    return datetime.combine(now.astimezone(tz).date(), _parse(s["open"]), tz)


def next_open(cfg: dict, now: datetime) -> datetime | None:
    """Próxima apertura (en la zona de la sesión); None si no hay horario o ya está abierto."""
    s = cfg.get("session")
    if not s or is_open(cfg, now):
        return None
    tz = ZoneInfo(s["tz"])
    local = now.astimezone(tz)
    for i in range(8):
        day = local.date() + timedelta(days=i)
        start = datetime.combine(day, _parse(s["open"]), tz)
        if day.weekday() in s["days"] and start > local:
            return start
    return None


def fraction(cfg: dict) -> float:
    """Parte de la semana con mercado abierto (1.0 sin horario): ritmo de muestras de learn."""
    s = cfg.get("session")
    if not s:
        return 1.0
    o, c = _parse(s["open"]), _parse(s["close"])
    hours = (c.hour * 60 + c.minute - o.hour * 60 - o.minute) / 60
    return hours * len(s["days"]) / (24 * 7)
