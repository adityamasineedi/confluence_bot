"""Range LONG scorer — aggregates range signals for a buy-at-support setup."""
import os
import yaml

from signals.range.absorption      import check_absorption_ratio
from signals.range.wyckoff_spring  import check_wyckoff_spring
from signals.range.perp_basis      import check_perp_basis
from signals.range.anchored_vwap   import check_anchored_vwap, check_vwap_oversold
from signals.trend.fvg             import check_fvg_bullish
from signals.range.rsi_oversold    import check_rsi_oversold
from .range_filter import passes_range_filters

_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "config.yaml")
with open(_CONFIG_PATH) as _f:
    _cfg = yaml.safe_load(_f)

_WEIGHTS   = _cfg["weights"]["range_long"]
_THRESHOLD = _cfg["thresholds"]["range_long_fire"]

# Mandatory signals — at least one price-action anchor must fire.
# rsi_oversold is intentionally excluded: RSI < 30 in isolation is a lagging
# oscillator reading that fires in strong downtrends without structural support.
# Only absorption (high-volume support) or wyckoff_spring (low-vol wick reversal)
# provide evidence that buyers are actually present at this level.
_MANDATORY = {"absorption", "wyckoff_spring"}


async def score(symbol: str, cache) -> dict:
    """Score a symbol for a RANGE LONG setup.

    Mandatory: at least one price-action anchor (absorption OR wyckoff_spring) must fire.
    rsi_oversold contributes to score but does not satisfy the anchor alone.
    Returns a dict: {symbol, regime, direction, score, signals, fire}.
    """
    signals: dict[str, bool] = {
        "absorption":        check_absorption_ratio(symbol, cache),
        "wyckoff_spring":    check_wyckoff_spring(symbol, cache),
        "perp_basis":        check_perp_basis(symbol, cache),
        "anchored_vwap":     check_anchored_vwap(symbol, cache),
        "rsi_oversold":      check_rsi_oversold(symbol, cache),
        "vwap_oversold":     check_vwap_oversold(symbol, cache),
        "fvg_bullish":       check_fvg_bullish(symbol, cache),
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
