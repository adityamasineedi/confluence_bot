"""Trend LONG scorer — aggregates trend signals into a confluence score.

Uses normalised scoring: signals backed by unavailable data sources (no WebSocket
CVD, no CoinGlass key, no Deribit) are excluded from the denominator so the score
represents confluence *of available data*, not diluted by structural gaps.
"""
import os
import yaml

from signals.trend.cvd          import check_cvd_bullish
from signals.trend.liquidity    import check_liq_sweep
from signals.trend.oi_funding   import check_oi_funding
from signals.trend.vpvr         import check_vpvr_reclaim
from signals.trend.htf_structure import check_htf_structure
from signals.trend.order_block  import check_order_block
from signals.trend.whale_flow   import check_whale_flow
from signals.trend.rsi_divergence   import check_rsi_divergence_bullish
from signals.trend.ema_cross        import check_ema_pullback_long
from signals.trend.long_short_ratio import check_ls_crowded_short
from signals.bear.funding_ramp      import check_funding_ramp_bullish
from signals.trend.fvg              import check_fvg_bullish
from signals.trend.bb_squeeze       import check_bb_squeeze_bullish
from .filter import passes_trend_long_filters

_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "config.yaml")
with open(_CONFIG_PATH) as _f:
    _cfg = yaml.safe_load(_f)

_WEIGHTS   = _cfg["weights"]["trend_long"]
_THRESHOLD = _cfg["thresholds"]["trend_long_fire"]


def _available_signals(symbol: str, cache) -> set[str]:
    """Return signal names that have live data backing them AND have non-zero weight.

    Signals with weight=0.0 (disabled) are excluded from the denominator so they
    cannot inflate it and suppress scores for active signals.
    Signals with live data unavailable are also excluded from the denominator.
    """
    available: set[str] = set()

    # Helper: only add if weight > 0 (disabled signals must not touch denominator)
    def _add(name: str) -> None:
        if _WEIGHTS.get(name, 0.0) > 0.0:
            available.add(name)

    # Core signals — always available (REST/OHLCV data, no external key needed)
    _add("oi_funding")
    _add("vpvr_support")
    _add("htf_structure")
    _add("order_block")
    _add("rsi_divergence")   # needs only 1H OHLCV — always available
    _add("ema_pullback")     # needs only 1H closes — always available

    # CVD requires aggTrade WebSocket warmup; check if 1H CVD values exist
    # (check_cvd_bullish uses 1H tf — guard must match to avoid stale denominator)
    if cache.get_cvd(symbol, 1, "1h"):
        _add("cvd_bullish")

    # Liq clusters from CoinGlass (paid) OR synthetic pivots (BinanceRestPoller)
    if cache.get_liq_clusters(symbol):
        _add("liq_sweep")

    # CryptoQuant whale inflow — exchange inflow must be non-None
    if cache.get_exchange_inflow(symbol) is not None:
        _add("whale_flow")

    # Coinglass L/S ratio — only available when API key is set
    if cache.get_long_short_ratio(symbol) is not None:
        _add("ls_crowded_short")

    # Extreme negative funding (short squeeze fuel) — available when Coinglass key set
    if cache.get_funding_rate(symbol) is not None:
        _add("funding_ramp_bull")

    # FVG and BB squeeze use only OHLCV — always available
    _add("fvg_bullish")
    _add("bb_squeeze_bull")

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
        "whale_flow":    check_whale_flow(symbol, cache),
        "rsi_divergence":    check_rsi_divergence_bullish(symbol, cache),
        "ema_pullback":      check_ema_pullback_long(symbol, cache),
        "ls_crowded_short":  check_ls_crowded_short(symbol, cache),
        "funding_ramp_bull": check_funding_ramp_bullish(symbol, cache),
        "fvg_bullish":       check_fvg_bullish(symbol, cache),
        "bb_squeeze_bull":   check_bb_squeeze_bullish(symbol, cache),
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
