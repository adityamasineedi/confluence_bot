"""Dead cat bounce signal — relief rally into resistance after crash, re-short setup."""

_CRASH_WINDOW    = 12    # 1H candles to measure the initial crash
_CRASH_MIN_DROP  = 0.08  # initial decline must be ≥ 8 %
_BOUNCE_MIN_PCT  = 0.03  # bounce must be ≥ 3 % from crash low
_BOUNCE_MAX_PCT  = 0.50  # bounce must not exceed 50 % of crash range (still a dead cat)
_VOL_DECAY_RATIO = 0.80  # bounce average volume ≤ 80 % of crash average volume


def check_dead_cat_setup(symbol: str, cache) -> bool:
    """True when a weak dead-cat bounce follows a sharp crash.

    Algorithm:
    1. Identify crash: highest close in first half of window vs subsequent low.
       Drop ≥ 8 % qualifies as a crash.
    2. Identify bounce from that low to current price.
       Bounce must be [3 %, 50 %] of the crash range.
    3. Volume decay: average volume over the bounce is ≤ 80 % of crash average.
    4. CVD slope still negative (sellers haven't capitulated).
    """
    ohlcv = cache.get_ohlcv(symbol, window=_CRASH_WINDOW * 2, tf="1h")
    if len(ohlcv) < _CRASH_WINDOW + 3:
        return False

    crash_window = ohlcv[: _CRASH_WINDOW]
    crash_high   = max(c["h"] for c in crash_window)
    crash_low    = min(c["l"] for c in crash_window)

    if crash_high == 0:
        return False

    drop_pct = (crash_high - crash_low) / crash_high
    if drop_pct < _CRASH_MIN_DROP:
        return False

    crash_range = crash_high - crash_low
    bounce_window = ohlcv[_CRASH_WINDOW:]
    if not bounce_window:
        return False

    current_price = bounce_window[-1]["c"]
    bounce_pct    = (current_price - crash_low) / crash_range

    if not (_BOUNCE_MIN_PCT <= bounce_pct <= _BOUNCE_MAX_PCT):
        return False

    # Volume decay check
    crash_vol_avg  = sum(c["v"] for c in crash_window) / len(crash_window)
    bounce_vol_avg = sum(c["v"] for c in bounce_window) / len(bounce_window)
    if crash_vol_avg > 0 and bounce_vol_avg > crash_vol_avg * _VOL_DECAY_RATIO:
        return False  # bounce volume too strong — not a dead cat

    # CVD still bearish
    cvd = cache.get_cvd(symbol, window=6, tf="1h")
    if len(cvd) >= 3 and cvd[-1] >= cvd[-3]:
        return False  # CVD rising — sellers not in control

    return True
