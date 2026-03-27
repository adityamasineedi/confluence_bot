"""Anchored VWAP signal — price reclaims AVWAP anchored to range start.

Also provides VWAP standard-deviation band signals:
  check_vwap_oversold   — price below VWAP-2σ (range long entry)
  check_vwap_overbought — price above VWAP+2σ (range short entry)

VWAP deviation bands are one of the most reliable intraday mean-reversion tools.
Institutional algos routinely fade 2σ extensions back toward VWAP.
"""

_PROXIMITY_PCT = 0.005   # price within 0.5 % of AVWAP counts as "at AVWAP"
_STD_LONG_BAND  = 2.0    # enter long when price is this many σ below VWAP
_STD_SHORT_BAND = 2.0    # enter short when price is this many σ above VWAP


def _compute_avwap_with_std(candles: list[dict]) -> tuple[float, float]:
    """Return (vwap, std_dev) anchored from candles[0].

    Uses the volume-weighted variance formula:
        variance = Σ(tp² × vol) / Σ(vol) − vwap²
    Returns (0.0, 0.0) when volume is zero.
    """
    cum_pv  = 0.0
    cum_pv2 = 0.0
    cum_v   = 0.0
    for c in candles:
        tp       = (c["h"] + c["l"] + c["c"]) / 3.0
        cum_pv  += tp * c["v"]
        cum_pv2 += tp * tp * c["v"]
        cum_v   += c["v"]
    if cum_v == 0.0:
        return 0.0, 0.0
    vwap     = cum_pv / cum_v
    variance = max(cum_pv2 / cum_v - vwap * vwap, 0.0)
    return vwap, variance ** 0.5


def _compute_avwap(candles: list[dict]) -> float:
    """Cumulative (typical_price × volume) / cumulative_volume from candles[0]."""
    vwap, _ = _compute_avwap_with_std(candles)
    return vwap


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

    curr_close = anchored[-1]["c"]
    if curr_close <= avwap:
        return False

    # Price is above AVWAP and was below it within the last 3 bars (recent reclaim)
    recent_closes = [c["c"] for c in anchored[-4:-1]]
    return any(c <= avwap for c in recent_closes)


def _get_anchored_candles(symbol: str, cache) -> list[dict]:
    """Return candles sliced from the range anchor (or full 200-bar window)."""
    anchor_ts = cache.get_range_start_timestamp(symbol)
    candles   = cache.get_ohlcv(symbol, window=200, tf="1h")
    if len(candles) < 3:
        return []
    if anchor_ts is not None:
        anchored = [c for c in candles if c["ts"] >= anchor_ts]
        return anchored if len(anchored) >= 3 else candles
    return candles


def check_vwap_oversold(symbol: str, cache) -> bool:
    """True when price is below VWAP - 2σ — statistically oversold, fade to VWAP.

    This is a range-long signal: price has stretched too far below the session
    VWAP and is likely to mean-revert upward.  Used in range_scorer as an
    additional entry confirmation alongside absorption / wyckoff_spring.
    """
    anchored = _get_anchored_candles(symbol, cache)
    if not anchored:
        return False

    vwap, std = _compute_avwap_with_std(anchored)
    if vwap == 0.0 or std == 0.0:
        return False

    price     = anchored[-1]["c"]
    lower_band = vwap - _STD_LONG_BAND * std
    return price < lower_band


def check_vwap_overbought(symbol: str, cache) -> bool:
    """True when price is above VWAP + 2σ — statistically overbought, fade to VWAP.

    This is a range-short signal: price has stretched too far above the session
    VWAP and is likely to mean-revert downward.  Used in range_short_scorer.
    """
    anchored = _get_anchored_candles(symbol, cache)
    if not anchored:
        return False

    vwap, std = _compute_avwap_with_std(anchored)
    if vwap == 0.0 or std == 0.0:
        return False

    price      = anchored[-1]["c"]
    upper_band = vwap + _STD_SHORT_BAND * std
    return price > upper_band
