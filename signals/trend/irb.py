"""
signals/trend/irb.py
Rob Hoffman Inventory Retracement Bar (IRB).

A bullish IRB requires:
  1. Two consecutive down bars (retracement into trend)
  2. Current bar closes in the TOP 25% of its own high-low range
  3. Current bar is green (close > open)

A bearish IRB requires:
  1. Two consecutive up bars (retracement into downtrend)
  2. Current bar closes in the BOTTOM 25% of its own range
  3. Current bar is red (close < open)

Best used at key levels (PDH, PDL, POC, FVG zone).
Uses 15m bars — matches EMA pullback and FVG entry timeframes.
"""


def check_irb_long(symbol: str, cache) -> bool:
    """Bullish IRB: 2-bar pullback then close in top 25% of bar range.

    Returns False on insufficient data — never raises.
    """
    bars = cache.get_ohlcv(symbol, window=5, tf="15m")
    if not bars or len(bars) < 4:
        return False

    # Two consecutive down bars before the IRB bar
    bar_2 = bars[-3]   # 2 bars before current
    bar_1 = bars[-2]   # 1 bar before current
    retracement = (bar_2["c"] < bar_2["o"]) and (bar_1["c"] < bar_1["o"])
    if not retracement:
        return False

    # IRB bar: closes in top 25% of its range AND is green
    irb_bar   = bars[-1]
    bar_range = irb_bar["h"] - irb_bar["l"]
    if bar_range <= 0:
        return False

    close_position = (irb_bar["c"] - irb_bar["l"]) / bar_range
    return close_position >= 0.75 and irb_bar["c"] > irb_bar["o"]


def check_irb_short(symbol: str, cache) -> bool:
    """Bearish IRB: 2-bar rally then close in bottom 25% of bar range.

    Returns False on insufficient data — never raises.
    """
    bars = cache.get_ohlcv(symbol, window=5, tf="15m")
    if not bars or len(bars) < 4:
        return False

    # Two consecutive up bars before the IRB bar
    bar_2 = bars[-3]
    bar_1 = bars[-2]
    retracement = (bar_2["c"] > bar_2["o"]) and (bar_1["c"] > bar_1["o"])
    if not retracement:
        return False

    # IRB bar: closes in bottom 25% of its range AND is red
    irb_bar   = bars[-1]
    bar_range = irb_bar["h"] - irb_bar["l"]
    if bar_range <= 0:
        return False

    close_position = (irb_bar["c"] - irb_bar["l"]) / bar_range
    return close_position <= 0.25 and irb_bar["c"] < irb_bar["o"]
