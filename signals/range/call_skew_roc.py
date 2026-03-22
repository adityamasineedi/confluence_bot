"""Call skew Rate of Change — rapid shift toward calls as a range-long signal."""

_ROC_WINDOW    = 6      # compare current skew vs N readings ago
_ROC_THRESHOLD = 0.01   # skew must rise ≥ 0.01 (1 vol point) over the window


def check_call_skew_roc(symbol: str, cache) -> bool:
    """True when the 25-delta call/put skew is rising rapidly.

    A sharp positive move in skew (calls becoming more expensive relative to
    puts) signals that traders are aggressively buying upside protection —
    bullish confirmation at range support.

    RoC = skew[-1] - skew[-N]  (absolute, not percentage — skew is already %)
    Requires at least _ROC_WINDOW + 1 readings.
    """
    window   = _ROC_WINDOW + 1
    skew_series = cache.get_skew_history(symbol, n=window)
    if len(skew_series) < window:
        return False

    roc = skew_series[-1] - skew_series[-window]
    return roc >= _ROC_THRESHOLD
