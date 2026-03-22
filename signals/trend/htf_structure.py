"""Higher Time Frame structure signal — weekly bullish continuation."""


def check_htf_structure(symbol: str, cache) -> bool:
    """True when the most recent weekly close exceeds the prior weekly close.

    A rising weekly close series confirms the macro uptrend is intact.
    Requires at least 2 weekly bars (oldest is index -2, newest is -1).
    """
    weekly = cache.get_ohlcv(symbol, window=4, tf="1w")
    if len(weekly) < 2:
        return False
    return weekly[-1]["c"] > weekly[-2]["c"]
