"""Hard filters for RANGE trades — must all pass before a range signal fires."""
import os
import yaml

_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "config.yaml")
with open(_CONFIG_PATH) as _f:
    _cfg = yaml.safe_load(_f)

_MIN_RANGE_CANDLES = _cfg["regime"]["min_range_candles"]   # 20
_MIN_24H_VOL       = _cfg["filters"]["min_24h_volume_usdt"]


def passes_range_filters(symbol: str, cache) -> bool:
    """Return True only when all RANGE hard filters are satisfied.

    Gates:
    1. Price still within cached range bounds (not broken out)
    2. Range has been intact for at least min_range_candles 4H bars
    3. 24H volume above minimum liquidity threshold
    """
    range_high = cache.get_range_high(symbol)
    range_low  = cache.get_range_low(symbol)

    if range_high is None or range_low is None:
        return False

    # Gate 1: price within bounds
    closes_1m = cache.get_closes(symbol, window=1, tf="1m")
    if not closes_1m:
        return False
    price = closes_1m[-1]
    if not (range_low <= price <= range_high):
        return False

    # Gate 2: range age (range_start_ts set by regime detector)
    start_ts = cache.get_range_start_timestamp(symbol)
    if start_ts is not None:
        candles_4h = cache.get_ohlcv(symbol, window=_MIN_RANGE_CANDLES + 5, tf="4h")
        # Count how many 4H candles have ts >= start_ts
        candles_in_range = sum(1 for c in candles_4h if c["ts"] >= start_ts)
        if candles_in_range < _MIN_RANGE_CANDLES:
            return False

    # Gate 3: liquidity
    vol_24h = cache.get_vol_24h(symbol)
    if vol_24h is not None and vol_24h < _MIN_24H_VOL:
        return False

    return True
