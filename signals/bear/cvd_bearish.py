"""Bearish CVD signal — cumulative delta diverging negatively."""


def check_cvd_bearish_div(symbol: str, cache) -> bool:
    """True when price makes a higher high but CVD makes a lower high.

    Bearish hidden divergence: buyers can't push CVD up despite price rising
    → sellers are distributing, reversal likely.

    Requires at least 3 data points (oldest at index -3, newest at -1).
    """
    prices = cache.get_closes(symbol, window=10, tf="5m")
    cvd    = cache.get_cvd(symbol,    window=10, tf="5m")
    if len(prices) < 3 or len(cvd) < 3:
        return False
    return prices[-1] > prices[-3] and cvd[-1] < cvd[-3]


def check_cvd_bearish(symbol: str, cache) -> bool:
    """True when CVD slope is negative — net selling pressure over last N candles.

    A falling CVD (cvd[-1] < cvd[-3]) without any price condition confirms
    clean downside flow — sellers in control.
    """
    cvd = cache.get_cvd(symbol, window=10, tf="5m")
    if len(cvd) < 3:
        return False
    return cvd[-1] < cvd[-3]
