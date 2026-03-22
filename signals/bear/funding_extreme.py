"""Funding rate extreme — extremely positive funding as contrarian short signal."""

# From config.yaml: funding.extreme_positive = 0.001 (0.1 % per 8 h)
_EXTREME_THRESHOLD = 0.001


def check_funding_extreme_positive(symbol: str, cache) -> bool:
    """True when the current funding rate is extremely positive.

    Extreme positive funding means longs are paying shorts an unusually high
    premium — the market is over-leveraged to the long side and primed for a
    squeeze when momentum fades.
    """
    rate = cache.get_funding_rate(symbol)
    return rate is not None and rate > _EXTREME_THRESHOLD
