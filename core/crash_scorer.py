"""Crash SHORT scorer — aggregates crash signals for high-conviction short setups."""
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

    score_val = sum(
        _WEIGHTS.get(name, 0.0) for name, hit in signals.items() if hit
    )

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
