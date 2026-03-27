"""Fair Value Gap (FVG) — unfilled price imbalance zones.

An FVG forms when a candle moves so fast that it leaves a gap between the previous
candle's high and the next candle's low (bullish) or vice versa (bearish).  These
gaps act as magnets — price returns to fill them ~65 % of the time when trend-aligned.

Bullish FVG:  candle[i-2].high  < candle[i].low    → unfilled gap above
Bearish FVG:  candle[i-2].low   > candle[i].high   → unfilled gap below

Signal fires when:
  - An unfilled FVG exists within the last MAX_AGE_BARS bars
  - Current price is inside the gap zone (retesting it)
  - For bullish FVG: current close is bullish (close > open) — demand holding
  - For bearish FVG: current close is bearish (close < open) — supply holding

Timeframe: 1H for trend confluence; scans last LOOKBACK bars.
Minimum gap size: 0.1 % of mid-price to filter micro-wicks from genuine imbalances.
"""

_LOOKBACK       = 50    # 1H candles to scan for gaps
_MAX_AGE_BARS   = 30    # FVG older than this is stale (price likely won't retest)
_MIN_GAP_PCT    = 0.001 # minimum gap size: 0.1 % of price (filters noise)


def check_fvg_bullish(symbol: str, cache) -> bool:
    """True when price is currently inside an unfilled bullish FVG on 1H.

    Bullish FVG:  gap_low  = candle[i-2].high
                  gap_high = candle[i].low
                  gap_low < gap_high  (genuine imbalance)

    Retest condition: current price is between gap_low and gap_high
    with a bullish close (close > open) confirming demand holding.
    """
    candles = cache.get_ohlcv(symbol, window=_LOOKBACK, tf="1h")
    if len(candles) < 5:
        return False

    current      = candles[-1]
    price        = current["c"]
    is_bullish_c = current["c"] > current["o"]

    # Scan bars excluding the last 2 (need candle[i+1] to exist)
    for i in range(len(candles) - 3, max(len(candles) - _MAX_AGE_BARS - 3, 1), -1):
        gap_low  = candles[i - 1]["h"]   # high of bar before the impulse
        gap_high = candles[i + 1]["l"]   # low of bar after the impulse

        if gap_high <= gap_low:
            continue  # not a gap

        mid = (gap_high + gap_low) / 2.0
        if mid == 0.0 or (gap_high - gap_low) / mid < _MIN_GAP_PCT:
            continue  # too small to be meaningful

        # Check gap not already filled: no candle after formation closed below gap_low
        filled = False
        for j in range(i + 2, len(candles) - 1):
            if candles[j]["l"] < gap_low:
                filled = True
                break
        if filled:
            continue

        # Price is retesting the gap zone
        if gap_low <= price <= gap_high and is_bullish_c:
            return True

    return False


def check_fvg_bearish(symbol: str, cache) -> bool:
    """True when price is currently inside an unfilled bearish FVG on 1H.

    Bearish FVG:  gap_high = candle[i-2].low
                  gap_low  = candle[i].high
                  gap_high > gap_low  (genuine imbalance)

    Retest condition: current price is between gap_low and gap_high
    with a bearish close (close < open) confirming supply holding.
    """
    candles = cache.get_ohlcv(symbol, window=_LOOKBACK, tf="1h")
    if len(candles) < 5:
        return False

    current       = candles[-1]
    price         = current["c"]
    is_bearish_c  = current["c"] < current["o"]

    for i in range(len(candles) - 3, max(len(candles) - _MAX_AGE_BARS - 3, 1), -1):
        gap_high = candles[i - 1]["l"]   # low of bar before the impulse
        gap_low  = candles[i + 1]["h"]   # high of bar after the impulse

        if gap_high <= gap_low:
            continue

        mid = (gap_high + gap_low) / 2.0
        if mid == 0.0 or (gap_high - gap_low) / mid < _MIN_GAP_PCT:
            continue

        # Check not already filled: no candle after formation closed above gap_high
        filled = False
        for j in range(i + 2, len(candles) - 1):
            if candles[j]["h"] > gap_high:
                filled = True
                break
        if filled:
            continue

        if gap_low <= price <= gap_high and is_bearish_c:
            return True

    return False
