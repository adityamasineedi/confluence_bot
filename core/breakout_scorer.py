"""BREAKOUT regime scorers — live versions using config thresholds.

Replaces backtest.scorer imports that were incorrectly used in live trading.
"""
import os
import yaml

from signals.trend.htf_structure  import check_htf_structure
from signals.trend.oi_funding     import check_oi_funding
from signals.range.absorption     import check_absorption_ratio
from signals.trend.bb_squeeze     import check_bb_squeeze_bullish
from signals.bear.htf_lower_high  import check_htf_lower_high
from signals.bear.oi_flush        import check_oi_long_flush
from signals.range.ask_absorption import check_ask_absorption_ratio
from .filter import passes_breakout_long_filters, passes_breakout_short_filters

_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "config.yaml")
with open(_CONFIG_PATH) as _f:
    _cfg = yaml.safe_load(_f)

_W_LONG  = _cfg["weights"]["breakout_long"]
_W_SHORT = _cfg["weights"]["breakout_short"]
_THR_L   = _cfg["thresholds"]["breakout_long_fire"]
_THR_S   = _cfg["thresholds"]["breakout_short_fire"]


def _norm(signals: dict[str, bool], weights: dict[str, float], available: set[str]) -> float:
    denom = sum(w for k, w in weights.items() if k in available)
    if denom == 0.0:
        return 0.0
    numer = sum(w for k, w in weights.items() if k in available and signals.get(k, False))
    return numer / denom


async def score_long(symbol: str, cache) -> dict:
    """BREAKOUT LONG: price just cleared range high with volume and HTF confirmation."""
    signals: dict[str, bool] = {
        "htf_structure":  check_htf_structure(symbol, cache),
        "oi_funding":     check_oi_funding(symbol, cache),
        "absorption":     check_absorption_ratio(symbol, cache),
        "bb_squeeze_bull": check_bb_squeeze_bullish(symbol, cache),
    }
    avail     = {"htf_structure", "oi_funding", "absorption", "bb_squeeze_bull"}
    score_val = _norm(signals, _W_LONG, avail)

    # Need at least 2 of 3 signals, and htf_structure is required
    min_ok = sum(signals.values()) >= 2 and signals["htf_structure"]
    fire   = score_val >= _THR_L and min_ok and passes_breakout_long_filters(symbol, cache)

    return {
        "symbol":    symbol,
        "regime":    "BREAKOUT",
        "direction": "LONG",
        "score":     round(score_val, 4),
        "signals":   signals,
        "available": sorted(avail),
        "fire":      fire,
    }


async def score_short(symbol: str, cache) -> dict:
    """BREAKOUT SHORT: price just broke below range low with OI flush confirmation."""
    signals: dict[str, bool] = {
        "htf_lower_high": check_htf_lower_high(symbol, cache),
        "oi_flush":       check_oi_long_flush(symbol, cache),
        "ask_absorption": check_ask_absorption_ratio(symbol, cache),
    }
    avail     = {"htf_lower_high", "oi_flush", "ask_absorption"}
    score_val = _norm(signals, _W_SHORT, avail)

    # Need at least 2 of 3 signals
    min_ok = sum(signals.values()) >= 2
    fire   = score_val >= _THR_S and min_ok and passes_breakout_short_filters(symbol, cache)

    return {
        "symbol":    symbol,
        "regime":    "BREAKOUT",
        "direction": "SHORT",
        "score":     round(score_val, 4),
        "signals":   signals,
        "available": sorted(avail),
        "fire":      fire,
    }
