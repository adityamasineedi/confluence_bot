"""Options skew signal — 25-delta risk reversal for range direction confirmation."""

# 25-delta skew = call_iv - put_iv (Deribit)
# Negative skew → puts bid (fear), positive → calls bid (greed)
# For range LONG: skew ≥ -0.05 (puts not significantly bid, no crash fear)
_SKEW_LONG_MIN = -0.05   # skew floor for a long signal


def check_options_skew(symbol: str, cache) -> bool:
    """True when the 25-delta options skew supports a range-long entry.

    A skew at or above -0.05 means put pricing is not extreme, consistent
    with a balanced/recovering market rather than a panic-driven sell-off.
    This is used as a soft confirmation, not a primary signal.
    """
    skew_series = cache.get_skew_history(symbol, n=1)
    if not skew_series:
        return False

    skew = skew_series[-1]
    return skew >= _SKEW_LONG_MIN
