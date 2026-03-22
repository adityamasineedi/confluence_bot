"""Bear / trend SHORT scorer — aggregates bear signals for a trend short setup."""
import os
import yaml

from signals.bear.cvd_bearish    import check_cvd_bearish
from signals.bear.bear_ob        import check_bear_ob_breakdown
from signals.bear.oi_flush       import check_oi_long_flush
from signals.bear.htf_lower_high import check_htf_lower_high
from signals.bear.funding_extreme import check_funding_extreme_positive
from signals.bear.whale_inflow   import check_whale_exchange_inflow
from .filter import passes_trend_short_filters

_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "config.yaml")
with open(_CONFIG_PATH) as _f:
    _cfg = yaml.safe_load(_f)

_WEIGHTS   = _cfg["weights"]["bear"]
_THRESHOLD = _cfg["thresholds"]["trend_short_fire"]


async def score(symbol: str, cache) -> dict:
    """Score a symbol for a TREND SHORT (BEAR) setup.

    Returns a dict: {symbol, regime, direction, score, signals, fire}.
    """
    signals: dict[str, bool] = {
        "cvd_bearish":     check_cvd_bearish(symbol, cache),
        "bear_ob":         check_bear_ob_breakdown(symbol, cache),
        "oi_flush":        check_oi_long_flush(symbol, cache),
        "htf_lower_high":  check_htf_lower_high(symbol, cache),
        "funding_extreme": check_funding_extreme_positive(symbol, cache),
        "whale_inflow":    check_whale_exchange_inflow(symbol, cache),
    }

    score_val = sum(
        _WEIGHTS.get(name, 0.0) for name, hit in signals.items() if hit
    )

    fire = score_val >= _THRESHOLD and passes_trend_short_filters(symbol, cache)

    return {
        "symbol":    symbol,
        "regime":    "TREND",
        "direction": "SHORT",
        "score":     round(score_val, 4),
        "signals":   signals,
        "fire":      fire,
    }
