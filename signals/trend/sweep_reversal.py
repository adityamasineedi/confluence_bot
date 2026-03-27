"""Liquidity Sweep Reversal signals — stop-hunt pattern detection on 15m bars.

Strategy logic
--------------
Market makers push price through a prior swing high/low to harvest stop orders,
then immediately reverse. We detect this wick-through-and-close-back pattern and
enter on the reversal candle close.

Sweep LONG (bullish):
  - Price wicks BELOW a recent swing low (sweeping sell stops), then closes ABOVE it.
  - Big volume on the sweep candle confirms institutional participation.
  - RSI is oversold or shows a higher low (divergence) — exhaustion of sellers.

Sweep SHORT (bearish):
  - Price wicks ABOVE a recent swing high (sweeping buy stops), then closes BELOW it.
  - Big volume + overbought or bearish divergence on RSI.

Signal functions follow the standard def check_X(symbol, cache) -> bool interface.
"""

_SWING_LOOKBACK   = 50    # bars to look back for swing highs/lows
_SWING_PIVOT_N    = 5     # bars each side — 5 means only significant pivots (11-bar)
_SWEEP_MARGIN_PCT = 0.0015  # wick must go at least 0.15% beyond the swing level
_CLOSE_BUFFER_PCT = 0.003   # close must be ≥ 0.3% inside the level (stronger reclaim)
_VOL_SPIKE_MULT   = 1.4     # sweep candle volume must be ≥ 1.4× 20-bar average
_BODY_STRENGTH    = 0.4     # close must be in top/bottom 40% of candle range
_RSI_PERIOD       = 14
_RSI_LONG_MAX     = 50      # RSI at sweep must be below 50 (oversold regime)
_RSI_SHORT_MIN    = 50      # RSI at sweep must be above 50 (overbought regime)


def _ema(values: list[float], period: int) -> float:
    if len(values) < period:
        return 0.0
    k   = 2.0 / (period + 1)
    ema = sum(values[:period]) / period
    for v in values[period:]:
        ema = v * k + ema * (1 - k)
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
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)


def _find_swing_lows(bars: list[dict], lookback: int, n: int) -> list[float]:
    """Return a list of swing low prices from the last `lookback` bars (excluding last 2)."""
    levels = []
    segment = bars[-(lookback + n): -2]   # exclude last 2 (forming candles)
    for i in range(n, len(segment) - n):
        low = segment[i]["l"]
        is_pivot = all(segment[i]["l"] <= segment[i - j]["l"] for j in range(1, n + 1)) and \
                   all(segment[i]["l"] <= segment[i + j]["l"] for j in range(1, n + 1))
        if is_pivot:
            levels.append(low)
    return levels


def _find_swing_highs(bars: list[dict], lookback: int, n: int) -> list[float]:
    """Return a list of swing high prices from the last `lookback` bars (excluding last 2)."""
    levels = []
    segment = bars[-(lookback + n): -2]
    for i in range(n, len(segment) - n):
        high = segment[i]["h"]
        is_pivot = all(segment[i]["h"] >= segment[i - j]["h"] for j in range(1, n + 1)) and \
                   all(segment[i]["h"] >= segment[i + j]["h"] for j in range(1, n + 1))
        if is_pivot:
            levels.append(high)
    return levels


def check_sweep_long(symbol: str, cache) -> bool:
    """True when a swing low is swept (wick below) then price closes back above.

    Conditions (all must hold):
    1. Swing low identified in last 50 bars.
    2. Sweep candle: low goes ≥ 0.15% below swing low (stop run).
    3. Sweep candle closes ≥ 0.2% above swing low (strong reclaim).
    4. Volume on sweep candle ≥ 1.4× 20-bar average (institutional absorption).
    5. RSI at close is below 50 (entering from oversold/bearish conditions).
    """
    bars = cache.get_ohlcv(symbol, window=_SWING_LOOKBACK + _SWING_PIVOT_N + 5, tf="15m")
    if len(bars) < _SWING_LOOKBACK + _SWING_PIVOT_N + 2:
        return False

    swing_lows = _find_swing_lows(bars, _SWING_LOOKBACK, _SWING_PIVOT_N)
    if not swing_lows:
        return False

    candle = bars[-1]
    low    = candle["l"]
    close  = candle["c"]

    # Check against each swing low — fire if ANY is swept and reclaimed
    for level in swing_lows:
        # 1. Wick went below the level
        if low > level * (1 - _SWEEP_MARGIN_PCT):
            continue
        # 2. Close is back above the level with buffer
        if close < level * (1 + _CLOSE_BUFFER_PCT):
            continue

        # 3. Volume spike on this candle
        vols = [b["v"] for b in bars[-21:-1]]
        if len(vols) < 10:
            continue
        avg_vol = sum(vols) / len(vols)
        if avg_vol > 0 and candle["v"] < avg_vol * _VOL_SPIKE_MULT:
            continue

        # 4. RSI check
        closes = [b["c"] for b in bars]
        rsi = _rsi(closes)
        if rsi > _RSI_LONG_MAX:
            continue

        # 5. Body strength: close in top 40% of candle range (strong bullish close)
        candle_range = candle["h"] - candle["l"]
        if candle_range > 0:
            if (close - candle["l"]) / candle_range < _BODY_STRENGTH:
                continue

        return True

    return False


def check_sweep_short(symbol: str, cache) -> bool:
    """True when a swing high is swept (wick above) then price closes back below.

    Conditions (all must hold):
    1. Swing high identified in last 50 bars.
    2. Sweep candle: high goes ≥ 0.15% above swing high (stop run).
    3. Sweep candle closes ≥ 0.2% below swing high (strong rejection).
    4. Volume on sweep candle ≥ 1.4× 20-bar average.
    5. RSI at close is above 50 (entering from overbought/bullish conditions).
    """
    bars = cache.get_ohlcv(symbol, window=_SWING_LOOKBACK + _SWING_PIVOT_N + 5, tf="15m")
    if len(bars) < _SWING_LOOKBACK + _SWING_PIVOT_N + 2:
        return False

    swing_highs = _find_swing_highs(bars, _SWING_LOOKBACK, _SWING_PIVOT_N)
    if not swing_highs:
        return False

    candle = bars[-1]
    high   = candle["h"]
    close  = candle["c"]

    for level in swing_highs:
        # 1. Wick went above the level
        if high < level * (1 + _SWEEP_MARGIN_PCT):
            continue
        # 2. Close is back below the level with buffer
        if close > level * (1 - _CLOSE_BUFFER_PCT):
            continue

        # 3. Volume spike
        vols = [b["v"] for b in bars[-21:-1]]
        if len(vols) < 10:
            continue
        avg_vol = sum(vols) / len(vols)
        if avg_vol > 0 and candle["v"] < avg_vol * _VOL_SPIKE_MULT:
            continue

        # 4. RSI check
        closes = [b["c"] for b in bars]
        rsi = _rsi(closes)
        if rsi < _RSI_SHORT_MIN:
            continue

        # 5. Body strength: close in bottom 40% of candle range (strong bearish close)
        candle_range = candle["h"] - candle["l"]
        if candle_range > 0:
            if (candle["h"] - close) / candle_range < _BODY_STRENGTH:
                continue

        return True

    return False


def get_sweep_long_levels(symbol: str, cache) -> tuple[float, float, float]:
    """Return (entry, stop, tp) for the most recent bullish sweep, or (0,0,0).

    Entry : close of sweep candle
    Stop  : lowest wick of sweep candle - 0.05% buffer
    TP    : entry + (entry - stop) * 2.5
    """
    bars = cache.get_ohlcv(symbol, window=_SWING_LOOKBACK + _SWING_PIVOT_N + 5, tf="15m")
    if not bars:
        return 0.0, 0.0, 0.0
    candle = bars[-1]
    entry  = candle["c"]
    stop   = candle["l"] * (1 - 0.0005)
    dist   = entry - stop
    if dist <= 0:
        return 0.0, 0.0, 0.0
    tp = entry + dist * 2.5
    return entry, stop, tp


def get_sweep_short_levels(symbol: str, cache) -> tuple[float, float, float]:
    """Return (entry, stop, tp) for the most recent bearish sweep, or (0,0,0)."""
    bars = cache.get_ohlcv(symbol, window=_SWING_LOOKBACK + _SWING_PIVOT_N + 5, tf="15m")
    if not bars:
        return 0.0, 0.0, 0.0
    candle = bars[-1]
    entry  = candle["c"]
    stop   = candle["h"] * (1 + 0.0005)
    dist   = stop - entry
    if dist <= 0:
        return 0.0, 0.0, 0.0
    tp = entry - dist * 2.5
    return entry, stop, tp
