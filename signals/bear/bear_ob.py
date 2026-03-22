"""Bearish Order Block — institutional supply zone retest for short entries."""

_IMPULSE_CANDLES = 3     # number of consecutive bearish candles that define an impulse
_IMPULSE_MIN_PCT = 0.01  # each impulse candle must be at least 1 % body
_LOOKBACK        = 60    # 1H candles to scan for bearish impulse


def check_bear_ob_breakdown(symbol: str, cache) -> bool:
    """True when price retests a bearish order block and shows rejection.

    Algorithm:
    1. Scan recent 1H candles for a bearish impulse: _IMPULSE_CANDLES consecutive
       bearish bars each with body ≥ 1 % of open price.
    2. The candle immediately BEFORE the impulse is the bearish OB
       (last bullish candle before strong selling).
    3. OB zone = [ob_low, ob_high] = [ob_candle["l"], ob_candle["h"]].
    4. Signal fires when the current price is inside the OB zone AND
       the current candle is bearish (close < open) — rejection confirmed.
    5. Zone is invalidated if any close above ob_high occurred after the OB.
    """
    ohlcv = cache.get_ohlcv(symbol, window=_LOOKBACK, tf="1h")
    if len(ohlcv) < _IMPULSE_CANDLES + 2:
        return False

    # Search from oldest to newest for the most recent valid bearish OB
    ob_zone = None
    for i in range(len(ohlcv) - _IMPULSE_CANDLES - 1):
        # Check _IMPULSE_CANDLES consecutive bearish bars starting at i+1
        impulse = ohlcv[i + 1 : i + 1 + _IMPULSE_CANDLES]
        if not all(
            c["c"] < c["o"] and (c["o"] - c["c"]) / c["o"] >= _IMPULSE_MIN_PCT
            for c in impulse
        ):
            continue

        ob_candle = ohlcv[i]
        ob_high   = ob_candle["h"]
        ob_low    = ob_candle["l"]

        # Invalidate if any close above ob_high after the OB was formed
        post_ob = ohlcv[i + 1 :]
        if any(c["c"] > ob_high for c in post_ob[:-1]):  # exclude current candle
            continue

        ob_zone = (ob_low, ob_high)
        # Keep the most recent valid OB (loop will overwrite with newer if found)

    if ob_zone is None:
        return False

    ob_low, ob_high = ob_zone
    current = ohlcv[-1]
    price   = current["c"]

    # Price inside OB and bearish candle (rejection)
    return ob_low <= price <= ob_high and current["c"] < current["o"]
