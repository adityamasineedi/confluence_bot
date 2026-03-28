"""PUMP regime scorer — live version using config thresholds and available-signal detection.

Replaces the backtest.scorer.score_pump import that was incorrectly used in live trading.
The PUMP regime fires when a +12% 7-day move is confirmed by HTF structure (at minimum).
"""
import os
import yaml

from signals.trend.htf_structure import check_htf_structure
from signals.trend.oi_funding    import check_oi_funding
from signals.trend.order_block   import check_order_block
from .filter import passes_pump_filters

_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "config.yaml")
with open(_CONFIG_PATH) as _f:
    _cfg = yaml.safe_load(_f)

_WEIGHTS   = _cfg["weights"]["pump"]
_THRESHOLD = _cfg["thresholds"]["pump_long_fire"]


def _available_signals(symbol: str, cache) -> set[str]:
    """All three pump signals are OHLCV-based — always available."""
    return {"htf_structure", "oi_funding", "order_block"}


def _normalised_score(signals: dict[str, bool], available: set[str]) -> float:
    denom = sum(w for k, w in _WEIGHTS.items() if k in available)
    if denom == 0.0:
        return 0.0
    numer = sum(w for k, w in _WEIGHTS.items() if k in available and signals.get(k, False))
    return numer / denom


async def score(symbol: str, cache) -> dict:
    """Score a symbol for a PUMP LONG setup.

    Returns {symbol, regime, direction, score, signals, fire}.
    htf_structure is mandatory AND at least one of order_block/oi_funding must fire.
    """
    signals: dict[str, bool] = {
        "htf_structure": check_htf_structure(symbol, cache),
        "oi_funding":    check_oi_funding(symbol, cache),
        "order_block":   check_order_block(symbol, cache),
    }

    avail     = _available_signals(symbol, cache)
    score_val = _normalised_score(signals, avail)

    # htf_structure is mandatory AND at least one of order_block / oi_funding must confirm.
    # htf_structure alone (score 0.40) can no longer clear the 0.65 threshold, but
    # htf + ob (0.70) or htf + oi (0.70) both pass — requiring real confluence.
    min_ok = signals["htf_structure"] and (
        signals["order_block"] or signals["oi_funding"]
    )
    fire   = score_val >= _THRESHOLD and min_ok and passes_pump_filters(symbol, cache)

    return {
        "symbol":    symbol,
        "regime":    "PUMP",
        "direction": "LONG",
        "score":     round(score_val, 4),
        "signals":   signals,
        "available": sorted(avail),
        "fire":      fire,
    }
