"""Range LONG scorer — aggregates range signals for a buy-at-support setup."""
import os
import yaml

from signals.range.absorption      import check_absorption_ratio
from signals.range.wyckoff_spring  import check_wyckoff_spring
from signals.range.perp_basis      import check_perp_basis
from signals.range.options_skew    import check_options_skew
from signals.range.anchored_vwap   import check_anchored_vwap
from signals.range.time_distribution import check_time_distribution
from signals.range.call_skew_roc   import check_call_skew_roc
from .range_filter import passes_range_filters

_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "config.yaml")
with open(_CONFIG_PATH) as _f:
    _cfg = yaml.safe_load(_f)

_WEIGHTS   = _cfg["weights"]["range_long"]
_THRESHOLD = _cfg["thresholds"]["range_long_fire"]

# Mandatory signals — if neither fires, suppress regardless of score
_MANDATORY = {"absorption", "wyckoff_spring"}


async def score(symbol: str, cache) -> dict:
    """Score a symbol for a RANGE LONG setup.

    Mandatory signals: at least one of (absorption, wyckoff_spring) must be True.
    Returns a dict: {symbol, regime, direction, score, signals, fire}.
    """
    signals: dict[str, bool] = {
        "absorption":       check_absorption_ratio(symbol, cache),
        "wyckoff_spring":   check_wyckoff_spring(symbol, cache),
        "perp_basis":       check_perp_basis(symbol, cache),
        "options_skew":     check_options_skew(symbol, cache),
        "anchored_vwap":    check_anchored_vwap(symbol, cache),
        "time_distribution": check_time_distribution(symbol, cache),
        "call_skew_roc":    check_call_skew_roc(symbol, cache),
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
        "direction": "LONG",
        "score":     round(score_val, 4),
        "signals":   signals,
        "fire":      fire,
    }
