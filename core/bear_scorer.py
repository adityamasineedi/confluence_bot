"""Bear / trend SHORT scorer — aggregates bear signals for a trend short setup.

Uses normalised scoring: signals backed by unavailable data sources are excluded
from the denominator.
"""
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


def _available_signals(symbol: str, cache) -> set[str]:
    available = {"bear_ob", "oi_flush", "htf_lower_high", "funding_extreme"}
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

    avail     = _available_signals(symbol, cache)
    score_val = _normalised_score(signals, _WEIGHTS, avail)
    fire      = score_val >= _THRESHOLD and passes_trend_short_filters(symbol, cache)

    return {
        "symbol":    symbol,
        "regime":    "TREND",
        "direction": "SHORT",
        "score":     round(score_val, 4),
        "signals":   signals,
        "available": sorted(avail),
        "fire":      fire,
    }
