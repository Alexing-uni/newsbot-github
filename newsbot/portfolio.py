"""Carteras (cripto, bolsa...) que comparten proceso y chat de Telegram.

La principal es la de config.toml (palabra "cripto"); las demás se declaran en
general.portfolios y cada una tiene su config, su data_dir y su base de datos.
- REGISTRY: {palabra: cfg} de todas las carteras cargadas (vacío = solo la principal).
- CURRENT / LABEL: cartera activa en este hilo o comando. La principal deja ambos vacíos, así
  que sus mensajes y comandos son idénticos a los de antes de existir las carteras.
"""
import contextvars
from contextlib import contextmanager

MAIN = "cripto"
REGISTRY: dict[str, dict] = {}
CURRENT: contextvars.ContextVar[str] = contextvars.ContextVar("portfolio", default="")
LABEL: contextvars.ContextVar[str] = contextvars.ContextVar("portfolio_label", default="")


def word(cfg: dict) -> str:
    """Palabra con la que se elige la cartera en Telegram ("bolsa"); la principal es "cripto"."""
    return cfg["general"].get("portfolio", MAIN)


def label(cfg: dict) -> str:
    """Etiqueta de sus avisos ("📈 Bolsa"); vacía en la principal."""
    return cfg["general"].get("label", "") if word(cfg) != MAIN else ""


def register(cfgs: list[dict]) -> None:
    REGISTRY.clear()
    REGISTRY.update({word(c): c for c in cfgs})


@contextmanager
def active(cfg: dict):
    """Marca `cfg` como cartera activa (avisos con su etiqueta, comandos con su palabra)."""
    main = word(cfg) == MAIN
    t1, t2 = CURRENT.set("" if main else word(cfg)), LABEL.set(label(cfg))
    try:
        yield
    finally:
        CURRENT.reset(t1)
        LABEL.reset(t2)


def cmd(name: str, *args) -> str:
    """Comando para mostrar en un mensaje: incluye la palabra de la cartera activa si no es la
    principal ("/aprobar bolsa 3"), para que no se apruebe en la cartera equivocada."""
    w = CURRENT.get()
    return "/" + " ".join([name, *([w] if w else []), *(str(a) for a in args)])
