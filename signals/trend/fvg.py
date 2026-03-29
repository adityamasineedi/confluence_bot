"""Fair Value Gap (FVG) — unfilled price imbalance zones.

An FVG forms when a candle moves so fast that it leaves a gap between bar[-2]'s
high and bar[0]'s low (bullish) or vice versa (bearish).  These gaps act as
price magnets — price returns to fill them roughly 65% of the time.

Bullish FVG (3-bar pattern, k = index of last bar):
    candles[k].low > candles[k-2].high
    gap_low  = candles[k-2].high
    gap_high = candles[k].low
    Signal True when price is inside the gap (gap_low ≤ price ≤ gap_high).

Bearish FVG:
    candles[k].high < candles[k-2].low
    gap_high = candles[k-2].low
    gap_low  = candles[k].high
    Signal True when price returns into the gap from above.

Gap is "filled" when a subsequent candle closes fully through it:
    bullish filled → close < gap_low
    bearish filled → close > gap_high

Virgin FVG: signal fires only on the FIRST entry into a given gap.
After price enters, the gap is recorded in _touched (module-level set).
Subsequent calls with price still inside that gap return False.

Config (from fvg: section in config.yaml):
    lookback_bars : how many 1H bars to scan         (default 50)
    min_gap_pct   : minimum gap width as fraction    (default 0.003 = 0.3%)
"""
import os
import yaml

_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "config.yaml")
with open(_CONFIG_PATH) as _f:
    _cfg = yaml.safe_load(_f)

_FVG_CFG     = _cfg.get("fvg", {})
_LOOKBACK    = int(_FVG_CFG.get("lookback_bars", 50))
_MIN_GAP_PCT = float(_FVG_CFG.get("min_gap_pct", 0.003))
_MACRO_FILTER_ENABLED = bool(_FVG_CFG.get("macro_filter_enabled", True))
_MACRO_EMA_PERIOD     = int(_FVG_CFG.get("macro_ema_period", 200))

# Module-level set of touched FVG keys: (symbol, direction, gap_low, gap_high).
# Records the first time price enters a gap — prevents re-signalling the same zone.
_touched: set[tuple] = set()


def _macro_allows_long(symbol: str, cache) -> bool:
    """LONG FVG only when above 1D EMA200 — prevents going long in bear markets."""
    if not _MACRO_FILTER_ENABLED:
        return True
    bars_1d = cache.get_ohlcv(symbol, window=_MACRO_EMA_PERIOD + 10, tf="1d")
    if not bars_1d or len(bars_1d) < _MACRO_EMA_PERIOD:
        return True   # insufficient data — don't block
    closes = [b["c"] for b in bars_1d]
    ema200 = sum(closes[:_MACRO_EMA_PERIOD]) / _MACRO_EMA_PERIOD
    k = 2.0 / (_MACRO_EMA_PERIOD + 1)
    for c in closes[_MACRO_EMA_PERIOD:]:
        ema200 = c * k + ema200 * (1 - k)
    return closes[-1] > ema200


def _macro_allows_short(symbol: str, cache) -> bool:
    """SHORT FVG only when below 1D EMA200 — prevents shorting in bull markets."""
    if not _MACRO_FILTER_ENABLED:
        return True
    bars_1d = cache.get_ohlcv(symbol, window=_MACRO_EMA_PERIOD + 10, tf="1d")
    if not bars_1d or len(bars_1d) < _MACRO_EMA_PERIOD:
        return True
    closes = [b["c"] for b in bars_1d]
    ema200 = sum(closes[:_MACRO_EMA_PERIOD]) / _MACRO_EMA_PERIOD
    k = 2.0 / (_MACRO_EMA_PERIOD + 1)
    for c in closes[_MACRO_EMA_PERIOD:]:
        ema200 = c * k + ema200 * (1 - k)
    return closes[-1] < ema200


# ── Internal helpers ──────────────────────────────────────────────────────────

def _find_fvg_bullish(
    candles: list[dict],
    min_gap_pct: float,
) -> tuple[float, float] | None:
    """Return (gap_low, gap_high) for the most recent unfilled bullish FVG, or None.

    Scans completed formations only (k ≤ len-2) so the in-progress bar is never
    the last bar of the pattern.  Returns on the first (most recent) match.
    """
    if len(candles) < 4:
        return None

    for k in range(len(candles) - 2, 1, -1):
        gap_low  = candles[k - 2]["h"]
        gap_high = candles[k]["l"]

        if gap_high <= gap_low:
            continue  # no gap — bars overlap

        mid = (gap_high + gap_low) / 2.0
        if mid == 0.0:
            continue
        if (gap_high - gap_low) / mid < min_gap_pct:
            continue  # gap too small to be meaningful

        # Filled check: any subsequent close below gap_low erases the gap
        filled = any(candles[j]["c"] < gap_low for j in range(k + 1, len(candles)))
        if filled:
            continue

        return gap_low, gap_high

    return None


def _find_fvg_bearish(
    candles: list[dict],
    min_gap_pct: float,
) -> tuple[float, float] | None:
    """Return (gap_low, gap_high) for the most recent unfilled bearish FVG, or None."""
    if len(candles) < 4:
        return None

    for k in range(len(candles) - 2, 1, -1):
        gap_high = candles[k - 2]["l"]
        gap_low  = candles[k]["h"]

        if gap_high <= gap_low:
            continue

        mid = (gap_high + gap_low) / 2.0
        if mid == 0.0:
            continue
        if (gap_high - gap_low) / mid < min_gap_pct:
            continue

        # Filled check: any subsequent close above gap_high erases the gap
        filled = any(candles[j]["c"] > gap_high for j in range(k + 1, len(candles)))
        if filled:
            continue

        return gap_low, gap_high

    return None


# ── Public signal functions ───────────────────────────────────────────────────

def check_fvg_bullish(symbol: str, cache) -> bool:
    """True when price is currently inside a virgin unfilled bullish FVG on 1H.

    Returns True exactly once per gap — the first time price enters.  Subsequent
    ticks with price still inside the same gap return False (gap is now touched).
    Returns False (never raises) on missing/insufficient cache data.
    """
    candles = cache.get_ohlcv(symbol, window=_LOOKBACK, tf="1h")
    if not candles or len(candles) < 5:
        return False

    result = _find_fvg_bullish(candles, _MIN_GAP_PCT)
    if result is None:
        return False

    gap_low, gap_high = result
    price = candles[-1]["c"]

    if not (gap_low <= price <= gap_high):
        return False  # price not yet inside the gap

    key = (symbol, "LONG", round(gap_low, 8), round(gap_high, 8))
    if key in _touched:
        return False  # already fired on this gap — only one entry per virgin gap

    if not _macro_allows_long(symbol, cache):
        return False   # bear market — don't buy into old bullish gaps

    _touched.add(key)
    return True


def check_fvg_bearish(symbol: str, cache) -> bool:
    """True when price is currently inside a virgin unfilled bearish FVG on 1H.

    Returns True exactly once per gap.  See check_fvg_bullish for details.
    """
    candles = cache.get_ohlcv(symbol, window=_LOOKBACK, tf="1h")
    if not candles or len(candles) < 5:
        return False

    result = _find_fvg_bearish(candles, _MIN_GAP_PCT)
    if result is None:
        return False

    gap_low, gap_high = result
    price = candles[-1]["c"]

    if not (gap_low <= price <= gap_high):
        return False

    key = (symbol, "SHORT", round(gap_low, 8), round(gap_high, 8))
    if key in _touched:
        return False

    if not _macro_allows_short(symbol, cache):
        return False   # bull market — don't short into old bearish gaps

    _touched.add(key)
    return True


def get_fvg_levels(
    symbol: str,
    cache,
    direction: str,
) -> tuple[float, float] | None:
    """Return (gap_low, gap_high) for the active FVG in the given direction.

    Finds the most recent unfilled FVG regardless of touched status.
    Used by fvg_scorer to compute strategy-specific SL and TP levels.
    Returns None when no qualifying FVG is present.
    """
    candles = cache.get_ohlcv(symbol, window=_LOOKBACK, tf="1h")
    if not candles or len(candles) < 5:
        return None

    if direction == "LONG":
        return _find_fvg_bullish(candles, _MIN_GAP_PCT)
    if direction == "SHORT":
        return _find_fvg_bearish(candles, _MIN_GAP_PCT)
    return None
