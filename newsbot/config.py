import copy
import hashlib
import tomllib
from pathlib import Path

from newsbot import portfolio
from newsbot.features import session

CORE_WEIGHTS = {"news", "trend", "intraday", "fng", "votes"}
SHADOW_WEIGHTS = {"rsi", "bollinger", "momentum", "volume",  # = features.market.SHADOW
                  "events", "breakout", "macd", "high52", "kronos", "dip",
                  "finbert"}  # solo bolsa (newsbot/stocks)


def validate(cfg: dict) -> dict:
    weights = cfg["weights"]
    if missing := CORE_WEIGHTS - set(weights):
        raise ValueError(f"Faltan pesos en [weights]: {sorted(missing)}")
    if unknown := set(weights) - CORE_WEIGHTS - SHADOW_WEIGHTS:
        raise ValueError(f"Pesos desconocidos en [weights]: {sorted(unknown)}")
    if any(v < 0 for v in weights.values()):
        raise ValueError("Los pesos de [weights] no pueden ser negativos")
    if abs(sum(weights.values()) - 1.0) > 1e-9:
        raise ValueError(f"Los pesos de [weights] deben sumar 1 (suman {sum(weights.values())})")
    if cfg["general"]["mode"] not in ("paper", "live"):
        raise ValueError("general.mode debe ser 'paper' o 'live'")
    if cfg["execution"]["order_type"] not in ("market", "limit_then_market"):
        raise ValueError("execution.order_type debe ser 'market' o 'limit_then_market'")
    if bad := set(cfg["sentiment"]["cascade"]) - {"llm", "finbert", "lexicon"}:
        raise ValueError(f"sentiment.cascade tiene métodos desconocidos: {bad}")
    if missing := [s for s in cfg["general"]["symbols"] if asset(s) not in cfg["news"]["assets"]]:
        raise ValueError(f"Faltan palabras clave en [news.assets] para: {missing}")
    if len({s.split("/")[1] for s in cfg["general"]["symbols"]}) != 1:
        raise ValueError("Todas las monedas de general.symbols deben cotizar contra la misma divisa")
    # Claves de las carteras de bolsa: sin ellas (cripto) no se comprueba nada nuevo.
    g, codes = cfg["general"], [asset(s) for s in cfg["general"]["symbols"]]
    if g.get("exchange") == "yahoo" and (
            missing := [c for c in codes if not (cfg.get("market", {}).get(c) or {}).get("ticker")]):
        raise ValueError(f"Faltan [market.CODE] con ticker para: {missing}")
    if "session" in cfg:
        session.validate(cfg["session"])
    if cfg["sentiment"].get("fallback", "lexicon") not in ("lexicon", "none"):
        raise ValueError("sentiment.fallback debe ser 'lexicon' o 'none'")
    if portfolio.word(cfg) in {c.lower() for c in codes}:
        raise ValueError(f"general.portfolio '{portfolio.word(cfg)}' coincide con un activo")
    if not isinstance(ps := g.get("portfolios", []), list) or not all(isinstance(x, str) for x in ps):
        raise ValueError("general.portfolios debe ser una lista de rutas (texto)")
    if not isinstance(ex := cfg["news"].get("exclude", []), list) or not all(
            isinstance(x, str) for x in ex):
        raise ValueError("news.exclude debe ser una lista de frases (texto)")
    if g.get("engine", "single") not in ("single", "stocks"):
        raise ValueError("general.engine debe ser 'single' (una posición) o 'stocks' (varias)")
    if g.get("engine") == "stocks":
        _validate_stocks(cfg)
    return cfg


def _validate_stocks(cfg: dict) -> None:
    """Cartera multiposición (newsbot/stocks): tamaños, confirmación por Telegram y eventos."""
    p, x = cfg["portfolio"], cfg["execution"]
    for k in ("max_position_pct", "min_cash_pct", "risk_per_trade_pct", "max_group_pct"):
        if not 0 < p[k] <= 1:
            raise ValueError(f"portfolio.{k} debe estar entre 0 y 1")
    if p["max_position_pct"] > p["max_group_pct"] or p["min_cash_pct"] + p["max_position_pct"] > 1:
        raise ValueError("portfolio: una posición no puede superar su sector ni dejar sin la caja mínima")
    if p["max_positions"] < 1 or p["max_group_positions"] < 1 or p["atr_stop_mult"] <= 0:
        raise ValueError("portfolio: max_positions, max_group_positions y atr_stop_mult deben ser > 0")
    if x["broker"] not in ("paper", "trading212"):
        raise ValueError("execution.broker debe ser 'paper' o 'trading212'")
    if x["broker"] == "trading212" and x.get("t212_env", "live") not in ("live", "demo"):
        raise ValueError("execution.t212_env debe ser 'live' o 'demo'")
    if x["broker"] == "trading212" and not x.get("confirm", True):
        raise ValueError("Trading 212 prohíbe el trading algorítmico por API: execution.confirm debe ser "
                         "true (cada orden se confirma en Telegram)")
    if p.get("budget_eur") is not None and p["budget_eur"] <= 0:
        raise ValueError("portfolio.budget_eur debe ser > 0 (o no ponerlo: toda la cuenta)")
    if x["proposal_minutes"] <= 0 or x["max_order_eur"] <= 0:
        raise ValueError("execution.proposal_minutes y execution.max_order_eur deben ser > 0")
    for code in (asset(s) for s in cfg["general"]["symbols"]):
        m = cfg["market"][code]
        if x["broker"] == "trading212" and not m.get("t212"):
            raise ValueError(f"Falta [market.{code}].t212 (ticker de Trading 212)")
        if not m.get("group"):
            raise ValueError(f"Falta [market.{code}].group (sector, para los límites por sector)")
    e = cfg.get("events", {})
    if e.get("enabled") and (e["lookahead_days"] <= 0 or e["pre_days"] <= 0 or e["post_days"] < 0):
        raise ValueError("events: lookahead_days y pre_days > 0, post_days >= 0")


def asset(symbol: str) -> str:
    """'ETH/EUR' -> 'ETH'"""
    return symbol.split("/")[0]


def load(path: str | Path = "config.toml") -> dict:
    raw = Path(path).read_bytes()
    cfg = validate(tomllib.loads(raw.decode("utf-8")))
    cfg["_hash"] = hashlib.sha256(raw).hexdigest()[:12]  # se guarda con cada decisión
    return cfg


def load_portfolios(cfg: dict, config_path: str | Path) -> list[dict]:
    """Configs de general.portfolios, con rutas relativas a la carpeta del config principal."""
    base, out, words = Path(config_path).resolve().parent, [], {portfolio.word(cfg)}
    for rel in cfg["general"].get("portfolios", []):
        path = base / rel
        try:
            c = load(path)
        except Exception as exc:
            raise ValueError(f"Cartera {path}: {exc}") from exc
        if portfolio.word(c) in words:
            raise ValueError(f"Cartera {path}: la palabra '{portfolio.word(c)}' ya está en uso")
        words.add(portfolio.word(c))
        out.append(c)
    return out


def apply(cfg: dict, override: dict) -> dict:
    """Copia de `cfg` con `override` ({sección: {clave: valor}}) encima, validada."""
    out = copy.deepcopy(cfg)
    for section, values in override.items():
        out[section].update(values)
    return validate(out)
