"""15m EMA Pullback signals — high-frequency trend-continuation entries.

Strategy logic
--------------
Uses the 4H trend direction as the macro filter, then enters on 15m pullbacks
to EMA21. Much higher frequency than the 1H version used in MAIN strategy.

Long setup:
  1. 4H: price above EMA50 (macro uptrend) OR 4H EMA21 > EMA50 (short-term momentum up)
  2. 15m: EMA21 > EMA50 (trend intact on entry timeframe)
  3. 15m: price pulled back to EMA21 (within 0.4%) then bounced
  4. 15m: close is now ABOVE EMA21 (confirmed bounce)
  5. RSI 15m in healthy pullback zone 35-60 (not overbought on entry)
  6. Volume on pullback bar ≤ 1.2× average (quiet retreat = weak sellers)

Short setup: mirror image.
"""

_EMA_FAST    = 21
_EMA_SLOW    = 50
_TOUCH_PCT   = 0.004   # price must be within 0.4% of EMA21 to count as a touch
_RSI_PERIOD  = 14
_RSI_LONG_MIN  = 35    # RSI floor — not crashed below oversold (trend still up)
_RSI_LONG_MAX  = 60    # RSI ceiling — not overbought on entry
_RSI_SHORT_MIN = 40    # RSI floor — not oversold (trend still down)
_RSI_SHORT_MAX = 65    # RSI ceiling — was overbought, now pulling back
_VOL_QUIET_MULT = 1.2  # pullback volume must be ≤ 1.2× average (low-vol pullback)


def _ema(closes: list[float], period: int) -> float:
    if len(closes) < period:
        return 0.0
    k   = 2.0 / (period + 1)
    ema = sum(closes[:period]) / period
    for c in closes[period:]:
        ema = c * k + ema * (1 - k)
    return ema


def _rsi(closes: list[float], period: int = _RSI_PERIOD) -> float:
    if len(closes) < period + 1:
        return 50.0
    gains, losses = [], []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gains.append(max(diff, 0.0))
        losses.append(max(-diff, 0.0))
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0.0:
        return 100.0
    return 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)


def _htf_bullish(symbol: str, cache) -> bool:
    """4H macro bias is bullish: close above 4H EMA50 OR 4H EMA21 > EMA50."""
    bars_4h = cache.get_ohlcv(symbol, window=_EMA_SLOW + 5, tf="4h")
    if len(bars_4h) < _EMA_SLOW:
        return False
    closes_4h = [b["c"] for b in bars_4h]
    ema50_4h  = _ema(closes_4h, _EMA_SLOW)
    ema21_4h  = _ema(closes_4h, _EMA_FAST)
    return closes_4h[-1] > ema50_4h or ema21_4h > ema50_4h


def _htf_bearish(symbol: str, cache) -> bool:
    """4H macro bias is bearish: close below 4H EMA50 AND 4H EMA21 < EMA50."""
    bars_4h = cache.get_ohlcv(symbol, window=_EMA_SLOW + 5, tf="4h")
    if len(bars_4h) < _EMA_SLOW:
        return False
    closes_4h = [b["c"] for b in bars_4h]
    ema50_4h  = _ema(closes_4h, _EMA_SLOW)
    ema21_4h  = _ema(closes_4h, _EMA_FAST)
    return closes_4h[-1] < ema50_4h and ema21_4h < ema50_4h


def check_ema15m_pullback_long(symbol: str, cache) -> bool:
    """True when 4H is bullish and 15m price bounces off EMA21 pullback."""
    if not _htf_bullish(symbol, cache):
        return False

    bars = cache.get_ohlcv(symbol, window=_EMA_SLOW + 10, tf="15m")
    if len(bars) < _EMA_SLOW + 2:
        return False

    closes = [b["c"] for b in bars]
    ema21  = _ema(closes, _EMA_FAST)
    ema50  = _ema(closes, _EMA_SLOW)

    if ema21 <= 0 or ema50 <= 0:
        return False

    # 15m trend intact: EMA21 > EMA50
    if ema21 <= ema50:
        return False

    price = closes[-1]

    # Price recently touched EMA21 (within TOUCH_PCT) and is now bouncing above it
    # Check: previous bar low was near EMA21, current close is above EMA21
    prev_bar = bars[-2]
    prev_low = prev_bar["l"]
    touch    = abs(prev_low - ema21) / ema21 <= _TOUCH_PCT or \
               abs(closes[-2] - ema21) / ema21 <= _TOUCH_PCT

    if not touch:
        return False

    # Current close is above EMA21 (bounce confirmed)
    if price <= ema21:
        return False

    # RSI in healthy pullback zone
    rsi = _rsi(closes)
    if not (_RSI_LONG_MIN <= rsi <= _RSI_LONG_MAX):
        return False

    # Volume on the pullback bar was quiet (low-volume retreat = weak sellers)
    vols = [b["v"] for b in bars[-21:]]
    if len(vols) >= 20:
        avg_vol    = sum(vols[:-1]) / len(vols[:-1])
        pullback_v = bars[-2]["v"]   # volume of the pullback/touch bar
        if avg_vol > 0 and pullback_v > avg_vol * _VOL_QUIET_MULT:
            return False   # high-volume pullback = not a healthy dip, could be reversal

    return True


def check_ema15m_pullback_short(symbol: str, cache) -> bool:
    """True when 4H is bearish and 15m price bounces down off EMA21 rally."""
    if not _htf_bearish(symbol, cache):
        return False

    bars = cache.get_ohlcv(symbol, window=_EMA_SLOW + 10, tf="15m")
    if len(bars) < _EMA_SLOW + 2:
        return False

    closes = [b["c"] for b in bars]
    ema21  = _ema(closes, _EMA_FAST)
    ema50  = _ema(closes, _EMA_SLOW)

    if ema21 <= 0 or ema50 <= 0:
        return False

    # 15m downtrend: EMA21 < EMA50
    if ema21 >= ema50:
        return False

    price = closes[-1]

    # Previous bar high was near EMA21, current close is below EMA21
    prev_bar  = bars[-2]
    prev_high = prev_bar["h"]
    touch     = abs(prev_high - ema21) / ema21 <= _TOUCH_PCT or \
                abs(closes[-2] - ema21) / ema21 <= _TOUCH_PCT

    if not touch:
        return False

    # Current close is below EMA21 (rejection confirmed)
    if price >= ema21:
        return False

    rsi = _rsi(closes)
    if not (_RSI_SHORT_MIN <= rsi <= _RSI_SHORT_MAX):
        return False

    vols = [b["v"] for b in bars[-21:]]
    if len(vols) >= 20:
        avg_vol    = sum(vols[:-1]) / len(vols[:-1])
        pullback_v = bars[-2]["v"]
        if avg_vol > 0 and pullback_v > avg_vol * _VOL_QUIET_MULT:
            return False

    return True


def get_ema15m_long_levels(symbol: str, cache) -> tuple[float, float, float]:
    """Return (entry, stop, tp) for long entry.

    SL is placed below the lower of: EMA21 minus buffer OR the pullback bar's
    actual low. Prevents the SL from sitting inside the wick range when the
    touch bar dipped more than the fixed 0.2% EMA buffer.
    """
    bars = cache.get_ohlcv(symbol, window=_EMA_FAST + 5, tf="15m")
    if len(bars) < _EMA_FAST + 2:
        return 0.0, 0.0, 0.0
    closes = [b["c"] for b in bars]
    ema21  = _ema(closes, _EMA_FAST)
    entry  = closes[-1]
    pullback_low = bars[-2]["l"]
    stop = min(ema21 * (1 - 0.002), pullback_low * (1 - 0.001))
    dist = entry - stop
    if dist <= 0:
        return 0.0, 0.0, 0.0
    tp = entry + dist * 2.5
    return entry, stop, tp


def get_ema15m_short_levels(symbol: str, cache) -> tuple[float, float, float]:
    """Return (entry, stop, tp) for short entry.

    SL is placed above the higher of: EMA21 plus buffer OR the pullback bar's
    actual high. Prevents the SL from sitting inside the wick range when the
    touch bar spiked more than the fixed 0.2% EMA buffer.
    """
    bars = cache.get_ohlcv(symbol, window=_EMA_FAST + 5, tf="15m")
    if len(bars) < _EMA_FAST + 2:
        return 0.0, 0.0, 0.0
    closes = [b["c"] for b in bars]
    ema21  = _ema(closes, _EMA_FAST)
    entry  = closes[-1]
    pullback_high = bars[-2]["h"]
    stop = max(ema21 * (1 + 0.002), pullback_high * (1 + 0.001))
    dist = stop - entry
    if dist <= 0:
        return 0.0, 0.0, 0.0
    tp = entry - dist * 2.5
    return entry, stop, tp
