"""Backtest scorer — wraps the live signal functions but normalises the score
over only the signals that have real data available in a backtest context.

Historically unavailable signals (CVD, liq clusters, options skew, whale aggTrades)
always return False regardless of market conditions, so including them in the
denominator would dilute every score to near-zero and prevent any trade from firing.

Instead we compute:

    score = sum(weight_i  for  True signals i)
            ─────────────────────────────────
            sum(weight_i  for  ALL data-bearing signals i)

…then apply the original threshold against this normalised 0-1 score.

The "data-bearing" set for each regime is hard-coded below based on what
BacktestCache can answer with OHLCV + OI + funding history alone.
"""
import os
import yaml

_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "config.yaml")
with open(_CONFIG_PATH) as _f:
    _cfg = yaml.safe_load(_f)

# ── Signals available in backtest (OHLCV + OI + funding only) ────────────────

_AVAILABLE = {
    "trend_long":  {"oi_funding", "vpvr_support", "htf_structure", "order_block"},
    "trend_short": {"oi_flush", "htf_lower_high", "bear_ob", "funding_extreme"},
    "range_long":  {"absorption", "wyckoff_spring", "anchored_vwap", "time_distribution"},
    "range_short": {"ask_absorption", "upthrust", "anchored_vwap", "time_distribution"},
    "crash":       {"dead_cat", "liq_grab_short", "oi_flush"},
}

# Minimum number of available signals that must fire before we consider the setup
_MIN_SIGNALS = {
    "trend_long":  2,
    "trend_short": 2,
    "range_long":  2,
    "range_short": 2,
    "crash":       2,
}

# Backtest-specific thresholds — lower than live because CVD/liq/options/whale signals
# are excluded from the denominator, leaving only 3-4 signals per regime.
# "2 of 4 available signals firing" is the effective bar; these thresholds
# represent approximately the normalised score produced by any 2-signal confluence.
_BT_THRESHOLDS = {
    "trend_long":  0.45,   # live: 0.65
    "trend_short": 0.50,   # live: 0.65  (oi_flush+htf_lower_high = 0.57)
    "range_long":  0.45,   # live: 0.60
    "range_short": 0.45,   # live: 0.60
    "crash":       0.55,   # live: 0.75
}


def _normalised_score(
    signals: dict[str, bool],
    weights: dict[str, float],
    available: set[str],
) -> float:
    """Score normalised to the weight of available signals only."""
    denom = sum(w for k, w in weights.items() if k in available)
    if denom == 0.0:
        return 0.0
    numer = sum(w for k, w in weights.items()
                if k in available and signals.get(k, False))
    return numer / denom


async def score_trend_long(symbol: str, cache) -> dict:
    from signals.trend.oi_funding    import check_oi_funding
    from signals.trend.vpvr          import check_vpvr_reclaim
    from signals.trend.htf_structure import check_htf_structure
    from signals.trend.order_block   import check_order_block
    from core.filter import passes_trend_long_filters

    weights = _cfg["weights"]["trend_long"]
    thr     = _BT_THRESHOLDS["trend_long"]
    avail   = _AVAILABLE["trend_long"]

    signals: dict[str, bool] = {
        "oi_funding":    check_oi_funding(symbol, cache),
        "vpvr_support":  check_vpvr_reclaim(symbol, cache),
        "htf_structure": check_htf_structure(symbol, cache),
        "order_block":   check_order_block(symbol, cache),
    }

    score = _normalised_score(signals, weights, avail)
    min_ok = sum(signals.values()) >= _MIN_SIGNALS["trend_long"]
    fire   = score >= thr and min_ok and passes_trend_long_filters(symbol, cache)

    return {"symbol": symbol, "regime": "TREND", "direction": "LONG",
            "score": round(score, 4), "signals": signals, "fire": fire}


async def score_trend_short(symbol: str, cache) -> dict:
    from signals.bear.oi_flush        import check_oi_long_flush
    from signals.bear.htf_lower_high  import check_htf_lower_high
    from signals.bear.bear_ob         import check_bear_ob_breakdown
    from signals.bear.funding_extreme import check_funding_extreme_positive
    from core.filter import passes_trend_short_filters

    weights = _cfg["weights"]["bear"]
    thr     = _BT_THRESHOLDS["trend_short"]
    avail   = _AVAILABLE["trend_short"]

    signals: dict[str, bool] = {
        "oi_flush":        check_oi_long_flush(symbol, cache),
        "htf_lower_high":  check_htf_lower_high(symbol, cache),
        "bear_ob":         check_bear_ob_breakdown(symbol, cache),
        "funding_extreme": check_funding_extreme_positive(symbol, cache),
    }

    score  = _normalised_score(signals, weights, avail)
    min_ok = sum(signals.values()) >= _MIN_SIGNALS["trend_short"]
    fire   = score >= thr and min_ok and passes_trend_short_filters(symbol, cache)

    return {"symbol": symbol, "regime": "TREND", "direction": "SHORT",
            "score": round(score, 4), "signals": signals, "fire": fire}


async def score_range_long(symbol: str, cache) -> dict:
    from signals.range.absorption       import check_absorption_ratio
    from signals.range.wyckoff_spring   import check_wyckoff_spring
    from signals.range.anchored_vwap    import check_anchored_vwap
    from signals.range.time_distribution import check_time_distribution
    from core.range_filter import passes_range_filters

    weights = _cfg["weights"]["range_long"]
    thr     = _BT_THRESHOLDS["range_long"]
    avail   = _AVAILABLE["range_long"]

    signals: dict[str, bool] = {
        "absorption":        check_absorption_ratio(symbol, cache),
        "wyckoff_spring":    check_wyckoff_spring(symbol, cache),
        "anchored_vwap":     check_anchored_vwap(symbol, cache),
        "time_distribution": check_time_distribution(symbol, cache),
    }

    score  = _normalised_score(signals, weights, avail)
    min_ok = signals.get("absorption") or signals.get("wyckoff_spring")
    fire   = score >= thr and min_ok and passes_range_filters(symbol, cache)

    return {"symbol": symbol, "regime": "RANGE", "direction": "LONG",
            "score": round(score, 4), "signals": signals, "fire": fire}


async def score_range_short(symbol: str, cache) -> dict:
    from signals.range.ask_absorption   import check_ask_absorption_ratio
    from signals.range.upthrust         import check_wyckoff_upthrust
    from signals.range.anchored_vwap    import check_anchored_vwap
    from signals.range.time_distribution import check_time_distribution
    from core.range_filter import passes_range_filters

    weights = _cfg["weights"]["range_short"]
    thr     = _BT_THRESHOLDS["range_short"]
    avail   = _AVAILABLE["range_short"]

    signals: dict[str, bool] = {
        "ask_absorption":    check_ask_absorption_ratio(symbol, cache),
        "upthrust":          check_wyckoff_upthrust(symbol, cache),
        "anchored_vwap":     check_anchored_vwap(symbol, cache),
        "time_distribution": check_time_distribution(symbol, cache),
    }

    score  = _normalised_score(signals, weights, avail)
    min_ok = signals.get("ask_absorption") or signals.get("upthrust")
    fire   = score >= thr and min_ok and passes_range_filters(symbol, cache)

    return {"symbol": symbol, "regime": "RANGE", "direction": "SHORT",
            "score": round(score, 4), "signals": signals, "fire": fire}


async def score_crash(symbol: str, cache) -> dict:
    from signals.crash.dead_cat       import check_dead_cat_setup
    from signals.crash.liq_grab_short import check_liq_grab_short
    from signals.bear.oi_flush        import check_oi_long_flush
    from core.filter import passes_crash_filters

    weights = _cfg["weights"]["crash"]
    thr     = _BT_THRESHOLDS["crash"]
    avail   = _AVAILABLE["crash"]

    signals: dict[str, bool] = {
        "dead_cat":       check_dead_cat_setup(symbol, cache),
        "liq_grab_short": check_liq_grab_short(symbol, cache),
        "oi_flush":       check_oi_long_flush(symbol, cache),
    }

    score  = _normalised_score(signals, weights, avail)
    min_ok = signals.get("dead_cat", False)
    fire   = score >= thr and min_ok and passes_crash_filters(symbol, cache)

    return {"symbol": symbol, "regime": "CRASH", "direction": "SHORT",
            "score": round(score, 4), "signals": signals, "fire": fire}
