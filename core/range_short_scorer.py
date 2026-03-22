"""Range SHORT scorer — aggregates range signals for a sell-at-resistance setup."""
import os
import yaml

from signals.range.ask_absorption  import check_ask_absorption_ratio
from signals.range.upthrust        import check_wyckoff_upthrust
from signals.range.perp_basis      import check_perp_basis
from signals.range.options_skew    import check_options_skew
from signals.range.anchored_vwap   import check_anchored_vwap
from signals.range.time_distribution import check_time_distribution
from .range_filter import passes_range_filters

_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "config.yaml")
with open(_CONFIG_PATH) as _f:
    _cfg = yaml.safe_load(_f)

_WEIGHTS   = _cfg["weights"]["range_short"]
_THRESHOLD = _cfg["thresholds"]["range_short_fire"]

# Mandatory signals — at least one must be True
_MANDATORY = {"ask_absorption", "upthrust"}


async def score(symbol: str, cache) -> dict:
    """Score a symbol for a RANGE SHORT setup.

    Mandatory signals: at least one of (ask_absorption, upthrust) must be True.
    Returns a dict: {symbol, regime, direction, score, signals, fire}.
    """
    signals: dict[str, bool] = {
        "ask_absorption":   check_ask_absorption_ratio(symbol, cache),
        "upthrust":         check_wyckoff_upthrust(symbol, cache),
        "perp_basis":       check_perp_basis(symbol, cache),
        "options_skew":     check_options_skew(symbol, cache),
        "anchored_vwap":    check_anchored_vwap(symbol, cache),
        "time_distribution": check_time_distribution(symbol, cache),
    }

    score_val = sum(
        _WEIGHTS.get(name, 0.0) for name, hit in signals.items() if hit
    )

    mandatory_ok = any(signals[m] for m in _MANDATORY)
    fire = (
        score_val >= _THRESHOLD
        and mandatory_ok
        and passes_range_filters(symbol, cache)
    )

    return {
        "symbol":    symbol,
        "regime":    "RANGE",
        "direction": "SHORT",
        "score":     round(score_val, 4),
        "signals":   signals,
        "fire":      fire,
    }
