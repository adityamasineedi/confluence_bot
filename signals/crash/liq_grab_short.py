"""Liquidity grab short — bearish liq sweep above a swing high then reversal down."""

_LOOKBACK        = 20    # 15-minute candles to find the swing high
_SWEEP_MAX_PCT   = 0.005 # wick above swing high must be ≤ 0.5 % (shallow sweep)
_VOL_SPIKE_MULT  = 1.5   # sweep candle volume ≥ 1.5× vol MA
_MIN_BEAR_CLOSES = 2     # at least 2 bearish closes in last 3 candles after sweep


def check_liq_grab_short(symbol: str, cache) -> bool:
    """True when price sweeps above a recent swing high then closes back below it.

    In a CRASH regime this pattern signals a liquidity grab before continuation
    lower — stops above swing highs are triggered, then price reverses.

    Conditions:
    1. Swing high = max(high) over the preceding _LOOKBACK candles (exc. last 3).
    2. Sweep candle: high > swing_high AND close < swing_high.
    3. Sweep is shallow: (high - swing_high) / swing_high ≤ 0.5 %.
    4. Volume spike on sweep candle.
    5. ≥ 2 of the last 3 candles are bearish (close < open).
    """
    ohlcv = cache.get_ohlcv(symbol, window=_LOOKBACK + 5, tf="15m")
    if len(ohlcv) < _LOOKBACK + 3:
        return False

    reference = ohlcv[: -3]   # exclude last 3 candles to find prior swing high
    swing_high = max(c["h"] for c in reference[-_LOOKBACK:])

    recent      = ohlcv[-3:]
    sweep_candle = ohlcv[-2]   # the sweep happened on the second-to-last candle

    # Sweep: wicked above swing high but closed below it
    if not (sweep_candle["h"] > swing_high and sweep_candle["c"] < swing_high):
        return False

    # Shallow sweep
    sweep_pct = (sweep_candle["h"] - swing_high) / swing_high
    if sweep_pct > _SWEEP_MAX_PCT:
        return False

    # Volume spike
    vol_window = ohlcv[max(0, len(ohlcv) - 12) : -2]
    if not vol_window:
        return False
    vol_ma = sum(c["v"] for c in vol_window) / len(vol_window)
    if sweep_candle["v"] < vol_ma * _VOL_SPIKE_MULT:
        return False

    # Bearish closes in last 3 candles
    bear_closes = sum(1 for c in recent if c["c"] < c["o"])
    return bear_closes >= _MIN_BEAR_CLOSES
