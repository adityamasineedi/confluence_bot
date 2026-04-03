"""Bear / trend SHORT scorer — aggregates bear signals for a trend short setup.

Uses normalised scoring: signals backed by unavailable data sources are excluded
from the denominator.
"""
import os
import yaml

from signals.bear.cvd_bearish    import check_cvd_bearish
from signals.bear.oi_flush       import check_oi_long_flush
from signals.trend.funding_extreme import check_funding_extreme_short
from signals.bear.funding_ramp    import check_funding_ramp_bearish, check_funding_ramp_bullish
from signals.bear.whale_inflow   import check_whale_exchange_inflow
from signals.trend.rsi_divergence   import check_rsi_divergence_bearish
from signals.trend.ema_cross        import check_ema_pullback_short
from signals.trend.long_short_ratio import check_ls_crowded_long
from .filter import passes_trend_short_filters

_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "config.yaml")
with open(_CONFIG_PATH) as _f:
    _cfg = yaml.safe_load(_f)

_WEIGHTS   = _cfg["weights"]["bear"]
_THRESHOLD = _cfg["thresholds"]["trend_short_fire"]


def _available_signals(symbol: str, cache) -> set[str]:
    # Only include signals with non-zero weight — disabled signals must not inflate denominator
    available: set[str] = set()

    def _add(name: str) -> None:
        if _WEIGHTS.get(name, 0.0) > 0.0:
            available.add(name)

    _add("oi_flush")
    _add("funding_extreme")
    _add("rsi_divergence")   # needs only 1H OHLCV — always available
    _add("ema_pullback")
    if cache.get_cvd(symbol, 1, "1h"):
        _add("cvd_bearish")
    if cache.get_exchange_inflow(symbol) is not None:
        _add("whale_inflow")
    if cache.get_long_short_ratio(symbol) is not None:
        _add("ls_crowded_long")
    # Funding ramp — uses funding rate scalar (always available when Coinglass key set)
    if cache.get_funding_rate(symbol) is not None:
        _add("funding_ramp")
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
        "oi_flush":        check_oi_long_flush(symbol, cache),
        "funding_extreme": check_funding_extreme_short(symbol, cache),
        "whale_inflow":    check_whale_exchange_inflow(symbol, cache),
        "rsi_divergence":  check_rsi_divergence_bearish(symbol, cache),
        "ema_pullback":    check_ema_pullback_short(symbol, cache),
        "ls_crowded_long": check_ls_crowded_long(symbol, cache),
        "funding_ramp":    check_funding_ramp_bearish(symbol, cache),
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
