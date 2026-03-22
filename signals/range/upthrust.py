"""Upthrust signal — Wyckoff upthrust at range resistance (SHORT)."""

_THRUST_DEPTH_MAX = 0.005   # wick may exceed range_high by up to 0.5 %
_VOL_SPIKE_MULT   = 1.5     # upthrust candle volume ≥ 1.5× vol MA


def check_wyckoff_upthrust(symbol: str, cache) -> bool:
    """True when price wicks above range_high then closes back inside the range.

    Classic Wyckoff upthrust (mirror of spring):
    1. Upthrust candle: high > range_high AND close < range_high (wick pierces, body inside)
    2. Breach is shallow: (high - range_high) / range_high ≤ 0.5 %
    3. Volume is elevated on the upthrust candle (supply entering)
    4. Next candle closes below range_high (confirms rejection)

    Requires at least 5 candles of history.
    """
    ohlcv      = cache.get_ohlcv(symbol, window=20, tf="15m")
    range_high = cache.get_range_high(symbol)

    if len(ohlcv) < 5 or range_high is None or range_high == 0:
        return False

    upthrust = ohlcv[-2]
    confirm  = ohlcv[-1]

    # Candle wicked above resistance but closed inside range
    if not (upthrust["h"] > range_high and upthrust["c"] < range_high):
        return False

    # Breach must be shallow
    depth_pct = (upthrust["h"] - range_high) / range_high
    if depth_pct > _THRUST_DEPTH_MAX:
        return False

    # Volume spike on upthrust candle
    vol_window = ohlcv[max(0, len(ohlcv) - 12) : -2]
    if not vol_window:
        return False
    vol_ma = sum(c["v"] for c in vol_window) / len(vol_window)
    if upthrust["v"] < vol_ma * _VOL_SPIKE_MULT:
        return False

    # Confirming candle closes below range_high
    return confirm["c"] < range_high
