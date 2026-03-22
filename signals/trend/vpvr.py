"""Volume Profile / Visible Range (VPVR) — POC reclaim signal."""

_LOOKBACK   = 200    # hourly candles for the profile
_N_BINS     = 100    # histogram resolution
_VOL_MA_WIN = 20     # candles for volume moving-average
_VOL_MULT   = 1.3    # current volume must exceed vol_ma × this


def _compute_poc(candles: list[dict]) -> float:
    """Return the price level with the highest volume-weighted frequency.

    Uses typical price  (H + L + C) / 3  as the representative price per bar.
    Bins cover [min_low, max_high] in _N_BINS equal steps.
    """
    if not candles:
        return 0.0

    lo  = min(c["l"] for c in candles)
    hi  = max(c["h"] for c in candles)
    if hi <= lo:
        return (lo + hi) / 2

    bin_size = (hi - lo) / _N_BINS
    volume_bins: list[float] = [0.0] * _N_BINS

    for c in candles:
        typical = (c["h"] + c["l"] + c["c"]) / 3
        idx = int((typical - lo) / bin_size)
        idx = min(idx, _N_BINS - 1)  # clamp top edge
        volume_bins[idx] += c["v"]

    poc_idx  = volume_bins.index(max(volume_bins))
    poc_price = lo + (poc_idx + 0.5) * bin_size
    return poc_price


def check_vpvr_reclaim(symbol: str, cache) -> bool:
    """True when price reclaims the Point of Control (POC) on above-average volume.

    Conditions:
    - prev_close < poc  (price was below POC)
    - curr_close >= poc (price has crossed back above)
    - curr_volume > vol_ma × 1.3  (reclaim is volume-confirmed)
    """
    candles = cache.get_ohlcv(symbol, window=_LOOKBACK, tf="1h")
    if len(candles) < _VOL_MA_WIN + 2:
        return False

    poc = _compute_poc(candles)
    if poc == 0.0:
        return False

    prev_close = candles[-2]["c"]
    curr_close = candles[-1]["c"]
    curr_vol   = candles[-1]["v"]

    if prev_close >= poc:
        return False  # wasn't below POC, no reclaim

    # Volume moving average over preceding window (exclude current candle)
    vol_window = candles[-_VOL_MA_WIN - 1 : -1]
    vol_ma = sum(c["v"] for c in vol_window) / len(vol_window)

    return curr_close >= poc and curr_vol > vol_ma * _VOL_MULT
