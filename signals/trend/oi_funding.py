"""Open Interest + Funding Rate signal — trend long confirmation."""

_OI_RISE_THRESHOLD  = 0.005  # OI must grow ≥ 0.5 % vs 1-hour-ago reading
_FUNDING_LONG_MAX   = 0.0002 # allow slightly positive/neutral funding (≤0.02%/8h is not greed)


def check_oi_funding(symbol: str, cache) -> bool:
    """True when OI is rising AND funding rate is negative.

    Rising OI with negative funding means new longs are being added while
    shorts are still paying — strong accumulation signal.

    Spec:
        oi_change = (oi_now - oi_1h) / oi_1h  > 0.02
        funding < 0
    """
    oi_series = cache.get_oi_history(symbol, window=2)
    if len(oi_series) < 2:
        return False

    oi_now = oi_series[-1]
    oi_1h  = oi_series[-2]

    if oi_1h == 0:
        return False

    oi_change = (oi_now - oi_1h) / oi_1h

    funding = cache.get_funding_rate(symbol)
    if funding is None:
        return False

    return oi_change > _OI_RISE_THRESHOLD and funding < _FUNDING_LONG_MAX
