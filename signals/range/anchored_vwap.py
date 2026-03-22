"""Anchored VWAP signal — price reclaims AVWAP anchored to range start."""

_PROXIMITY_PCT = 0.005   # price within 0.5 % of AVWAP counts as "at AVWAP"


def _compute_avwap(candles: list[dict]) -> float:
    """Cumulative (typical_price × volume) / cumulative_volume from candles[0]."""
    cum_pv = 0.0
    cum_v  = 0.0
    for c in candles:
        typical = (c["h"] + c["l"] + c["c"]) / 3
        cum_pv += typical * c["v"]
        cum_v  += c["v"]
    if cum_v == 0:
        return 0.0
    return cum_pv / cum_v


def check_anchored_vwap(symbol: str, cache) -> bool:
    """True when price reclaims the AVWAP anchored to the range start timestamp.

    Reclaim conditions:
    - prev_close ≤ avwap  (was at or below)
    - curr_close > avwap  (crossed back above)

    Falls back to full hourly history when no range start timestamp is stored.
    """
    anchor_ts = cache.get_range_start_timestamp(symbol)

    # Use hourly candles; up to 200 for the profile
    candles = cache.get_ohlcv(symbol, window=200, tf="1h")
    if len(candles) < 3:
        return False

    # Slice from anchor if available
    if anchor_ts is not None:
        anchored = [c for c in candles if c["ts"] >= anchor_ts]
        if len(anchored) < 3:
            anchored = candles  # not enough post-anchor data, use full window
    else:
        anchored = candles

    avwap = _compute_avwap(anchored)
    if avwap == 0.0:
        return False

    prev_close = anchored[-2]["c"]
    curr_close = anchored[-1]["c"]

    # Price was at or below AVWAP and now closes above it
    return prev_close <= avwap and curr_close > avwap
