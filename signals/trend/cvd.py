"""Cumulative Volume Delta — bullish pressure signals for TREND regime."""


def check_cvd_bullish(symbol: str, cache) -> bool:
    """True when buying pressure is building over the last 48 × 1H candles (2 days).

    Compares the average CVD of the second half of the window to the first half.
    A rising average means net buying is accelerating, not fading.
    Requires at least 12 closed 1H candles to avoid noise during warmup.
    """
    cvd = cache.get_cvd(symbol, window=48, tf="1h")
    if not cvd or len(cvd) < 12:
        return False
    mid = len(cvd) // 2
    first_half_avg  = sum(cvd[:mid]) / mid
    second_half_avg = sum(cvd[mid:]) / (len(cvd) - mid)
    return second_half_avg > first_half_avg


def check_cvd_divergence(symbol: str, cache) -> bool:
    """Bullish divergence: price making a lower low but CVD making a higher low.

    Uses 24 × 4H bars (4 days) so both the price and flow comparison span
    a meaningful trend segment rather than intraday noise.
    Compares the minimum of the first half to the minimum of the second half
    for both series, which is more robust than a single point comparison.
    """
    prices = cache.get_closes(symbol, window=24, tf="4h")
    cvd    = cache.get_cvd(symbol,    window=24, tf="4h")
    if not prices or not cvd or len(prices) < 8 or len(cvd) < 8:
        return False
    quarter     = len(prices) // 4
    price_early = min(prices[:quarter * 2])
    price_late  = min(prices[quarter * 2:])
    cvd_early   = min(cvd[:quarter * 2])
    cvd_late    = min(cvd[quarter * 2:])
    return price_late < price_early and cvd_late > cvd_early
