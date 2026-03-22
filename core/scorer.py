"""Trend LONG scorer — aggregates trend signals into a confluence score."""
import os
import yaml

from signals.trend.cvd       import check_cvd_bullish
from signals.trend.liquidity  import check_liq_sweep
from signals.trend.oi_funding import check_oi_funding
from signals.trend.vpvr        import check_vpvr_reclaim
from signals.trend.htf_structure import check_htf_structure
from signals.trend.order_block   import check_order_block
from signals.trend.options       import check_options_flow
from signals.trend.whale_flow    import check_whale_flow
from .filter import passes_trend_long_filters

_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "config.yaml")
with open(_CONFIG_PATH) as _f:
    _cfg = yaml.safe_load(_f)

_WEIGHTS   = _cfg["weights"]["trend_long"]
_THRESHOLD = _cfg["thresholds"]["trend_long_fire"]


async def score(symbol: str, cache) -> dict:
    """Score a symbol for a TREND LONG setup.

    Returns a dict: {symbol, regime, direction, score, signals, fire}
    where score is the weighted sum of True signals / sum of all weights,
    and fire is True when score ≥ threshold AND all hard filters pass.
    """
    signals: dict[str, bool] = {
        "cvd_bullish":   check_cvd_bullish(symbol, cache),
        "liq_sweep":     check_liq_sweep(symbol, cache),
        "oi_funding":    check_oi_funding(symbol, cache),
        "vpvr_support":  check_vpvr_reclaim(symbol, cache),
        "htf_structure": check_htf_structure(symbol, cache),
        "order_block":   check_order_block(symbol, cache),
        "options_flow":  check_options_flow(symbol, cache),
        "whale_flow":    check_whale_flow(symbol, cache),
    }

    score_val = sum(
        _WEIGHTS.get(name, 0.0) for name, hit in signals.items() if hit
    )

    fire = score_val >= _THRESHOLD and passes_trend_long_filters(symbol, cache)

    return {
        "symbol":    symbol,
        "regime":    "TREND",
        "direction": "LONG",
        "score":     round(score_val, 4),
        "signals":   signals,
        "fire":      fire,
    }
