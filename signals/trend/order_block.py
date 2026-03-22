"""Bullish Order Block detection — institutional demand zones."""

_IMPULSE_CANDLES = 3     # consecutive bullish candles that define an impulse
_IMPULSE_MIN_PCT = 0.01  # each impulse candle body ≥ 1 % of open price
_LOOKBACK        = 60    # 1H candles to scan


def check_order_block(symbol: str, cache) -> bool:
    """True when price is retesting a bullish order block.

    Algorithm:
    1. Scan 1H candles for a bullish impulse: _IMPULSE_CANDLES consecutive
       bullish bars each with body ≥ 1 % of open.
    2. The candle immediately BEFORE the impulse is the bullish OB
       (last bearish candle before strong buying).
    3. OB zone = [ob_low, ob_high].
    4. Signal fires when current price is inside the OB zone AND the current
       candle is bullish (close > open) — demand zone holding.
    5. Zone is invalidated if any close below ob_low occurred after the OB.
    """
    ohlcv = cache.get_ohlcv(symbol, window=_LOOKBACK, tf="1h")
    if len(ohlcv) < _IMPULSE_CANDLES + 2:
        return False

    ob_zone = None
    for i in range(len(ohlcv) - _IMPULSE_CANDLES - 1):
        impulse = ohlcv[i + 1 : i + 1 + _IMPULSE_CANDLES]
        if not all(
            c["c"] > c["o"] and (c["c"] - c["o"]) / c["o"] >= _IMPULSE_MIN_PCT
            for c in impulse
        ):
            continue

        ob_candle = ohlcv[i]
        ob_high   = ob_candle["h"]
        ob_low    = ob_candle["l"]

        # Invalidate if any close below ob_low after the OB
        post_ob = ohlcv[i + 1 :]
        if any(c["c"] < ob_low for c in post_ob[:-1]):
            continue

        ob_zone = (ob_low, ob_high)

    if ob_zone is None:
        return False

    ob_low, ob_high = ob_zone
    current = ohlcv[-1]
    price   = current["c"]

    return ob_low <= price <= ob_high and current["c"] > current["o"]
