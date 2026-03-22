"""Wyckoff spring signal — false breakdown below range support followed by recovery."""

_SPRING_DEPTH_MAX = 0.005   # wick may breach range_low by up to 0.5 % (false break)
_VOL_SPIKE_MULT   = 1.5     # spring candle volume ≥ 1.5× vol MA


def check_wyckoff_spring(symbol: str, cache) -> bool:
    """True when price wicks below range_low then closes back inside the range.

    Classic Wyckoff spring pattern:
    1. Spring candle: low < range_low AND close > range_low  (wick pierces, body inside)
    2. Breach is shallow: (range_low - low) / range_low ≤ 0.5 %
    3. Volume is elevated on the spring candle (supply absorbed)
    4. Next candle closes above range_low (confirms recovery)

    Requires at least 5 candles of history.
    """
    ohlcv     = cache.get_ohlcv(symbol, window=20, tf="15m")
    range_low = cache.get_range_low(symbol)

    if len(ohlcv) < 5 or range_low is None or range_low == 0:
        return False

    # The spring is the second-to-last candle; latest candle confirms recovery.
    spring  = ohlcv[-2]
    confirm = ohlcv[-1]

    # Candle wicked below but closed inside range
    if not (spring["l"] < range_low and spring["c"] > range_low):
        return False

    # Breach must be shallow
    depth_pct = (range_low - spring["l"]) / range_low
    if depth_pct > _SPRING_DEPTH_MAX:
        return False

    # Volume spike on spring candle (use up to 10 candles before the spring)
    vol_window = ohlcv[max(0, len(ohlcv) - 12) : -2]
    if not vol_window:
        return False
    vol_ma = sum(c["v"] for c in vol_window) / len(vol_window)
    if spring["v"] < vol_ma * _VOL_SPIKE_MULT:
        return False

    # Confirming candle closes above range_low
    return confirm["c"] > range_low
