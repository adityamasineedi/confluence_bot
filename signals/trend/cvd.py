"""Cumulative Volume Delta — bullish divergence signal for TREND regime."""


def check_cvd_divergence(symbol: str, cache) -> bool:
    """True when price is making a lower low but CVD is making a higher low.

    Bullish hidden divergence: sellers can't push CVD down despite price falling
    → buyers are absorbing, reversal likely.

    Requires at least 3 data points (oldest is index -3, newest is -1).
    """
    prices = cache.get_closes(symbol, window=10, tf="5m")
    cvd    = cache.get_cvd(symbol,    window=10, tf="5m")
    if len(prices) < 3 or len(cvd) < 3:
        return False
    return prices[-1] < prices[-3] and cvd[-1] > cvd[-3]


def check_cvd_bullish(symbol: str, cache) -> bool:
    """True when CVD slope is positive — net buying pressure over last N candles.

    Uses the same divergence check as the primary signal; a positive slope
    (cvd[-1] > cvd[-3]) without the price condition confirms clean upside flow.
    """
    cvd = cache.get_cvd(symbol, window=10, tf="5m")
    if len(cvd) < 3:
        return False
    return cvd[-1] > cvd[-3]
