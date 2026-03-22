"""Time distribution signal — Value Area Low for range-long entries."""

_LOOKBACK  = 64    # 15-minute candles (≈ 16 hours)
_N_BINS    = 40    # histogram resolution
_VALUE_PCT = 0.70  # Value Area = 70 % of total time (standard market profile)


def _compute_val(candles: list[dict]) -> float:
    """Return the Value Area Low price.

    Methodology:
    1. Build time-at-price histogram: each candle contributes 1 unit to every
       bin that its [low, high] range spans.
    2. POC = bin with the most time.
    3. Expand symmetrically from POC until 70 % of total time is covered
       (Value Area).  VAL is the bottom boundary of that area.
    """
    if not candles:
        return 0.0

    lo  = min(c["l"] for c in candles)
    hi  = max(c["h"] for c in candles)
    if hi <= lo:
        return lo

    bin_size = (hi - lo) / _N_BINS
    time_bins = [0] * _N_BINS

    for c in candles:
        first = int((c["l"] - lo) / bin_size)
        last  = int((c["h"] - lo) / bin_size)
        first = max(0, min(first, _N_BINS - 1))
        last  = max(0, min(last,  _N_BINS - 1))
        for i in range(first, last + 1):
            time_bins[i] += 1

    total_time = sum(time_bins)
    target = total_time * _VALUE_PCT

    poc_idx = time_bins.index(max(time_bins))
    covered = time_bins[poc_idx]
    low_idx  = poc_idx
    high_idx = poc_idx

    while covered < target:
        expand_low  = time_bins[low_idx  - 1] if low_idx  > 0            else 0
        expand_high = time_bins[high_idx + 1] if high_idx < _N_BINS - 1 else 0
        if expand_low >= expand_high and low_idx > 0:
            low_idx -= 1
            covered += time_bins[low_idx]
        elif high_idx < _N_BINS - 1:
            high_idx += 1
            covered  += time_bins[high_idx]
        else:
            break

    val = lo + low_idx * bin_size
    return val


def check_time_distribution(symbol: str, cache) -> bool:
    """True when current price is at or below the Value Area Low.

    Price at VAL means the market is at the cheapest accepted level within
    the recent distribution — a mean-reversion long opportunity in a range.
    """
    candles = cache.get_ohlcv(symbol, window=_LOOKBACK, tf="15m")
    if len(candles) < 10:
        return False

    val   = _compute_val(candles)
    price = candles[-1]["c"]

    return price <= val
