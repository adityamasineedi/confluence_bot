"""Options flow signal — 25-delta skew as bullish trend confirmation."""

# Skew = call_iv - put_iv (from Deribit cache)
# For trend LONG: skew must be ≥ 0 (calls not cheaper than puts → bullish bias)
_SKEW_LONG_MIN = 0.0


def check_options_flow(symbol: str, cache) -> bool:
    """True when the 25-delta options skew is non-negative (calls ≥ puts in IV).

    A non-negative skew in a trend regime means options traders are positioning
    for upside — consistent with a trend long entry.  Uses Deribit data cached
    via the skew series.
    """
    skew_series = cache.get_skew_history(symbol, n=1)
    if not skew_series:
        return False

    return skew_series[-1] >= _SKEW_LONG_MIN
