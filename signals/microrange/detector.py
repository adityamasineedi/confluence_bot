"""Micro-range box detection and entry-zone filters.

All functions are pure (no I/O, no cache).  They operate on plain OHLCV bar dicts
with keys: o, h, l, c, v, ts.

No look-ahead: the box is always computed from the N bars *before* the current bar
(``bars[-window_bars-1:-1]``), so the entry signal only fires after the current bar
has printed — safe for backtest and live use.
"""
import os, yaml
_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "config.yaml")
with open(_CONFIG_PATH) as _f:
    _cfg = yaml.safe_load(_f)
_MR = _cfg.get("microrange", {})

_ENTRY_ZONE_PCT = float(_MR.get("entry_zone_pct", 0.002))
_MAX_VOL_RATIO  = float(_MR.get("max_vol_ratio",  1.3))
_RSI_LONG_MAX   = float(_MR.get("rsi_long_max",   40.0))
_RSI_SHORT_MIN  = float(_MR.get("rsi_short_min",  60.0))


# ── RSI helper (Wilder smoothing) ─────────────────────────────────────────────

def _calc_rsi(closes: list[float], period: int = 14) -> float:
    """Return RSI(period) on the closes list; returns 50.0 when insufficient data."""
    if len(closes) < period + 1:
        return 50.0
    gains, losses = [], []
    for i in range(1, len(closes)):
        change = closes[i] - closes[i - 1]
        gains.append(max(change, 0.0))
        losses.append(max(-change, 0.0))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0.0:
        return 100.0
    return 100.0 - (100.0 / (1.0 + avg_gain / avg_loss))


# ── Box detection ─────────────────────────────────────────────────────────────

def detect_micro_range(
    bars: list[dict],
    window_bars: int,
    range_max_pct: float,
) -> dict | None:
    """Detect a tight consolidation box on the most recent completed bars.

    Uses bars[-window_bars-1:-1] (excludes current bar — no look-ahead).
    Returns a dict with box geometry, or None if the window is too wide.

    Parameters
    ----------
    bars          : OHLCV bar dicts in chronological order (most recent last)
    window_bars   : number of completed bars to measure the box over
    range_max_pct : maximum (high − low) / mid to qualify as a micro-range

    Returns
    -------
    dict with keys: range_low, range_high, range_width, range_width_pct, bar_count
    None if not enough data or box is too wide
    """
    if len(bars) < window_bars + 2:
        return None

    box_bars = bars[-(window_bars + 1):-1]   # completed bars, no current bar
    range_high = max(b["h"] for b in box_bars)
    range_low  = min(b["l"] for b in box_bars)
    if range_low <= 0.0:
        return None

    mid   = (range_high + range_low) / 2.0
    width = range_high - range_low
    pct   = width / mid

    if pct > range_max_pct:
        return None

    return {
        "range_low":       range_low,
        "range_high":      range_high,
        "range_width":     width,
        "range_width_pct": pct,
        "bar_count":       len(box_bars),
    }


# ── Entry zone filters ────────────────────────────────────────────────────────

def near_range_low(price: float, range_low: float, entry_zone_pct: float) -> bool:
    """True when price is within entry_zone_pct above range_low."""
    if range_low <= 0.0:
        return False
    proximity = (price - range_low) / range_low
    return 0.0 <= proximity <= entry_zone_pct


def near_range_high(price: float, range_high: float, entry_zone_pct: float) -> bool:
    """True when price is within entry_zone_pct below range_high."""
    if range_high <= 0.0:
        return False
    proximity = (range_high - price) / range_high
    return 0.0 <= proximity <= entry_zone_pct


# ── Volume filter ─────────────────────────────────────────────────────────────

def low_volume(bars: list[dict], max_vol_ratio: float, lookback: int = 20) -> bool:
    """True when the current bar's volume is NOT a spike (quiet consolidation).

    Rejects entries during volume breakout bars that might invalidate the range.

    Parameters
    ----------
    bars          : OHLCV bar list, most recent last
    max_vol_ratio : current bar volume must be < max_vol_ratio × 20-bar average
    lookback      : number of bars to compute the volume average over
    """
    if len(bars) < lookback + 1:
        return True   # not enough history — assume quiet
    sample   = bars[-(lookback + 1):-1]
    avg_vol  = sum(b["v"] for b in sample) / len(sample)
    curr_vol = bars[-1]["v"]
    if avg_vol <= 0.0:
        return True
    return (curr_vol / avg_vol) <= max_vol_ratio


# ── RSI entry filters ─────────────────────────────────────────────────────────

def rsi_supports_long(closes: list[float], rsi_long_max: float) -> bool:
    """True when RSI(14) ≤ rsi_long_max — oversold zone, good for range long."""
    return _calc_rsi(closes) <= rsi_long_max


def rsi_supports_short(closes: list[float], rsi_short_min: float) -> bool:
    """True when RSI(14) ≥ rsi_short_min — overbought zone, good for range short."""
    return _calc_rsi(closes) >= rsi_short_min


# ── SL / TP calculator (boundary-anchored) ────────────────────────────────────

def compute_levels(
    direction: str,
    range_low: float,
    range_high: float,
    range_width: float,
    stop_pct: float,
    tp_ratio: float,
) -> tuple[float, float]:
    """Return (stop_loss, take_profit) anchored to the range boundary.

    LONG:
        SL = range_low  × (1 − stop_pct)          — just below the floor
        TP = range_low  + range_width × tp_ratio   — target N% across the box

    SHORT:
        SL = range_high × (1 + stop_pct)           — just above the ceiling
        TP = range_high − range_width × tp_ratio   — target N% across the box

    For a 1.0% box (range_width/mid ≈ 0.01) and tp_ratio=0.75, stop_pct=0.003:
        RR ≈ (0.01 × mid × 0.75) / (range_low × 0.003) ≈ 2.5
    """
    if direction == "LONG":
        sl = range_low  * (1.0 - stop_pct)
        tp = range_low  + range_width * tp_ratio
    else:
        sl = range_high * (1.0 + stop_pct)
        tp = range_high - range_width * tp_ratio
    return round(sl, 8), round(tp, 8)
