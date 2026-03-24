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
    "trend_long":      {"oi_funding", "vpvr_support", "htf_structure", "order_block"},
    "trend_short":     {"oi_flush", "htf_lower_high", "bear_ob", "funding_extreme"},
    "range_long":      {"absorption", "wyckoff_spring", "anchored_vwap", "time_distribution"},
    "range_short":     {"ask_absorption", "upthrust", "anchored_vwap", "time_distribution"},
    "crash":           {"dead_cat", "liq_grab_short", "oi_flush"},
    "pump":            {"htf_structure", "oi_funding", "order_block"},
    "breakout_long":   {"htf_structure", "oi_funding", "absorption"},
    "breakout_short":  {"htf_lower_high", "oi_flush", "ask_absorption"},
}

# Minimum number of available signals that must fire before we consider the setup
_MIN_SIGNALS = {
    "trend_long":     2,
    "trend_short":    2,
    "range_long":     2,
    "range_short":    2,
    "crash":          2,
    "pump":           1,   # pump moves fast — 1 confirming signal is enough
    "breakout_long":  2,   # need at least 2 of 3 signals to avoid false breakouts
    "breakout_short": 2,
}

# Backtest-specific thresholds — lower than live because CVD/liq/options/whale signals
# are excluded from the denominator, leaving only 3-4 signals per regime.
_BT_THRESHOLDS = {
    "trend_long":     0.38,   # live: 0.65
    "trend_short":    0.42,   # live: 0.65
    "range_long":     0.38,   # live: 0.60
    "range_short":    0.38,   # live: 0.60
    "crash":          0.50,   # live: 0.75
    "pump":           0.40,   # live: 0.70  — pump: 1 of 3 signals is enough
    "breakout_long":  0.42,   # live: 0.60  — require 2 of 3 signals
    "breakout_short": 0.42,   # live: 0.60
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


async def score_pump(symbol: str, cache) -> dict:
    """PUMP regime scorer — parabolic upside.

    Reuses trend_long signals (htf_structure, oi_funding, order_block).
    Regime detection already confirmed the +12% 7-day move and EMA50 position;
    we need at least 1 signal to confirm momentum is genuine, not a fake spike.
    """
    from signals.trend.htf_structure import check_htf_structure
    from signals.trend.oi_funding    import check_oi_funding
    from signals.trend.order_block   import check_order_block
    from core.filter import passes_pump_filters

    weights = _cfg["weights"]["pump"]
    thr     = _BT_THRESHOLDS["pump"]
    avail   = _AVAILABLE["pump"]

    signals: dict[str, bool] = {
        "htf_structure": check_htf_structure(symbol, cache),
        "oi_funding":    check_oi_funding(symbol, cache),
        "order_block":   check_order_block(symbol, cache),
    }

    score  = _normalised_score(signals, weights, avail)
    min_ok = sum(signals.values()) >= _MIN_SIGNALS["pump"]
    fire   = score >= thr and min_ok and passes_pump_filters(symbol, cache)

    return {"symbol": symbol, "regime": "PUMP", "direction": "LONG",
            "score": round(score, 4), "signals": signals, "fire": fire}


async def score_breakout_long(symbol: str, cache) -> dict:
    """BREAKOUT LONG scorer — price just left range high with volume."""
    from signals.trend.htf_structure  import check_htf_structure
    from signals.trend.oi_funding     import check_oi_funding
    from signals.range.absorption     import check_absorption_ratio
    from core.filter import passes_breakout_long_filters

    weights = _cfg["weights"]["breakout_long"]
    thr     = _BT_THRESHOLDS["breakout_long"]
    avail   = _AVAILABLE["breakout_long"]

    signals: dict[str, bool] = {
        "htf_structure": check_htf_structure(symbol, cache),
        "oi_funding":    check_oi_funding(symbol, cache),
        "absorption":    check_absorption_ratio(symbol, cache),
    }

    score  = _normalised_score(signals, weights, avail)
    min_ok = sum(signals.values()) >= _MIN_SIGNALS["breakout_long"]
    fire   = score >= thr and min_ok and passes_breakout_long_filters(symbol, cache)

    return {"symbol": symbol, "regime": "BREAKOUT", "direction": "LONG",
            "score": round(score, 4), "signals": signals, "fire": fire}


async def score_breakout_short(symbol: str, cache) -> dict:
    """BREAKOUT SHORT scorer — price just broke below range low with volume."""
    from signals.bear.htf_lower_high import check_htf_lower_high
    from signals.bear.oi_flush       import check_oi_long_flush
    from signals.range.ask_absorption import check_ask_absorption_ratio
    from core.filter import passes_breakout_short_filters

    weights = _cfg["weights"]["breakout_short"]
    thr     = _BT_THRESHOLDS["breakout_short"]
    avail   = _AVAILABLE["breakout_short"]

    signals: dict[str, bool] = {
        "htf_lower_high": check_htf_lower_high(symbol, cache),
        "oi_flush":       check_oi_long_flush(symbol, cache),
        "ask_absorption": check_ask_absorption_ratio(symbol, cache),
    }

    score  = _normalised_score(signals, weights, avail)
    min_ok = sum(signals.values()) >= _MIN_SIGNALS["breakout_short"]
    fire   = score >= thr and min_ok and passes_breakout_short_filters(symbol, cache)

    return {"symbol": symbol, "regime": "BREAKOUT", "direction": "SHORT",
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
