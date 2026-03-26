"""1H inside bar compression zone detector.

An "inside bar" is a 1H candle fully contained within the prior candle's
high-low range.  Two or more consecutive inside bars signal compression
(market indecision) — price is coiling before a release.

We trade the mean-reversion flip *within* the compression zone:
- Touch the compression low  → LONG  (bounce off floor)
- Touch the compression high → SHORT (rejection from ceiling)

POC (point of control) is computed as the midpoint of the highest-volume
inside bar — this is the attractor price inside the zone, used as an
intermediate TP target check.

All functions are pure (no I/O, no cache).

Definitions
-----------
- "mother bar": the first candle that contains the inside bars
- "inside sequence": N >= 2 consecutive inside bars, each inside the one before it
- "compression zone": [min(low) of sequence, max(high) of sequence]
"""


def _is_inside(bar: dict, ref: dict) -> bool:
    """True if `bar` is fully inside `ref` (lower high AND higher low)."""
    return bar["h"] <= ref["h"] and bar["l"] >= ref["l"]


def detect_compression(
    bars:         list[dict],
    min_inside:   int = 2,
) -> dict | None:
    """Find the most recent inside bar compression zone.

    Scans from the most recent bar backwards to find the longest run of
    consecutive inside bars (each inside the previous bar).  Returns the
    zone geometry, or None if no run of length >= min_inside found.

    Uses bars[:-1] (all completed bars) — no look-ahead.

    Parameters
    ----------
    bars        : 1H OHLCV bar dicts, chronological order, most recent last
    min_inside  : minimum number of consecutive inside bars required

    Returns
    -------
    dict with keys:
        zone_high    : max high of the inside sequence
        zone_low     : min low of the inside sequence
        zone_width   : zone_high - zone_low
        zone_mid     : midpoint
        zone_pct     : width / zone_mid (compression tightness)
        bar_count    : number of inside bars in sequence
        poc          : midpoint of highest-volume inside bar (volume proxy POC)
    None if no qualifying sequence found
    """
    # Work on completed bars only (exclude current open bar)
    completed = bars[:-1] if len(bars) > 1 else bars
    if len(completed) < min_inside + 1:
        return None

    # Walk backwards: find longest inside run ending at the most recent bar
    # "Inside" means: each bar is inside the one before it
    run = [completed[-1]]
    for i in range(len(completed) - 2, -1, -1):
        if _is_inside(completed[i + 1], completed[i]):
            run.insert(0, completed[i + 1])
        else:
            break

    if len(run) < min_inside:
        return None

    zone_high = max(b["h"] for b in run)
    zone_low  = min(b["l"] for b in run)
    zone_mid  = (zone_high + zone_low) / 2.0

    if zone_low <= 0.0:
        return None

    # Volume POC: midpoint of the bar with highest volume
    poc_bar  = max(run, key=lambda b: b.get("v", 0.0))
    poc      = (poc_bar["h"] + poc_bar["l"]) / 2.0

    return {
        "zone_high":  zone_high,
        "zone_low":   zone_low,
        "zone_width": zone_high - zone_low,
        "zone_mid":   zone_mid,
        "zone_pct":   (zone_high - zone_low) / zone_mid,
        "bar_count":  len(run),
        "poc":        poc,
    }


def near_zone_low(price: float, zone_low: float, entry_zone_pct: float) -> bool:
    """True when price is within entry_zone_pct above zone_low."""
    if zone_low <= 0.0:
        return False
    proximity = (price - zone_low) / zone_low
    return 0.0 <= proximity <= entry_zone_pct


def near_zone_high(price: float, zone_high: float, entry_zone_pct: float) -> bool:
    """True when price is within entry_zone_pct below zone_high."""
    if zone_high <= 0.0:
        return False
    proximity = (zone_high - price) / zone_high
    return 0.0 <= proximity <= entry_zone_pct


def compute_levels(
    direction:     str,
    zone_low:      float,
    zone_high:     float,
    sl_buffer_pct: float,
    rr_ratio:      float,
    entry:         float,
) -> tuple[float, float]:
    """Return (stop_loss, take_profit).

    SL: just outside the zone boundary (buffer beyond the edge).
    TP: entry ± (sl_dist × rr_ratio)  — typically 1.5 RR.
    """
    if direction == "LONG":
        sl      = zone_low  * (1.0 - sl_buffer_pct)
        sl_dist = abs(entry - sl)
        tp      = entry + sl_dist * rr_ratio
    else:
        sl      = zone_high * (1.0 + sl_buffer_pct)
        sl_dist = abs(sl - entry)
        tp      = entry - sl_dist * rr_ratio

    return round(sl, 8), round(tp, 8)
