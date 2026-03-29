"""Volume momentum helpers — reusable across scorers.

All functions take a list of OHLCV bar dicts (keys: o, h, l, c, v, ts)
and return bool.  Handle None / empty lists gracefully.

Functions
---------
volume_spike           — current bar volume ≥ mult × rolling average
volume_quiet           — current bar volume ≤ mult × rolling average
volume_divergence_bearish — price rising but volume declining (distribution)
volume_divergence_bullish — price falling but volume declining (accumulation)
increasing_volume      — volume trend is rising (momentum building)
relative_volume        — RVOL ratio float (current / average)
"""
from __future__ import annotations


def relative_volume(bars: list[dict], lookback: int = 20) -> float:
    """Return current bar volume / average volume over lookback bars.

    Returns 1.0 (neutral) when data is insufficient or average is zero.
    """
    if not bars or len(bars) < 2:
        return 1.0
    vol_slice = bars[-(lookback + 1):-1]   # exclude current bar from average
    if not vol_slice:
        return 1.0
    avg = sum(b["v"] for b in vol_slice) / len(vol_slice)
    if avg <= 0.0:
        return 1.0
    return bars[-1]["v"] / avg


def volume_spike(bars: list[dict], lookback: int = 20, mult: float = 1.5) -> bool:
    """True when the current bar's volume is ≥ mult × the rolling average.

    Parameters
    ----------
    bars     : list of OHLCV bar dicts, most-recent last
    lookback : number of prior bars used to compute the average
    mult     : minimum multiple above average to qualify as a spike
    """
    if not bars or len(bars) < 2:
        return False
    return relative_volume(bars, lookback) >= mult


def volume_quiet(bars: list[dict], lookback: int = 20, mult: float = 1.3) -> bool:
    """True when the current bar's volume is ≤ mult × the rolling average.

    Quiet volume confirms consolidation — good for range / fade entries.

    Parameters
    ----------
    bars     : list of OHLCV bar dicts, most-recent last
    lookback : number of prior bars used to compute the average
    mult     : maximum multiple to qualify as "quiet"
    """
    if not bars or len(bars) < 2:
        return True   # assume quiet when no data
    return relative_volume(bars, lookback) <= mult


def volume_divergence_bearish(bars: list[dict], lookback: int = 5) -> bool:
    """True when price is rising but volume is declining over *lookback* bars.

    Rising price + falling volume = distribution (bearish divergence).
    Returns False (safe default) when data is insufficient.

    Parameters
    ----------
    bars     : list of OHLCV bar dicts, most-recent last
    lookback : window to measure slope of price and volume
    """
    if not bars or len(bars) < lookback + 1:
        return False
    window = bars[-(lookback + 1):]
    first_close = window[0]["c"]
    last_close  = window[-1]["c"]
    first_vol   = window[0]["v"]
    last_vol    = window[-1]["v"]
    if first_close <= 0.0 or first_vol <= 0.0:
        return False
    price_rising  = last_close > first_close
    volume_falling = last_vol < first_vol
    return price_rising and volume_falling


def volume_divergence_bullish(bars: list[dict], lookback: int = 5) -> bool:
    """True when price is falling but volume is declining over *lookback* bars.

    Falling price + falling volume = accumulation (bullish divergence).
    Returns False (safe default) when data is insufficient.

    Parameters
    ----------
    bars     : list of OHLCV bar dicts, most-recent last
    lookback : window to measure slope of price and volume
    """
    if not bars or len(bars) < lookback + 1:
        return False
    window = bars[-(lookback + 1):]
    first_close = window[0]["c"]
    last_close  = window[-1]["c"]
    first_vol   = window[0]["v"]
    last_vol    = window[-1]["v"]
    if first_close <= 0.0 or first_vol <= 0.0:
        return False
    price_falling  = last_close < first_close
    volume_falling = last_vol < first_vol
    return price_falling and volume_falling


def increasing_volume(bars: list[dict], lookback: int = 5, min_ratio: float = 1.1) -> bool:
    """True when volume is trending upward over *lookback* bars.

    Uses a simple first-vs-last comparison: last bar volume must be at least
    min_ratio × first bar volume.  Returns False when data is insufficient.

    Parameters
    ----------
    bars      : list of OHLCV bar dicts, most-recent last
    lookback  : window to measure volume trend
    min_ratio : last / first volume must exceed this ratio to qualify
    """
    if not bars or len(bars) < lookback + 1:
        return False
    window = bars[-(lookback + 1):]
    first_vol = window[0]["v"]
    last_vol  = window[-1]["v"]
    if first_vol <= 0.0:
        return False
    return (last_vol / first_vol) >= min_ratio
