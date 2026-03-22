"""Ask absorption signal — buyers absorbed by sellers at range resistance (SHORT)."""

_PROXIMITY_PCT   = 0.01   # price within 1 % of range_high
_VOL_SPIKE_MULT  = 1.5    # current volume ≥ 1.5× vol MA
_BODY_RANGE_MAX  = 0.4    # body/range ratio < 0.4 (small body = absorption)


def check_ask_absorption_ratio(symbol: str, cache) -> bool:
    """True when a high-volume candle near range resistance has a small body.

    Mirror of bid absorption (check_absorption_ratio) but at the top of the
    range.  A spike-volume candle that barely closes higher near resistance
    signals that sellers are aggressively absorbing buy pressure — short setup.
    """
    ohlcv      = cache.get_ohlcv(symbol, window=20, tf="5m")
    range_high = cache.get_range_high(symbol)

    if len(ohlcv) < 5 or range_high is None or range_high == 0:
        return False

    last  = ohlcv[-1]
    price = last["c"]

    # Must be within 1 % below range high
    if (range_high - price) / range_high > _PROXIMITY_PCT:
        return False

    # Volume elevated vs rolling average
    vol_ma = cache.get_vol_ma(symbol, window=20, tf="5m")
    if vol_ma == 0.0 or last["v"] < vol_ma * _VOL_SPIKE_MULT:
        return False

    # Small body = buyers tried but sellers absorbed
    candle_range = last["h"] - last["l"]
    if candle_range == 0.0:
        return False

    body = abs(last["c"] - last["o"])
    return (body / candle_range) < _BODY_RANGE_MAX
