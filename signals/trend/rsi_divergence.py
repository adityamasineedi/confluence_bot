"""RSI divergence signals — one of the highest-edge reversal signals in crypto.

Bullish divergence:  price makes a lower low but RSI makes a higher low.
  → Sellers are losing momentum despite new price lows.  Reversal likely.

Bearish divergence:  price makes a higher high but RSI makes a lower high.
  → Buyers are losing momentum despite new price highs.  Reversal likely.

Both use 1H candles.  Swing points require 2-bar confirmation on each side.
"""

_RSI_PERIOD          = 14
_SWING_LOOKBACK      = 2   # bars each side required to confirm a swing point
_MIN_SWING_SEPARATION = 4  # two swings must be at least 4 bars apart
_MAX_SWING_AGE        = 35  # only consider swings within last 35 bars


# ── RSI series ────────────────────────────────────────────────────────────────

def _rsi_series(closes: list[float], period: int = _RSI_PERIOD) -> list[float]:
    """Wilder RSI series.

    Returns a list of length (len(closes) - period).
    rsi_series[k]  corresponds to  closes[period + k].
    rsi_series[-1] is the current RSI.
    Returns [] when insufficient data.
    """
    if len(closes) < period + 1:
        return []

    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))

    avg_g = sum(gains[:period]) / period
    avg_l = sum(losses[:period]) / period
    rsi = [100.0 if avg_l == 0 else 100.0 - 100.0 / (1.0 + avg_g / avg_l)]

    for i in range(period, len(gains)):
        avg_g = (avg_g * (period - 1) + gains[i]) / period
        avg_l = (avg_l * (period - 1) + losses[i]) / period
        rsi.append(100.0 if avg_l == 0 else 100.0 - 100.0 / (1.0 + avg_g / avg_l))

    return rsi


# ── Swing detection ───────────────────────────────────────────────────────────

def _swing_low_indices(lows: list[float], lb: int = _SWING_LOOKBACK) -> list[int]:
    """Return indices where lows[i] is the minimum of the ±lb window."""
    out = []
    for i in range(lb, len(lows) - lb):
        window = lows[i - lb : i + lb + 1]
        if lows[i] == min(window):
            out.append(i)
    return out


def _swing_high_indices(highs: list[float], lb: int = _SWING_LOOKBACK) -> list[int]:
    """Return indices where highs[i] is the maximum of the ±lb window."""
    out = []
    for i in range(lb, len(highs) - lb):
        window = highs[i - lb : i + lb + 1]
        if highs[i] == max(window):
            out.append(i)
    return out


# ── Public signals ────────────────────────────────────────────────────────────

def check_rsi_divergence_bullish(symbol: str, cache) -> bool:
    """True when price makes a lower low but RSI makes a higher low on 1H.

    Classic bullish divergence — sellers are exhausting, reversal likely.
    Requires two confirmed swing lows separated by ≥4 bars.
    """
    ohlcv = cache.get_ohlcv(symbol, window=_MAX_SWING_AGE + 5, tf="1h")
    if len(ohlcv) < 20:
        return False

    closes = [c["c"] for c in ohlcv]
    lows   = [c["l"] for c in ohlcv]

    rsi = _rsi_series(closes)
    if not rsi:
        return False

    # rsi[k] corresponds to ohlcv index (len(ohlcv) - len(rsi) + k)
    rsi_offset = len(ohlcv) - len(rsi)

    swing_idxs = _swing_low_indices(lows)
    # Keep only swings that have RSI data and are within the lookback window
    recent = [i for i in swing_idxs if i >= rsi_offset and i < len(ohlcv) - _SWING_LOOKBACK]
    if len(recent) < 2:
        return False

    sw1, sw2 = recent[-2], recent[-1]
    if sw2 - sw1 < _MIN_SWING_SEPARATION:
        return False

    price_lower_low = lows[sw2] < lows[sw1]
    rsi_higher_low  = rsi[sw2 - rsi_offset] > rsi[sw1 - rsi_offset]

    return price_lower_low and rsi_higher_low


def check_rsi_divergence_bearish(symbol: str, cache) -> bool:
    """True when price makes a higher high but RSI makes a lower high on 1H.

    Classic bearish divergence — buyers are exhausting, reversal likely.
    Requires two confirmed swing highs separated by ≥4 bars.
    """
    ohlcv = cache.get_ohlcv(symbol, window=_MAX_SWING_AGE + 5, tf="1h")
    if len(ohlcv) < 20:
        return False

    closes = [c["c"] for c in ohlcv]
    highs  = [c["h"] for c in ohlcv]

    rsi = _rsi_series(closes)
    if not rsi:
        return False

    rsi_offset = len(ohlcv) - len(rsi)

    swing_idxs = _swing_high_indices(highs)
    recent = [i for i in swing_idxs if i >= rsi_offset and i < len(ohlcv) - _SWING_LOOKBACK]
    if len(recent) < 2:
        return False

    sh1, sh2 = recent[-2], recent[-1]
    if sh2 - sh1 < _MIN_SWING_SEPARATION:
        return False

    price_higher_high = highs[sh2] > highs[sh1]
    rsi_lower_high    = rsi[sh2 - rsi_offset] < rsi[sh1 - rsi_offset]

    return price_higher_high and rsi_lower_high
