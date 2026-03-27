"""HTF Demand / Supply Zone signals — 4H origin-of-move zone reactions.

Strategy logic
--------------
Demand zones are rectangular areas on the 4H chart where price previously
consolidated (base) before an impulsive bullish move. When price returns to
this zone for the first time, we expect buyers to defend it again.

Supply zones are the mirror: consolidation before an impulsive drop.

Zone detection algorithm:
  1. Scan last 100 × 4H bars for "base" candles: a run of ≥ 2 bars where
     each bar's range (high-low) / close ≤ BASE_RANGE_PCT (tight consolidation).
  2. The base must be followed within the next 3 bars by an impulsive move
     of ≥ IMPULSE_PCT away from the base midpoint.
  3. Zone boundaries: base_low (demand) or base_high (supply).
  4. "First retest": price has not closed inside the zone since the origin move.
  5. Current price is inside the zone (±ZONE_BUFFER_PCT).

Entry confirmation (separate function):
  - 1H bullish close inside demand zone (last 1H bar)
  - Not a deep close below zone bottom (would invalidate the zone)
"""

_LOOKBACK_4H      = 100     # bars of 4H history to scan for zones
_BASE_MIN_BARS    = 2       # minimum consolidation bars to form a zone
_BASE_RANGE_PCT   = 0.008   # max (high-low)/close per bar = 0.8% to qualify as base
_IMPULSE_PCT      = 0.030   # impulsive move must be ≥ 3% from base mid (raised from 2%)
_IMPULSE_BARS     = 3       # move must occur within this many bars after base
_ZONE_BUFFER_PCT  = 0.005   # price must be within 0.5% of zone boundary to trigger
_ZONE_WIDTH_MAX   = 0.015   # zone (high-low)/mid must be ≤ 1.5% — tight zones only
_MIN_ZONE_AGE     = 3       # zone must be at least 3 bars old (not brand new)
_MAX_ZONE_AGE     = 80      # zone older than 80 bars is stale


def _find_demand_zones(bars: list[dict]) -> list[dict]:
    """Return list of demand zones: {low, high, mid, age_bars, origin_idx}."""
    zones = []
    n = len(bars)

    i = 0
    while i < n - _BASE_MIN_BARS - _IMPULSE_BARS:
        # Try to start a base at bar i
        base_start = i
        base_end   = i

        # Extend base while bars remain tight
        while base_end < n - 1:
            bar  = bars[base_end]
            rng  = (bar["h"] - bar["l"]) / bar["c"] if bar["c"] > 0 else 1.0
            if rng > _BASE_RANGE_PCT:
                break
            base_end += 1

        base_len = base_end - base_start
        if base_len < _BASE_MIN_BARS:
            i += 1
            continue

        # Compute base levels
        base_low  = min(bars[j]["l"] for j in range(base_start, base_end))
        base_high = max(bars[j]["h"] for j in range(base_start, base_end))
        base_mid  = (base_low + base_high) / 2

        # Reject wide zones — only tight price clusters qualify
        if base_mid > 0 and (base_high - base_low) / base_mid > _ZONE_WIDTH_MAX:
            i = base_end + 1
            continue

        # Check for impulsive BULLISH move after the base
        for k in range(base_end, min(base_end + _IMPULSE_BARS, n)):
            impulse_high = bars[k]["h"]
            if (impulse_high - base_mid) / base_mid >= _IMPULSE_PCT:
                age = n - 1 - k   # bars since zone origin
                if _MIN_ZONE_AGE <= age <= _MAX_ZONE_AGE:
                    post_bars   = bars[k + 1:]
                    prior_bars  = post_bars[:-1]   # all bars between origin and current
                    # Zone is invalid if:
                    # (a) price previously broke BELOW base_low (zone destroyed), OR
                    # (b) any prior bar's wick already entered the zone from above
                    #     (would be a second retest, not first)
                    destroyed   = any(b["c"] < base_low for b in post_bars)
                    prev_touch  = any(b["l"] <= base_high for b in prior_bars)
                    if not destroyed and not prev_touch:
                        zones.append({
                            "low":        base_low,
                            "high":       base_high,
                            "mid":        base_mid,
                            "age_bars":   age,
                            "origin_idx": k,
                        })
                break

        i = base_end + 1

    return zones


def _find_supply_zones(bars: list[dict]) -> list[dict]:
    """Return list of supply zones: {low, high, mid, age_bars}."""
    zones = []
    n = len(bars)

    i = 0
    while i < n - _BASE_MIN_BARS - _IMPULSE_BARS:
        base_start = i
        base_end   = i

        while base_end < n - 1:
            bar  = bars[base_end]
            rng  = (bar["h"] - bar["l"]) / bar["c"] if bar["c"] > 0 else 1.0
            if rng > _BASE_RANGE_PCT:
                break
            base_end += 1

        base_len = base_end - base_start
        if base_len < _BASE_MIN_BARS:
            i += 1
            continue

        base_low  = min(bars[j]["l"] for j in range(base_start, base_end))
        base_high = max(bars[j]["h"] for j in range(base_start, base_end))
        base_mid  = (base_low + base_high) / 2

        # Reject wide zones — only tight price clusters qualify
        if base_mid > 0 and (base_high - base_low) / base_mid > _ZONE_WIDTH_MAX:
            i = base_end + 1
            continue

        # Check for impulsive BEARISH move after the base
        for k in range(base_end, min(base_end + _IMPULSE_BARS, n)):
            impulse_low = bars[k]["l"]
            if (base_mid - impulse_low) / base_mid >= _IMPULSE_PCT:
                age = n - 1 - k
                if _MIN_ZONE_AGE <= age <= _MAX_ZONE_AGE:
                    post_bars  = bars[k + 1:]
                    prior_bars = post_bars[:-1]
                    # Zone invalid if price previously broke ABOVE base_high (destroyed)
                    # or any prior bar's wick already entered the zone (second retest)
                    destroyed  = any(b["c"] > base_high for b in post_bars)
                    prev_touch = any(b["h"] >= base_low for b in prior_bars)
                    if not destroyed and not prev_touch:
                        zones.append({
                            "low":        base_low,
                            "high":       base_high,
                            "mid":        base_mid,
                            "age_bars":   age,
                            "origin_idx": k,
                        })
                break

        i = base_end + 1

    return zones


def check_demand_zone_long(symbol: str, cache) -> bool:
    """True when current price is retesting a 4H demand zone with 1H bullish close.

    Conditions:
    1. Active demand zone found on 4H (consolidation before bullish impulse).
    2. Current price is within the zone (low to high + buffer).
    3. Price has NOT previously closed below the zone (first retest).
    4. 1H candle close is bullish (close > open) inside or just above zone.
    """
    bars_4h = cache.get_ohlcv(symbol, window=_LOOKBACK_4H, tf="4h")
    if len(bars_4h) < _LOOKBACK_4H // 2:
        return False

    zones = _find_demand_zones(bars_4h)
    if not zones:
        return False

    price = bars_4h[-1]["c"]

    for zone in zones:
        # Price must be inside or just above zone
        in_zone = zone["low"] * (1 - _ZONE_BUFFER_PCT) <= price <= zone["high"] * (1 + _ZONE_BUFFER_PCT)
        if not in_zone:
            continue

        # 1H confirmation: bullish close
        bars_1h = cache.get_ohlcv(symbol, window=3, tf="1h")
        if not bars_1h:
            continue
        last_1h = bars_1h[-1]
        if last_1h["c"] <= last_1h["o"]:
            continue   # 1H candle not bullish

        # 1H close must be within zone (not far above)
        if last_1h["c"] > zone["high"] * (1 + _ZONE_BUFFER_PCT):
            continue   # closed too far above zone — missed the entry

        return True

    return False


def check_supply_zone_short(symbol: str, cache) -> bool:
    """True when current price is retesting a 4H supply zone with 1H bearish close.

    Conditions:
    1. Active supply zone found on 4H (consolidation before bearish impulse).
    2. Current price is within the zone.
    3. Price has NOT previously closed above the zone (first retest).
    4. 1H candle close is bearish (close < open) inside or just below zone.
    """
    bars_4h = cache.get_ohlcv(symbol, window=_LOOKBACK_4H, tf="4h")
    if len(bars_4h) < _LOOKBACK_4H // 2:
        return False

    zones = _find_supply_zones(bars_4h)
    if not zones:
        return False

    price = bars_4h[-1]["c"]

    for zone in zones:
        in_zone = zone["low"] * (1 - _ZONE_BUFFER_PCT) <= price <= zone["high"] * (1 + _ZONE_BUFFER_PCT)
        if not in_zone:
            continue

        bars_1h = cache.get_ohlcv(symbol, window=3, tf="1h")
        if not bars_1h:
            continue
        last_1h = bars_1h[-1]
        if last_1h["c"] >= last_1h["o"]:
            continue   # 1H candle not bearish

        if last_1h["c"] < zone["low"] * (1 - _ZONE_BUFFER_PCT):
            continue   # closed too far below zone

        return True

    return False


def get_demand_zone_levels(symbol: str, cache) -> tuple[float, float, float]:
    """Return (entry, stop, tp) for demand zone long. SL below zone, TP = 1.5× risk."""
    bars_4h = cache.get_ohlcv(symbol, window=_LOOKBACK_4H, tf="4h")
    if not bars_4h:
        return 0.0, 0.0, 0.0

    zones = _find_demand_zones(bars_4h)
    if not zones:
        return 0.0, 0.0, 0.0

    price = bars_4h[-1]["c"]
    # Use closest zone
    active = [z for z in zones
              if z["low"] * (1 - _ZONE_BUFFER_PCT) <= price <= z["high"] * (1 + _ZONE_BUFFER_PCT)]
    if not active:
        return 0.0, 0.0, 0.0

    zone  = active[0]
    entry = price
    stop  = zone["low"] * (1 - 0.002)   # 0.2% below zone bottom
    dist  = entry - stop
    if dist <= 0:
        return 0.0, 0.0, 0.0
    tp = entry + dist * 1.5
    return entry, stop, tp


def get_supply_zone_levels(symbol: str, cache) -> tuple[float, float, float]:
    """Return (entry, stop, tp) for supply zone short."""
    bars_4h = cache.get_ohlcv(symbol, window=_LOOKBACK_4H, tf="4h")
    if not bars_4h:
        return 0.0, 0.0, 0.0

    zones = _find_supply_zones(bars_4h)
    if not zones:
        return 0.0, 0.0, 0.0

    price  = bars_4h[-1]["c"]
    active = [z for z in zones
              if z["low"] * (1 - _ZONE_BUFFER_PCT) <= price <= z["high"] * (1 + _ZONE_BUFFER_PCT)]
    if not active:
        return 0.0, 0.0, 0.0

    zone  = active[0]
    entry = price
    stop  = zone["high"] * (1 + 0.002)
    dist  = stop - entry
    if dist <= 0:
        return 0.0, 0.0, 0.0
    tp = entry - dist * 1.5
    return entry, stop, tp
