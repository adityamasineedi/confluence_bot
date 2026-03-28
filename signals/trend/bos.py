"""Break of Structure (BOS) and Change of Character (CHoCH) signals on 1H.

Terminology
-----------
BOS  — Break of Structure: price closes beyond a confirmed swing point in the
       direction of the prevailing trend.  Higher-conviction continuation signal.
CHoCH — Change of Character: in a trending market, price closes beyond the most
       recent swing point in the OPPOSITE direction.  First sign of reversal —
       lower conviction than BOS, used as supplementary confluence.

Pivot detection (mirrors core/swing_monitor._calc_swing)
---------------------------------------------------------
A bar at index i is a swing high when:
    candles[i].h >= candles[i±j].h  for j in 1..pivot_n  (both sides)
Symmetric for swing lows.  Uses >= so equal highs are included.

Scan window: last LOOKBACK 1H bars from config.yaml (fvg.lookback_bars default 50).
All config values read from the bos: section of config.yaml.
"""
import os
import yaml

_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "config.yaml")
with open(_CONFIG_PATH) as _f:
    _cfg = yaml.safe_load(_f)

_BOS_CFG      = _cfg.get("bos", {})
_PIVOT_N      = int(_BOS_CFG.get("pivot_n",          3))
_LOOKBACK     = int(_BOS_CFG.get("lookback_bars",    50))
_VOL_MULT     = float(_BOS_CFG.get("vol_confirm_mult", 1.3))
_VOL_AVG_BARS = 20   # fixed: 20-bar average for volume baseline


# ── Pivot detection ───────────────────────────────────────────────────────────

def detect_swing_points(candles: list[dict], pivot_n: int = 3) -> dict:
    """Return pivot_highs and pivot_lows price-value lists (chronological order).

    Scans candles[pivot_n : -pivot_n] so every candidate has pivot_n confirmed
    bars on both sides — no look-ahead on the most recent pivot_n bars.

    Args:
        candles : OHLCV bar dicts with keys h, l (and others).
        pivot_n : bars each side required to confirm a swing point.

    Returns:
        {"pivot_highs": [float, ...], "pivot_lows": [float, ...]}
        Both lists are in chronological order (oldest → most recent).
    """
    pivot_highs: list[float] = []
    pivot_lows:  list[float] = []

    end = len(candles) - pivot_n
    for i in range(pivot_n, end):
        h = candles[i]["h"]
        l = candles[i]["l"]

        if (all(h >= candles[i - j]["h"] for j in range(1, pivot_n + 1)) and
                all(h >= candles[i + j]["h"] for j in range(1, pivot_n + 1))):
            pivot_highs.append(h)

        if (all(l <= candles[i - j]["l"] for j in range(1, pivot_n + 1)) and
                all(l <= candles[i + j]["l"] for j in range(1, pivot_n + 1))):
            pivot_lows.append(l)

    return {"pivot_highs": pivot_highs, "pivot_lows": pivot_lows}


# ── Volume helper ─────────────────────────────────────────────────────────────

def _vol_spike(candles: list[dict]) -> bool:
    """True when the last bar's volume is ≥ VOL_MULT × 20-bar average."""
    if len(candles) < _VOL_AVG_BARS + 1:
        return False
    avg = sum(b["v"] for b in candles[-_VOL_AVG_BARS - 1:-1]) / _VOL_AVG_BARS
    return avg > 0.0 and candles[-1]["v"] >= _VOL_MULT * avg


# ── BOS functions ─────────────────────────────────────────────────────────────

def check_bos_bullish(symbol: str, cache) -> dict | None:
    """Break of Structure LONG — 1H close above the most recent swing high.

    Conditions:
    1. Identify swing highs/lows in last LOOKBACK 1H bars (pivot_n bars each side).
    2. BOS = current close > most recent swing high.
    3. Prior bar (candles[-2]) close must have been BELOW that high — ensures this
       is a fresh break, not a bar that was already trading above the level.
    4. Volume on break bar ≥ VOL_MULT × 20-bar average.

    Returns:
        None          — insufficient data to assess.
        dict with:
            fired          (bool)  — all BOS conditions met.
            break_level    (float) — the swing high that was broken.
            prior_swing_low (float) — most recent swing low (SL anchor for scorer).
    """
    candles = cache.get_ohlcv(symbol, window=_LOOKBACK, tf="1h")
    min_len = _PIVOT_N * 2 + 4
    if not candles or len(candles) < min_len:
        return None

    swings = detect_swing_points(candles, _PIVOT_N)
    pivot_highs = swings["pivot_highs"]
    pivot_lows  = swings["pivot_lows"]

    if not pivot_highs:
        return {"fired": False, "break_level": 0.0, "prior_swing_low": 0.0}

    swing_high    = pivot_highs[-1]
    current_close = candles[-1]["c"]
    prior_close   = candles[-2]["c"] if len(candles) >= 2 else current_close

    fired = (
        current_close > swing_high      # close above swing high
        and prior_close < swing_high    # fresh break (prior bar was below)
        and _vol_spike(candles)         # institutional volume confirms push
    )

    prior_swing_low = pivot_lows[-1] if pivot_lows else 0.0

    return {
        "fired":           fired,
        "break_level":     swing_high,
        "prior_swing_low": prior_swing_low,
    }


def check_bos_bearish(symbol: str, cache) -> dict | None:
    """Break of Structure SHORT — 1H close below the most recent swing low.

    Mirror of check_bos_bullish for the downside.

    Returns:
        None  — insufficient data.
        dict with:
            fired           (bool)  — all BOS conditions met.
            break_level     (float) — the swing low that was broken.
            prior_swing_high (float) — most recent swing high (SL anchor).
    """
    candles = cache.get_ohlcv(symbol, window=_LOOKBACK, tf="1h")
    min_len = _PIVOT_N * 2 + 4
    if not candles or len(candles) < min_len:
        return None

    swings = detect_swing_points(candles, _PIVOT_N)
    pivot_highs = swings["pivot_highs"]
    pivot_lows  = swings["pivot_lows"]

    if not pivot_lows:
        return {"fired": False, "break_level": 0.0, "prior_swing_high": 0.0}

    swing_low     = pivot_lows[-1]
    current_close = candles[-1]["c"]
    prior_close   = candles[-2]["c"] if len(candles) >= 2 else current_close

    fired = (
        current_close < swing_low       # close below swing low
        and prior_close > swing_low     # fresh break
        and _vol_spike(candles)
    )

    prior_swing_high = pivot_highs[-1] if pivot_highs else 0.0

    return {
        "fired":            fired,
        "break_level":      swing_low,
        "prior_swing_high": prior_swing_high,
    }


# ── CHoCH functions ───────────────────────────────────────────────────────────

def check_choch_bullish(symbol: str, cache) -> bool:
    """Change of Character — in a downtrend, first break above a swing high.

    Conditions:
    1. Confirmed downtrend: last 2 pivot highs are lower highs
       (pivot_highs[-1] < pivot_highs[-2]).
    2. Current close breaks above the most recent lower high.
    3. Prior bar must have been below that level (fresh break).

    Returns True when a potential trend reversal is underway.
    Lower conviction than BOS — used as supplementary confluence only.
    Returns False (never raises) on missing data.
    """
    candles = cache.get_ohlcv(symbol, window=_LOOKBACK, tf="1h")
    min_len = _PIVOT_N * 2 + 4
    if not candles or len(candles) < min_len:
        return False

    swings      = detect_swing_points(candles, _PIVOT_N)
    pivot_highs = swings["pivot_highs"]

    if len(pivot_highs) < 2:
        return False

    # Downtrend: last two highs are lower highs
    if not (pivot_highs[-1] < pivot_highs[-2]):
        return False

    recent_lh   = pivot_highs[-1]
    cur_close   = candles[-1]["c"]
    prior_close = candles[-2]["c"] if len(candles) >= 2 else cur_close

    # CHoCH: fresh close above the most recent lower high
    return cur_close > recent_lh and prior_close < recent_lh


def check_choch_bearish(symbol: str, cache) -> bool:
    """Change of Character — in an uptrend, first break below a swing low.

    Conditions:
    1. Confirmed uptrend: last 2 pivot lows are higher lows
       (pivot_lows[-1] > pivot_lows[-2]).
    2. Current close breaks below the most recent higher low.
    3. Prior bar must have been above that level (fresh break).

    Returns False (never raises) on missing data.
    """
    candles = cache.get_ohlcv(symbol, window=_LOOKBACK, tf="1h")
    min_len = _PIVOT_N * 2 + 4
    if not candles or len(candles) < min_len:
        return False

    swings     = detect_swing_points(candles, _PIVOT_N)
    pivot_lows = swings["pivot_lows"]

    if len(pivot_lows) < 2:
        return False

    # Uptrend: last two lows are higher lows
    if not (pivot_lows[-1] > pivot_lows[-2]):
        return False

    recent_hl   = pivot_lows[-1]
    cur_close   = candles[-1]["c"]
    prior_close = candles[-2]["c"] if len(candles) >= 2 else cur_close

    # CHoCH: fresh close below the most recent higher low
    return cur_close < recent_hl and prior_close > recent_hl
