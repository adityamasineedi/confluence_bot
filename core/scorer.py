"""Trend LONG scorer — aggregates trend signals into a confluence score.

Uses normalised scoring: signals backed by unavailable data sources (no WebSocket
CVD, no CoinGlass key, no Deribit) are excluded from the denominator so the score
represents confluence *of available data*, not diluted by structural gaps.
"""
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


def _available_signals(symbol: str, cache) -> set[str]:
    """Return signal names that have live data backing them.

    Signals are excluded from scoring denominator when their data source
    is structurally absent (empty cache, stub returns no data).
    """
    available = {"oi_funding", "vpvr_support", "htf_structure", "order_block"}

    # CVD requires aggTrade WebSocket warmup; check if any CVD values exist
    if cache.get_cvd(symbol, 1, "5m"):
        available.add("cvd_bullish")

    # Liq clusters from CoinGlass (paid) OR synthetic pivots (BinanceRestPoller)
    if cache.get_liq_clusters(symbol):
        available.add("liq_sweep")

    # Deribit options flow — skew history must be non-empty
    if cache.get_skew_history(symbol, 1):
        available.add("options_flow")

    # CryptoQuant whale inflow — exchange inflow must be non-None
    if cache.get_exchange_inflow(symbol) is not None:
        available.add("whale_flow")

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
    """Score a symbol for a TREND LONG setup.

    Returns a dict: {symbol, regime, direction, score, signals, fire}
    where score is the normalised weighted sum over *available* signals only,
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

    avail     = _available_signals(symbol, cache)
    score_val = _normalised_score(signals, _WEIGHTS, avail)
    fire      = score_val >= _THRESHOLD and passes_trend_long_filters(symbol, cache)

    return {
        "symbol":    symbol,
        "regime":    "TREND",
        "direction": "LONG",
        "score":     round(score_val, 4),
        "signals":   signals,
        "available": sorted(avail),
        "fire":      fire,
    }
