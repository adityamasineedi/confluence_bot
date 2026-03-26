"""EMA 20/50 pullback entry signals — proven trend-following entry timing.

Strategy logic:
  Long:  20 EMA > 50 EMA (uptrend confirmed on 1H) AND price has pulled back
         to within 1% of the 20 EMA AND current candle is bullish.
         → Buying the dip in an uptrend at the dynamic support line.

  Short: 20 EMA < 50 EMA (downtrend confirmed on 1H) AND price has bounced
         up to within 1% of the 20 EMA AND current candle is bearish.
         → Selling the rally in a downtrend at the dynamic resistance line.

Why this works: in a trending market, price oscillates around the fast EMA.
Entries at the EMA provide a natural stop (below EMA for longs) and mean-
reversion back toward trend continuation provides the TP.
"""

_EMA_FAST    = 20
_EMA_SLOW    = 50
_TOUCH_PCT   = 0.020  # price must be within 2.0% of the 20 EMA to count as a touch


def _ema(closes: list[float], period: int) -> float:
    """Exponential moving average of the closes series.  Returns 0.0 on insufficient data."""
    if len(closes) < period:
        return 0.0
    k   = 2.0 / (period + 1)
    ema = sum(closes[:period]) / period
    for p in closes[period:]:
        ema = p * k + ema * (1 - k)
    return ema


def check_ema_pullback_long(symbol: str, cache) -> bool:
    """True when 1H 20 EMA > 50 EMA and price has pulled back to the 20 EMA.

    Conditions (all must hold):
    1. 20 EMA above 50 EMA  — confirmed 1H uptrend
    2. Price ≤ ema20 × (1 + _TOUCH_PCT) — price at or below the fast EMA
    3. Price ≥ ema50 — did not break below the slow EMA (trend intact)
    4. Current 1H candle is bullish (close > open) — demand present at EMA
    """
    closes = cache.get_closes(symbol, window=_EMA_SLOW + 10, tf="1h")
    if len(closes) < _EMA_SLOW + 2:
        return False

    ema20 = _ema(closes, _EMA_FAST)
    ema50 = _ema(closes, _EMA_SLOW)
    if ema20 <= ema50 or ema20 == 0.0:
        return False

    price = closes[-1]

    # Price must be near or touching 20 EMA from below/at
    if price > ema20 * (1 + _TOUCH_PCT):
        return False   # price is too far above EMA — not a pullback entry

    # Price must remain within 3% of 50 EMA (allow slight dips below in strong trends)
    if price < ema50 * 0.97:
        return False

    # Bullish confirmation candle
    ohlcv = cache.get_ohlcv(symbol, window=1, tf="1h")
    if ohlcv and ohlcv[-1]["c"] <= ohlcv[-1]["o"]:
        return False

    return True


def check_ema_pullback_short(symbol: str, cache) -> bool:
    """True when 1H 20 EMA < 50 EMA and price has rallied back to the 20 EMA.

    Conditions (all must hold):
    1. 20 EMA below 50 EMA  — confirmed 1H downtrend
    2. Price ≥ ema20 × (1 - _TOUCH_PCT) — price at or above the fast EMA
    3. Price ≤ ema50 — did not break above slow EMA (trend intact)
    4. Current 1H candle is bearish (close < open) — supply present at EMA
    """
    closes = cache.get_closes(symbol, window=_EMA_SLOW + 10, tf="1h")
    if len(closes) < _EMA_SLOW + 2:
        return False

    ema20 = _ema(closes, _EMA_FAST)
    ema50 = _ema(closes, _EMA_SLOW)
    if ema20 >= ema50 or ema20 == 0.0:
        return False

    price = closes[-1]

    # Price must be near or touching 20 EMA from above/at
    if price < ema20 * (1 - _TOUCH_PCT):
        return False   # price is too far below EMA — not a rally-to-resistance entry

    # Price must remain below 50 EMA (trend not broken)
    if price > ema50:
        return False

    # Bearish confirmation candle
    ohlcv = cache.get_ohlcv(symbol, window=1, tf="1h")
    if ohlcv and ohlcv[-1]["c"] >= ohlcv[-1]["o"]:
        return False

    return True
