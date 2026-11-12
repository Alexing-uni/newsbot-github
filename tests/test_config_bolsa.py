"""config_bolsa.toml carga y valida; las claves nuevas solo se comprueban cuando están."""
import copy
import re

import pytest
from conftest import ROOT

from newsbot import config as config_mod
from newsbot import portfolio
from newsbot.features import session
from newsbot.news import ingest

BOLSA = ROOT / "config_bolsa.toml"
# Universo de la cartera europea.
FULL = ["DFNS/EUR", "GOLD/EUR", "RHM/EUR", "LDO/EUR", "HO/EUR", "IDR/EUR", "ASML/EUR", "IFX/EUR", "SEMI/EUR",
        "NVDA/EUR", "AMD/EUR", "AAPL/EUR", "MSFT/EUR", "TTWO/EUR", "WMT/EUR", "SP500/EUR"]


@pytest.fixture
def bolsa():
    c = config_mod.load(BOLSA)
    assert c["general"]["symbols"] == FULL
    return c


def test_bolsa_config_loads(bolsa):
    g = bolsa["general"]
    assert (portfolio.word(bolsa), portfolio.label(bolsa)) == ("bolsa", "📈 Bolsa")
    assert (g["mode"], g["exchange"], g["data_dir"]) == ("paper", "yahoo", "data/bolsa")
    assert g["engine"] == "stocks"
    assert g["tick_minutes"] == 15 and g["initial_capital"] == 20.0  # = su presupuesto
    assert not g.get("wind_down") and {s.split("/")[0] for s in FULL} <= set(bolsa["market"])
    mk = bolsa["market"]
    assert {c: mk[c]["ticker"] for c in ("DFNS", "GOLD", "RHM", "SEMI", "SP500")} == {
        "DFNS": "DFEN.DE", "GOLD": "4GLD.DE", "RHM": "RHM.DE", "SEMI": "SMH.MI", "SP500": "SXR8.DE"}
    assert all(m["t212"].endswith("_EQ") and m["group"] for m in mk.values())
    assert bolsa["execution"]["broker"] == "trading212" and bolsa["execution"]["confirm"] is True
    assert mk["RHM"]["isin"] == "DE0007030009" and all(len(m["name"]) > 40 for m in mk.values())
    assert bolsa["news"]["fng"] is False and "cryptopanic_plan" not in bolsa["news"]
    assert bolsa["sentiment"]["fallback"] == "none" and bolsa["sentiment"]["cascade"] == ["llm"]
    assert bolsa["weights"]["fng"] == bolsa["weights"]["votes"] == 0
    assert bolsa["risk"]["min_order_eur"] == 1.0 and bolsa["costs"]["fee_pct"] == 0.0
    assert 0 < session.fraction(bolsa) < 0.25


def test_bolsa_keywords_and_feeds(bolsa):
    n = bolsa["news"]
    for code, words in n["assets"].items():
        assert len(words) == len(set(words)), code
        for w in words:  # cada palabra se encuentra a sí misma con el patrón de ingest (\b...\b)
            assert ingest.pattern([w]).search(f"x {w} x"), (code, w)
    assert "nato" in n["assets"]["RHM"] and "nato" not in n["assets"]["ASML"]
    assert "tariffs" in n["assets"]["SP500"] and "federal reserve" in n["assets"]["GOLD"]
    assert len(n["feeds"]) == 15 and "gn_events" in n["feeds"]
    for name, url in n["feeds"].items():
        assert url.startswith("https://") and " " not in url
        if name.startswith("gn_"):
            assert re.search(r"when%3A\d+[hd]", url), name
    w = n["source_weights"]
    assert w["reuters"] == 1.0 and w["ad-hoc-news.de"] == 0 and w["default"] == 0.4
    assert all(k == k.lower() for k in w)


def test_crypto_config_is_untouched_by_new_checks():
    cfg = config_mod.load(ROOT / "config.toml")
    assert "session" not in cfg and "market" not in cfg and portfolio.word(cfg) == "cripto"


@pytest.mark.parametrize("change, error", [
    (lambda c: c["market"]["RHM"].pop("ticker"), "RHM"),
    (lambda c: c["market"].pop("IFX"), "IFX"),
    (lambda c: c["session"].update(open="18:00"), "session.open"),
    (lambda c: c["session"].update(days=[7]), "session.days"),
    (lambda c: c["sentiment"].update(fallback="finbert"), "fallback"),
    (lambda c: c["general"].update(portfolio="rhm"), "rhm"),
    (lambda c: c["general"].update(portfolios=[1]), "portfolios"),
    (lambda c: c["news"].update(exclude="gold medal"), "exclude"),
])
def test_validate_errors(bolsa, change, error):
    c = copy.deepcopy(bolsa)
    change(c)
    with pytest.raises(ValueError, match=error):
        config_mod.validate(c)


def test_load_portfolios(tmp_path, bolsa):
    main = config_mod.load(ROOT / "config.toml")
    main["general"].pop("portfolios", None)  # el config.toml real puede declararla ya
    assert config_mod.load_portfolios(main, ROOT / "config.toml") == []
    main["general"]["portfolios"] = ["config_bolsa.toml"]
    [b] = config_mod.load_portfolios(main, ROOT / "config.toml")
    assert portfolio.word(b) == "bolsa" and b["market"] == bolsa["market"]
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "rota.toml").write_text("[general\n", encoding="utf-8")
    main["general"]["portfolios"] = ["sub/rota.toml"]
    with pytest.raises(ValueError, match="rota.toml"):
        config_mod.load_portfolios(main, tmp_path / "config.toml")
    main["general"]["portfolios"] = ["falta.toml"]
    with pytest.raises(ValueError, match="falta.toml"):
        config_mod.load_portfolios(main, tmp_path / "config.toml")
    (tmp_path / "otra.toml").write_text(BOLSA.read_text(encoding="utf-8").replace(
        'portfolio = "bolsa"', 'portfolio = "cripto"'), encoding="utf-8")
    main["general"]["portfolios"] = ["otra.toml"]
    with pytest.raises(ValueError, match="cripto"):
        config_mod.load_portfolios(main, tmp_path / "config.toml")
