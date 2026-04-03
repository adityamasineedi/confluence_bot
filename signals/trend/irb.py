"""Rob Hoffman Inventory Retracement Bar (IRB) signal.

An IRB marks a bar where institutional players absorbed a short-term
retracement and closed with conviction in the direction of the underlying
trend. Two conditions must hold simultaneously:

  1. The prior two bars retraced against the trend (2-bar pullback).
  2. The IRB bar itself closes in the top 25% of its range (LONG) or the
     bottom 25% (SHORT), indicating price was rejected and absorbed.

The bar must also close in the direction of the trade (green for LONG,
red for SHORT) to confirm conviction.  A doji (o == c) does not qualify.

Timeframe: 15m bars — gives enough granularity to catch institutional
absorption before a momentum leg, without the noise of 1m/5m bars.
"""


def check_irb_long(symbol: str, cache) -> bool:
    """Bullish IRB: 2-bar pullback then close in top 25% of bar range.

    Looks at bars[-3] and bars[-2] for the retracement (both down bars),
    and bars[-1] as the IRB candidate.
    """
    bars = cache.get_ohlcv(symbol, window=6, tf="15m")
    if not bars or len(bars) < 4:
        return False

    bar_2 = bars[-3]   # 2 bars ago
    bar_1 = bars[-2]   # 1 bar ago

    if not (bar_2["c"] < bar_2["o"] and bar_1["c"] < bar_1["o"]):
        return False

    irb_bar   = bars[-1]
    bar_range = irb_bar["h"] - irb_bar["l"]
    if bar_range == 0:
        return False

    close_position = (irb_bar["c"] - irb_bar["l"]) / bar_range
    if close_position < 0.75:
        return False

    return irb_bar["c"] > irb_bar["o"]


def check_irb_short(symbol: str, cache) -> bool:
    """Bearish IRB: 2-bar rally then close in bottom 25% of bar range.

    Looks at bars[-3] and bars[-2] for the retracement (both up bars),
    and bars[-1] as the IRB candidate.
    """
    bars = cache.get_ohlcv(symbol, window=6, tf="15m")
    if not bars or len(bars) < 4:
        return False

    bar_2 = bars[-3]
    bar_1 = bars[-2]

    if not (bar_2["c"] > bar_2["o"] and bar_1["c"] > bar_1["o"]):
        return False

    irb_bar   = bars[-1]
    bar_range = irb_bar["h"] - irb_bar["l"]
    if bar_range == 0:
        return False

    close_position = (irb_bar["c"] - irb_bar["l"]) / bar_range
    if close_position > 0.25:
        return False

    return irb_bar["c"] < irb_bar["o"]
