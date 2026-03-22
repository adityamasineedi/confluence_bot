"""Liquidity sweep signal — detects buy-side liq grabs followed by reversal up."""

_MIN_CLUSTER_USD = 5_000_000
_PROXIMITY_PCT   = 0.003   # price within 0.3 % of cluster level
_MIN_BULL_CLOSES = 2       # at least 2 bullish closes in last 3 candles


def check_liq_sweep(symbol: str, cache) -> bool:
    """True when price sweeps a large liquidity cluster then closes back above it.

    Algorithm:
    1. For each cluster with size_usd > $5 M:
       - proximity  : abs(price − level) / price < 0.003
       - swept       : any of the last 3 candles has low ≤ level ≤ high
       - bounce      : last candle close > level
       - bull_closes : ≥ 2 of last 3 candles are bullish (close > open)
    2. Return True if ALL four conditions pass for any single cluster.
    """
    clusters = cache.get_liq_clusters(symbol)
    if not clusters:
        return False

    candles = cache.get_ohlcv(symbol, window=5, tf="15m")
    if len(candles) < 3:
        return False

    recent   = candles[-3:]
    price    = candles[-1]["c"]
    bull_closes = sum(1 for c in recent if c["c"] > c["o"])

    for cluster in clusters:
        level    = cluster.get("price") or cluster.get("level", 0)
        size_usd = cluster.get("size_usd", 0)

        if size_usd < _MIN_CLUSTER_USD:
            continue

        proximity = abs(price - level) / price < _PROXIMITY_PCT
        swept     = any(c["l"] <= level <= c["h"] for c in recent)
        bounce    = candles[-1]["c"] > level

        if proximity and swept and bounce and bull_closes >= _MIN_BULL_CLOSES:
            return True

    return False
