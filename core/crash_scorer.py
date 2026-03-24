"""Crash SHORT scorer — aggregates crash signals for high-conviction short setups.

Uses normalised scoring: signals backed by unavailable data sources are excluded
from the denominator.
"""
import os
import yaml

from signals.crash.dead_cat       import check_dead_cat_setup
from signals.crash.liq_grab_short import check_liq_grab_short
from signals.bear.cvd_bearish     import check_cvd_bearish
from signals.bear.oi_flush        import check_oi_long_flush
from signals.bear.whale_inflow    import check_whale_exchange_inflow
from .filter import passes_crash_filters

_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "config.yaml")
with open(_CONFIG_PATH) as _f:
    _cfg = yaml.safe_load(_f)

_WEIGHTS   = _cfg["weights"]["crash"]
_THRESHOLD = _cfg["thresholds"]["crash_short_fire"]

# dead_cat is mandatory — a crash short without a bounce setup is premature
_MANDATORY = {"dead_cat"}


def _available_signals(symbol: str, cache) -> set[str]:
    # dead_cat, liq_grab_short, oi_flush use OHLCV only — always available
    available = {"dead_cat", "liq_grab_short", "oi_flush"}
    if cache.get_cvd(symbol, 1, "5m"):
        available.add("cvd_bearish")
    if cache.get_exchange_inflow(symbol) is not None:
        available.add("whale_inflow")
    return available


def _normalised_score(
    signals: dict[str, bool],
    weights: dict[str, float],
    available: set[str],
) -> float:
    denom = sum(w for k, w in weights.items() if k in available)
    if denom == 0.0:
        return 0.0
    numer = sum(w for k, w in weights.items() if k in available and signals.get(k, False))
    return numer / denom


async def score(symbol: str, cache) -> dict:
    """Score a symbol for a CRASH SHORT setup.

    Mandatory signals: dead_cat must be True.
    Higher threshold (0.75) from config — crash regime requires extra confluence.
    Returns a dict: {symbol, regime, direction, score, signals, fire}.
    """
    signals: dict[str, bool] = {
        "dead_cat":       check_dead_cat_setup(symbol, cache),
        "liq_grab_short": check_liq_grab_short(symbol, cache),
        "cvd_bearish":    check_cvd_bearish(symbol, cache),
        "oi_flush":       check_oi_long_flush(symbol, cache),
        "whale_inflow":   check_whale_exchange_inflow(symbol, cache),
    }

    avail        = _available_signals(symbol, cache)
    score_val    = _normalised_score(signals, _WEIGHTS, avail)
    mandatory_ok = all(signals[m] for m in _MANDATORY)
    fire = (
        score_val >= _THRESHOLD
        and mandatory_ok
        and passes_crash_filters(symbol, cache)
    )

    return {
        "symbol":    symbol,
        "regime":    "CRASH",
        "direction": "SHORT",
        "score":     round(score_val, 4),
        "signals":   signals,
        "fire":      fire,
    }
