"""Bollinger Band Squeeze — volatility compression before explosive breakout moves.

A squeeze occurs when Bollinger Bands (SMA20 ± 2σ) contract *inside* Keltner
Channels (EMA20 ± 1.5×ATR14).  The bar where BBands expand back outside Keltner
is the squeeze release — the start of the next directional impulse.

Strategy:
  - Squeeze must hold for ≥ 3 bars (filters noise)
  - Release bar: BB upper > KC upper (bands expanding)
  - Bullish release: close > EMA20 at moment of release
  - Bearish release: close < EMA20 at moment of release

Used in BREAKOUT scorer (primary) and TREND scorer (confluence confirmation).
Timeframe: 1H — balances frequency with signal quality.
"""

_BB_PERIOD        = 20    # SMA period for Bollinger midline
_BB_MULT          = 2.0   # standard deviation multiplier
_KC_PERIOD        = 20    # EMA period for Keltner midline
_KC_ATR_MULT      = 1.5   # ATR multiplier for Keltner width
_ATR_PERIOD       = 14
_MIN_SQUEEZE_BARS = 3     # minimum bars squeezed before release counts
_LOOKBACK         = 60    # 1H candles to load


# ── Math helpers (pure Python, no numpy needed for these small windows) ────────

def _sma(values: list[float], period: int) -> float:
    if len(values) < period:
        return 0.0
    return sum(values[-period:]) / period


def _ema(values: list[float], period: int) -> float:
    if len(values) < period:
        return 0.0
    k   = 2.0 / (period + 1)
    val = sum(values[:period]) / period
    for v in values[period:]:
        val = v * k + val * (1.0 - k)
    return val


def _atr(candles: list[dict], period: int) -> float:
    if len(candles) < period + 1:
        return 0.0
    trs = []
    for i in range(1, len(candles)):
        p = candles[i - 1]["c"]
        c = candles[i]
        trs.append(max(c["h"] - c["l"], abs(c["h"] - p), abs(c["l"] - p)))
    return sum(trs[-period:]) / period


def _squeeze_state(closes: list[float], candles: list[dict]) -> tuple[bool, float]:
    """Return (is_squeezed, ema20) for the most recent bar in the slice."""
    if len(closes) < _BB_PERIOD or len(candles) < _ATR_PERIOD + 1:
        return False, 0.0

    sma   = _sma(closes, _BB_PERIOD)
    var   = sum((c - sma) ** 2 for c in closes[-_BB_PERIOD:]) / _BB_PERIOD
    std   = var ** 0.5
    bb_up = sma + _BB_MULT * std
    bb_lo = sma - _BB_MULT * std

    ema20  = _ema(closes, _KC_PERIOD)
    atr_v  = _atr(candles, _ATR_PERIOD)
    kc_up  = ema20 + _KC_ATR_MULT * atr_v
    kc_lo  = ema20 - _KC_ATR_MULT * atr_v

    squeezed = bb_up < kc_up and bb_lo > kc_lo
    return squeezed, ema20


# ── Public signal functions ────────────────────────────────────────────────────

def check_bb_squeeze_bullish(symbol: str, cache) -> bool:
    """True when a Bollinger squeeze just released to the upside on 1H.

    Conditions:
    1. At least _MIN_SQUEEZE_BARS consecutive squeezed bars before current bar
    2. Current bar: BBands expanded outside Keltner (released)
    3. Close > EMA20 at release (bullish momentum direction)
    """
    candles = cache.get_ohlcv(symbol, window=_LOOKBACK, tf="1h")
    need    = _BB_PERIOD + _ATR_PERIOD + _MIN_SQUEEZE_BARS + 2
    if len(candles) < need:
        return False

    closes = [c["c"] for c in candles]

    # Current bar — must NOT be squeezed (release)
    curr_sq, ema20 = _squeeze_state(closes, candles)
    if curr_sq:
        return False

    # Momentum: bullish release requires close above EMA20
    if closes[-1] <= ema20 or ema20 == 0.0:
        return False

    # Previous _MIN_SQUEEZE_BARS bars must ALL be squeezed
    for lookback in range(1, _MIN_SQUEEZE_BARS + 1):
        sq, _ = _squeeze_state(closes[:-lookback], candles[:-lookback])
        if not sq:
            return False

    return True


def check_bb_squeeze_bearish(symbol: str, cache) -> bool:
    """True when a Bollinger squeeze just released to the downside on 1H.

    Conditions:
    1. At least _MIN_SQUEEZE_BARS consecutive squeezed bars before current bar
    2. Current bar: BBands expanded outside Keltner (released)
    3. Close < EMA20 at release (bearish momentum direction)
    """
    candles = cache.get_ohlcv(symbol, window=_LOOKBACK, tf="1h")
    need    = _BB_PERIOD + _ATR_PERIOD + _MIN_SQUEEZE_BARS + 2
    if len(candles) < need:
        return False

    closes = [c["c"] for c in candles]

    curr_sq, ema20 = _squeeze_state(closes, candles)
    if curr_sq:
        return False

    # Bearish release: close below EMA20
    if closes[-1] >= ema20 or ema20 == 0.0:
        return False

    for lookback in range(1, _MIN_SQUEEZE_BARS + 1):
        sq, _ = _squeeze_state(closes[:-lookback], candles[:-lookback])
        if not sq:
            return False

    return True
