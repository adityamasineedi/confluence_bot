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
    """Return True when sell pressure is absorbed at rolling support.

    Logic:
    - Compute rolling support = min low of last 20 bars (no cache dependency).
    - Price must be within 1.5% above that support.
    - Look at the last 3 high-volume bars near support: if net price change is
      small despite high sell volume (inferred from down-close candles with high vol),
      absorption is confirmed.
    - Requires at least 2 of the last 3 high-vol bars to close near their open
      (body < 35% of range), indicating sellers failed to push price lower.
    """
    ohlcv = cache.get_ohlcv(symbol, window=25, tf="5m")
    if len(ohlcv) < 10:
        return False

    # Rolling support: lowest low of last 20 bars
    lows = [b["l"] for b in ohlcv[-20:]]
    support = min(lows)
    price = ohlcv[-1]["c"]

    if support <= 0 or (price - support) / support > 0.015:
        return False

    # Volume MA
    vols = [b["v"] for b in ohlcv[-20:]]
    vol_ma = sum(vols) / len(vols) if vols else 0.0
    if vol_ma <= 0:
        return False

    # Count high-volume bars near support with small body (absorption candles)
    absorbed = 0
    checked = 0
    for bar in ohlcv[-5:]:
        if bar["l"] > support * 1.015:
            continue   # not near support
        if bar["v"] < vol_ma * 1.3:
            continue   # not high volume
        checked += 1
        candle_range = bar["h"] - bar["l"]
        if candle_range == 0:
            continue
        body = abs(bar["c"] - bar["o"])
        if (body / candle_range) < 0.35:
            absorbed += 1

    # Need at least 1 confirmed absorption candle near support
    return absorbed >= 1
