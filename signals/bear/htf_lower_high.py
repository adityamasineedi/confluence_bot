"""HTF lower high signal — bearish weekly structure for SHORT bias."""


def check_htf_lower_high(symbol: str, cache) -> bool:
    """True when the most recent weekly close is below the prior weekly close.

    A falling weekly close series signals bearish macro structure — the uptrend
    has failed to produce a new higher close.  Requires at least 2 weekly bars.
    """
    weekly = cache.get_ohlcv(symbol, window=4, tf="1w")
    if len(weekly) < 2:
        return False
    return weekly[-1]["c"] < weekly[-2]["c"]
