"""
signals/bear/cvd_bearish.py
Bearish CVD signals — selling pressure confirmation for BEAR/CRASH regime.

Uses 1H bars for slope (matches trend CVD fix) and 4H bars for
divergence (enough context to distinguish a real divergence from noise).
"""


def check_cvd_bearish(symbol: str, cache) -> bool:
    """True when selling pressure is building over the last 48 × 1H candles.

    Compares the average CVD of the second half vs first half of the window.
    A falling average means net selling is accelerating.
    Requires at least 12 closed 1H candles to avoid warmup noise.
    """
    cvd = cache.get_cvd(symbol, window=48, tf="1h")
    if not cvd or len(cvd) < 12:
        return False
    mid = len(cvd) // 2
    first_half_avg  = sum(cvd[:mid]) / mid
    second_half_avg = sum(cvd[mid:]) / (len(cvd) - mid)
    return second_half_avg < first_half_avg   # selling accelerating


def check_cvd_bearish_div(symbol: str, cache) -> bool:
    """Bearish divergence: price making higher high but CVD making lower high.

    Institutions selling into retail buying — classic distribution signal.
    Uses 24 × 4H bars (4 days) for meaningful trend comparison.
    """
    prices = cache.get_closes(symbol, window=24, tf="4h")
    cvd    = cache.get_cvd(symbol,    window=24, tf="4h")
    if not prices or not cvd or len(prices) < 8 or len(cvd) < 8:
        return False
    quarter      = len(prices) // 4
    price_early  = max(prices[:quarter * 2])
    price_late   = max(prices[quarter * 2:])
    cvd_early    = max(cvd[:quarter * 2])
    cvd_late     = max(cvd[quarter * 2:])
    # Price made higher high but CVD did not → sellers distributing
    return price_late > price_early and cvd_late < cvd_early
