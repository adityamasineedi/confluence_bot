"""Bid absorption signal — buyers absorbing sell pressure at range support (LONG)."""


def check_absorption_ratio(symbol: str, cache) -> bool:
    """Return True when a high-volume candle near range support has a small body.

    Logic:
    - Price must be within 1 % of cache range_low (confirming we're at support).
    - Current candle volume must be >= 1.5x the rolling volume MA (unusual activity).
    - Body/range ratio of that candle must be < 0.4 (long wick, contained close =
      sellers tried hard but buyers absorbed the pressure).
    """
    ohlcv = cache.get_ohlcv(symbol, window=20, tf="5m")
    range_low = cache.get_range_low(symbol)

    if len(ohlcv) < 5 or range_low is None:
        return False

    last = ohlcv[-1]
    price = last["c"]

    # Must be within 1 % above range low
    if range_low == 0 or (price - range_low) / range_low > 0.01:
        return False

    # Volume must be elevated vs rolling average
    vol_ma = cache.get_vol_ma(symbol, window=20, tf="5m")
    if vol_ma == 0.0 or last["v"] < vol_ma * 1.5:
        return False

    # Body/range ratio: small = price contained despite high sell volume
    candle_range = last["h"] - last["l"]
    if candle_range == 0.0:
        return False

    body = abs(last["c"] - last["o"])
    return (body / candle_range) < 0.4


def check_absorption(symbol: str, cache) -> bool:
    """Return True when bid absorption is detected at range support.

    Conditions to implement:
    - Price at or near range low / support level
    - Large sell volume absorbed without significant price decline (delta negative but price holds)
    - Order book shows stacked bids being refreshed

    TODO: detect range support level via rolling min
    TODO: compute delta during high-volume bars near support
    TODO: define absorption threshold in config.yaml
    """
    # TODO: ohlcv = cache.get_ohlcv(symbol, window=50, tf="5m")
    # TODO: check price proximity to range low
    # TODO: compute sell volume absorbed vs price movement
    return False
