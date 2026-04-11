"""
backtest/engine.py
Pure numpy vectorized backtest engine.
No live scorer calls. No asyncio overhead.
All indicators computed once as arrays, signal detection
is boolean array intersection — O(n) total not O(n²).
"""
import json
import os
from dataclasses import dataclass
from functools import partial

import numpy as np
import yaml

DATA_DIR  = os.path.join(os.path.dirname(__file__), "data")
_CFG_PATH = os.path.join(os.path.dirname(__file__), "..", "config.yaml")
with open(_CFG_PATH) as _f:
    _CFG = yaml.safe_load(_f)
WARMUP    = 210      # bars skipped for indicator warmup
MAX_HOLD  = 48       # 1H bars max — force close after 2 days
RR        = 2.5      # reward:risk ratio
SL_MULT   = 1.5      # stop = entry +/- ATR x SL_MULT
MIN_SL    = 0.005    # minimum 0.5% stop distance (noise floor)

# ── Trading costs from config.yaml ───────────────────────────────────────────
_BT_CFG       = _CFG.get("backtest", {})
_TAKER_FEE    = float(_BT_CFG.get("taker_fee_pct", 0.0005))    # 0.05% per side
_SLIPPAGE     = float(_BT_CFG.get("slippage_pct", 0.0002))     # 0.02% per side
_FUNDING_8H   = float(_BT_CFG.get("funding_cost_per_8h", 0.0001))  # 0.01% per 8h
FEE_RT        = (_TAKER_FEE + _SLIPPAGE) * 2   # round-trip: entry + exit
SLIP_FRAC     = _SLIPPAGE                       # applied to entry/exit prices
FUNDING_PER_BAR_1H = _FUNDING_8H / 8.0          # funding cost per 1H bar held
FUNDING_PER_BAR_5M = _FUNDING_8H / 96.0         # funding cost per 5M bar held

# numpy column indices
O, H, L, C, V, TS = 0, 1, 2, 3, 4, 5

# ── Volatility ratio gate ─────────────────────────────────────────────────────

def _mc_vol_ratio(bars_1h: np.ndarray, cursor: int,
                  short_window: int = 6,
                  long_window: int  = 48) -> float:
    """Returns ratio of recent volatility to baseline volatility.

    ratio > 1.0 = current market more volatile than normal
    ratio < 1.0 = current market calmer than normal

    Uses 1H bars:
      short_window = 6H  (recent vol)
      long_window  = 48H (baseline vol = 2-day average)
    """
    if cursor < long_window + 2:
        return 1.0   # insufficient data — treat as normal

    # Recent volatility (last 6 bars = 6H)
    recent_start  = max(0, cursor - short_window)
    recent_closes = bars_1h[recent_start:cursor, C]
    recent_closes = recent_closes[recent_closes > 0]
    if len(recent_closes) < 3:
        return 1.0

    recent_log_rets = np.diff(np.log(recent_closes))
    recent_vol = float(np.std(recent_log_rets)) if len(recent_log_rets) > 0 else 0.0

    # Baseline volatility (48H window ending before the recent window)
    base_start  = max(0, cursor - long_window)
    base_closes = bars_1h[base_start:cursor - short_window, C]
    base_closes = base_closes[base_closes > 0]
    if len(base_closes) < 10:
        return 1.0

    base_log_rets = np.diff(np.log(base_closes))
    base_vol = float(np.std(base_log_rets)) if len(base_log_rets) > 0 else 0.0

    if base_vol < 1e-10:
        return 1.0

    return round(recent_vol / base_vol, 3)


# ─── Data loading ─────────────────────────────────────────────────────────────

def load(symbol: str) -> dict[str, np.ndarray] | None:
    """Load all timeframes for a symbol from backtest/data/{symbol}.json"""
    path = os.path.join(DATA_DIR, f"{symbol}.json")
    if not os.path.exists(path):
        return None
    with open(path) as f:
        raw = json.load(f)
    result = {}
    for key, bars in raw.items():
        if bars:
            result[key] = np.array(
                [[b["o"], b["h"], b["l"], b["c"], b["v"], b["ts"]]
                 for b in bars],
                dtype=np.float64
            )
    return result


# ─── Indicators ───────────────────────────────────────────────────────────────

def ema(closes: np.ndarray, period: int) -> np.ndarray:
    """EMA — seeded SMA then Wilder smoothing."""
    n = len(closes)
    if n < period:
        return np.zeros(n)
    k   = 2.0 / (period + 1)
    out = np.empty(n)
    out[:period] = 0.0
    out[period - 1] = closes[:period].mean()
    # use numpy cumulative approach via python loop
    # — but now only called ONCE per symbol not per bar
    rest = closes[period:]
    prev = out[period - 1]
    for i, c in enumerate(rest, start=period):
        prev   = c * k + prev * (1.0 - k)
        out[i] = prev
    return out


def atr(bars: np.ndarray, period: int = 14) -> np.ndarray:
    n  = len(bars)
    tr = np.zeros(n)
    if n < 2:
        return tr
    tr[1:] = np.maximum(
        bars[1:, H] - bars[1:, L],
        np.maximum(np.abs(bars[1:, H] - bars[:-1, C]),
                   np.abs(bars[1:, L] - bars[:-1, C]))
    )
    out = np.zeros(n)
    if n >= period:
        out[period - 1] = tr[1:period].mean()
        a = 1.0 / period
        for i in range(period, n):
            out[i] = out[i - 1] * (1 - a) + tr[i] * a
    return out


def rsi(closes: np.ndarray, period: int = 14) -> np.ndarray:
    n    = len(closes)
    out  = np.full(n, 50.0)
    if n < period + 1:
        return out
    d    = np.diff(closes, prepend=closes[0])
    gain = np.where(d > 0,  d, 0.0)
    loss = np.where(d < 0, -d, 0.0)
    ag, al = np.zeros(n), np.zeros(n)
    ag[period] = gain[1:period + 1].mean()
    al[period] = loss[1:period + 1].mean()
    for i in range(period + 1, n):
        ag[i] = (ag[i - 1] * (period - 1) + gain[i]) / period
        al[i] = (al[i - 1] * (period - 1) + loss[i]) / period
    with np.errstate(divide="ignore", invalid="ignore"):
        out = 100.0 - 100.0 / (1.0 + np.where(al > 0, ag / al, 100.0))
    return out


def adx(bars: np.ndarray, period: int = 14) -> np.ndarray:
    n = len(bars)
    if n < period * 2 + 2:
        return np.zeros(n)
    up  = np.diff(bars[:, H], prepend=bars[0, H])
    dn  = -np.diff(bars[:, L], prepend=bars[0, L])
    pdm = np.where((up > dn)  & (up > 0),  up, 0.0)
    mdm = np.where((dn > up)  & (dn > 0),  dn, 0.0)
    tr_ = np.zeros(n)
    tr_[1:] = np.maximum(
        bars[1:, H] - bars[1:, L],
        np.maximum(np.abs(bars[1:, H] - bars[:-1, C]),
                   np.abs(bars[1:, L] - bars[:-1, C]))
    )

    def wilder(x: np.ndarray) -> np.ndarray:
        w = np.zeros(n)
        w[period] = x[1:period + 1].sum()
        for i in range(period + 1, n):
            w[i] = w[i - 1] - w[i - 1] / period + x[i]
        return w

    sa, sp, sm = wilder(tr_), wilder(pdm), wilder(mdm)
    with np.errstate(divide="ignore", invalid="ignore"):
        dip = np.where(sa > 0, 100.0 * sp / sa, 0.0)
        dim = np.where(sa > 0, 100.0 * sm / sa, 0.0)
        dx  = np.where((dip + dim) > 0,
                       np.abs(dip - dim) / (dip + dim) * 100.0, 0.0)
    out = np.zeros(n)
    start = period * 2
    if n > start:
        out[start] = dx[period:start].mean()
        for i in range(start + 1, n):
            out[i] = (out[i - 1] * (period - 1) + dx[i]) / period
    return out


def vol_ma(bars: np.ndarray, period: int = 20) -> np.ndarray:
    """Rolling volume MA using numpy stride tricks — no Python loop."""
    n   = len(bars)
    vol = bars[:, V]
    out = np.zeros(n)
    if n < period:
        return out
    # np.cumsum trick: O(n) instead of O(n x period)
    cs       = np.cumsum(vol)
    out[period:] = (cs[period:] - cs[:-period]) / period
    # fill first `period` bars with expanding mean
    out[period - 1] = vol[:period].mean()
    return out


def _utc_hour(ts_ms: int) -> int:
    return (ts_ms // 3_600_000) % 24


def _detect_range(bars, i, n_candles=8,
                  min_width=0.0018, max_width=0.0080,
                  atr_mult_max=1.35,
                  max_boundary_touches=4):
    start = i - n_candles
    if start < 1:
        return False, 0.0, 0.0
    window   = bars[start:i]
    rng_high = window[:, H].max()
    rng_low  = window[:, L].min()
    if rng_low <= 0:
        return False, 0.0, 0.0
    mid   = (rng_high + rng_low) / 2.0
    width = (rng_high - rng_low) / mid
    if not (min_width <= width <= max_width):
        return False, 0.0, 0.0

    # Fix 1: Boundary touch count — reject churning ranges
    upper_touches = int(np.sum(window[:, H] >= rng_high * 0.999))
    lower_touches = int(np.sum(window[:, L] <= rng_low  * 1.001))
    if upper_touches > max_boundary_touches and lower_touches > max_boundary_touches:
        return False, 0.0, 0.0  # range exhausted
    if upper_touches == 0 or lower_touches == 0:
        return False, 0.0, 0.0  # not a real range

    win  = bars[max(0, i-20):i]
    trs  = [max(win[j,H]-win[j,L], abs(win[j,H]-win[j-1,C]),
                abs(win[j,L]-win[j-1,C])) for j in range(1,len(win))]
    if not trs:
        return False, 0.0, 0.0
    avg = sum(trs[:-1]) / max(len(trs)-1, 1)
    if avg > 0 and trs[-1] > avg * atr_mult_max:
        return False, 0.0, 0.0
    return True, rng_high, rng_low


def _is_breakout_long(bar, rng_high, vol_mult, vm_val):
    if bar[C] <= rng_high: return False
    if max(bar[O], bar[C]) <= rng_high: return False
    if vm_val > 0 and bar[V] < vm_val * vol_mult: return False
    return True


def _is_breakout_short(bar, rng_low, vol_mult, vm_val):
    if bar[C] >= rng_low: return False
    if min(bar[O], bar[C]) >= rng_low: return False
    if vm_val > 0 and bar[V] < vm_val * vol_mult: return False
    return True


def map_series(src_bars: np.ndarray, src_vals: np.ndarray,
               tgt_bars: np.ndarray) -> np.ndarray:
    """Map src_vals (aligned with src_bars) onto tgt_bars via timestamp.
    Each target bar gets the most recent source value at or before its ts.
    """
    idx = np.searchsorted(src_bars[:, TS], tgt_bars[:, TS], side="right") - 1
    idx = np.clip(idx, 0, len(src_vals) - 1)
    return src_vals[idx]


# ─── Signal detection (boolean arrays) ───────────────────────────────────────

def sig_fvg_long(bars_1h: np.ndarray) -> np.ndarray:
    """Bullish FVG: bar[i-2].high < bar[i].low  (upward price gap)."""
    s = np.zeros(len(bars_1h), bool)
    if len(bars_1h) >= 3:
        s[2:] = bars_1h[:-2, H] < bars_1h[2:, L]
    return s


def sig_fvg_short(bars_1h: np.ndarray) -> np.ndarray:
    """Bearish FVG: bar[i-2].low > bar[i].high  (downward price gap)."""
    s = np.zeros(len(bars_1h), bool)
    if len(bars_1h) >= 3:
        s[2:] = bars_1h[:-2, L] > bars_1h[2:, H]
    return s


def sig_ema_pullback_long(bars: np.ndarray,
                           e21: np.ndarray, e50: np.ndarray,
                           touch_pct: float = 0.003) -> np.ndarray:
    """EMA21 pullback LONG: trend intact + touch + green bar + vol confirm."""
    with np.errstate(divide="ignore", invalid="ignore"):
        touch = np.where(e21 > 0, np.abs(bars[:, C] - e21) / e21 <= touch_pct, False)
    green = bars[:, C] > bars[:, O]
    vol_c = np.zeros(len(bars), bool)
    vol_c[1:] = bars[1:, V] > bars[:-1, V]
    return (e21 > e50) & touch & green & vol_c


def sig_ema_pullback_short(bars: np.ndarray,
                            e21: np.ndarray, e50: np.ndarray,
                            touch_pct: float = 0.003) -> np.ndarray:
    """EMA21 pullback SHORT: mirror of long."""
    with np.errstate(divide="ignore", invalid="ignore"):
        touch = np.where(e21 > 0, np.abs(bars[:, C] - e21) / e21 <= touch_pct, False)
    red   = bars[:, C] < bars[:, O]
    vol_c = np.zeros(len(bars), bool)
    vol_c[1:] = bars[1:, V] > bars[:-1, V]
    return (e21 < e50) & touch & red & vol_c


def sig_vwap_long(bars: np.ndarray) -> np.ndarray:
    """VWAP lower-band touch using rolling cumsum — no Python loop."""
    n     = len(bars)
    rsi_  = rsi(bars[:, C])
    vol   = bars[:, V]
    pv    = bars[:, C] * vol     # price x volume
    pc2   = bars[:, C] ** 2      # price^2 for std calculation

    win = 20
    out = np.zeros(n, bool)

    # Rolling sums via cumsum — O(n) no loop
    cs_vol = np.cumsum(vol)
    cs_pv  = np.cumsum(pv)
    cs_pc2 = np.cumsum(pc2)

    if n < win + 1:
        return out

    # For i >= win:
    # sum_vol  = cs_vol[i] - cs_vol[i-win]
    # sum_pv   = cs_pv[i]  - cs_pv[i-win]
    # sum_pc2  = cs_pc2[i] - cs_pc2[i-win]
    i = np.arange(win, n)
    sum_vol = cs_vol[i]   - cs_vol[i - win]
    sum_pv  = cs_pv[i]    - cs_pv[i - win]
    sum_pc2 = cs_pc2[i]   - cs_pc2[i - win]

    with np.errstate(divide="ignore", invalid="ignore"):
        vwap_ = np.where(sum_vol > 0, sum_pv / sum_vol, bars[i, C])
        var_  = np.where(sum_vol > 0, sum_pc2 / sum_vol - vwap_**2, 0.0)
        std_  = np.sqrt(np.maximum(var_, 0.0))

    lower = vwap_ - 2.0 * std_

    # price low touched or went below the lower band
    at_band       = np.zeros(n, bool)
    at_band[win:] = bars[win:, L] <= lower

    out = (rsi_ < 35) & at_band
    return out


def sig_microrange_short(bars: np.ndarray,
                          win: int = 10,
                          max_rng_pct: float = 0.007) -> np.ndarray:
    """Microrange SHORT — vectorized using rolling max/min + cumsum vol."""
    n    = len(bars)
    rsi_ = rsi(bars[:, C])
    vm   = vol_ma(bars, 20)
    sig  = np.zeros(n, bool)

    if n < win + WARMUP:
        return sig

    # Rolling max of highs and min of lows over window `win`
    # Use stride tricks for O(n) rolling window
    from numpy.lib.stride_tricks import sliding_window_view

    highs = bars[:, H]
    lows  = bars[:, L]

    # sliding_window_view gives shape (n-win+1, win)
    # We want the window ENDING at each bar i -> bars[i-win:i]
    # sliding_window_view(arr, win)[i] = arr[i:i+win]
    # so sliding_window_view(arr, win)[i-win] = arr[i-win:i] ✓
    if n < win:
        return sig

    swv_h = sliding_window_view(highs, win)  # shape: (n-win+1, win)
    swv_l = sliding_window_view(lows,  win)

    roll_hi = swv_h.max(axis=1)   # shape: (n-win+1,)
    roll_lo = swv_l.min(axis=1)

    # roll_hi[k] = max of bars[k:k+win]
    # we want: at bar i, box = bars[i-win:i]
    # -> roll_hi[i-win] = max of bars[i-win:i-win+win] = bars[i-win:i] ✓
    # valid range: i from win to n-1  ->  k from 0 to n-win-1
    k          = np.arange(0, n - win)
    bar_i      = k + win   # bar index for each window

    mid        = (roll_hi[k] + roll_lo[k]) / 2.0
    safe_mid   = np.where(mid > 0, mid, 1.0)
    tight      = (roll_hi[k] - roll_lo[k]) / safe_mid <= max_rng_pct

    price_i    = bars[bar_i, C]
    near_top   = price_i >= roll_hi[k] * (1.0 - 0.002)

    vm_at_i    = vm[bar_i]
    vol_at_i   = bars[bar_i, V]
    quiet_vol  = (vm_at_i > 0) & (vol_at_i < vm_at_i * 0.8)

    overbought = rsi_[bar_i] > 60.0

    # Only valid in WARMUP+ region
    warmup_ok  = bar_i >= WARMUP

    fired = tight & near_top & quiet_vol & overbought & warmup_ok

    sig[bar_i[fired]] = True
    return sig


def sig_wyckoff_spring(bars: np.ndarray,
                       lookback: int = 50) -> np.ndarray:
    """Wyckoff spring: wick pierces recent range_low then closes back inside.
    Signals accumulation — smart money absorbing supply at support.

    Conditions (all must hold):
    1. bar.low < min(bars[-lookback:-1].low)  ← pierces range low
    2. bar.close > min(bars[-lookback:-1].low) ← closes back above it
    3. Depth of pierce ≤ 0.5% of price        ← shallow sweep only
    4. Volume ≥ 1.5× 20-bar vol MA            ← absorption volume
    """
    n      = len(bars)
    vm     = vol_ma(bars, 20)
    sig    = np.zeros(n, bool)

    for i in range(lookback + 20, n):
        ref_lows   = bars[i - lookback: i, L]
        range_low  = ref_lows.min()
        bar_low    = bars[i, L]
        bar_close  = bars[i, C]

        # Must wick below range low
        if bar_low >= range_low:
            continue
        # Must close back above range low
        if bar_close <= range_low:
            continue
        # Shallow pierce only (≤ 0.5%)
        if range_low > 0 and (range_low - bar_low) / range_low > 0.005:
            continue
        # Volume spike
        if vm[i] > 0 and bars[i, V] < vm[i] * 1.5:
            continue

        sig[i] = True
    return sig


def sig_wyckoff_upthrust(bars: np.ndarray,
                          lookback: int = 50) -> np.ndarray:
    """Wyckoff upthrust: wick pierces recent range_high then closes back inside.
    Signals distribution — smart money selling supply at resistance.

    Conditions:
    1. bar.high > max(bars[-lookback:-1].high)  ← pierces range high
    2. bar.close < max(bars[-lookback:-1].high) ← closes back below it
    3. Depth ≤ 0.5% of price
    4. Volume ≥ 1.5× 20-bar vol MA
    """
    n   = len(bars)
    vm  = vol_ma(bars, 20)
    sig = np.zeros(n, bool)

    for i in range(lookback + 20, n):
        ref_highs  = bars[i - lookback: i, H]
        range_high = ref_highs.max()
        bar_high   = bars[i, H]
        bar_close  = bars[i, C]

        if bar_high <= range_high:
            continue
        if bar_close >= range_high:
            continue
        if range_high > 0 and (bar_high - range_high) / range_high > 0.005:
            continue
        if vm[i] > 0 and bars[i, V] < vm[i] * 1.5:
            continue

        sig[i] = True
    return sig


def sig_liq_sweep_long(bars: np.ndarray,
                        lookback: int = 20) -> np.ndarray:
    """Liquidity sweep LONG: price sweeps equal lows (stop hunt) then reverses up.

    Equal lows = previous swing low within 0.2% of another swing low.
    Sweep = wick goes below both lows, close comes back above them.

    Conditions:
    1. Find two recent swing lows within 0.2% of each other (equal lows)
    2. Current bar wicks below both
    3. Current bar closes above the lower of the two
    4. Volume ≥ 1.5× average (institutional participation)
    """
    n   = len(bars)
    vm  = vol_ma(bars, 20)
    sig = np.zeros(n, bool)

    for i in range(lookback + 20, n):
        window = bars[i - lookback: i]
        lows   = window[:, L]

        # Find swing lows (local minima)
        swing_lows = []
        for j in range(2, len(lows) - 2):
            if lows[j] == lows[max(0,j-2):j+3].min():
                swing_lows.append(lows[j])

        if len(swing_lows) < 2:
            continue

        # Check for equal lows (within 0.2%)
        eq_low = None
        for a in range(len(swing_lows)):
            for b in range(a+1, len(swing_lows)):
                if swing_lows[a] > 0:
                    diff = abs(swing_lows[a] - swing_lows[b]) / swing_lows[a]
                    if diff <= 0.002:
                        eq_low = min(swing_lows[a], swing_lows[b])
                        break
            if eq_low:
                break

        if eq_low is None:
            continue

        # Current bar sweeps below equal lows then closes above
        if bars[i, L] >= eq_low:
            continue
        if bars[i, C] <= eq_low:
            continue
        # Volume confirmation
        if vm[i] > 0 and bars[i, V] < vm[i] * 1.5:
            continue

        sig[i] = True
    return sig


def sig_liq_sweep_short(bars: np.ndarray,
                         lookback: int = 20) -> np.ndarray:
    """Liquidity sweep SHORT: price sweeps equal highs (stop hunt) then reverses down.
    Mirror of sig_liq_sweep_long — sweeps above equal highs, closes back below.
    """
    n   = len(bars)
    vm  = vol_ma(bars, 20)
    sig = np.zeros(n, bool)

    for i in range(lookback + 20, n):
        window = bars[i - lookback: i]
        highs  = window[:, H]

        swing_highs = []
        for j in range(2, len(highs) - 2):
            if highs[j] == highs[max(0,j-2):j+3].max():
                swing_highs.append(highs[j])

        if len(swing_highs) < 2:
            continue

        eq_high = None
        for a in range(len(swing_highs)):
            for b in range(a+1, len(swing_highs)):
                if swing_highs[a] > 0:
                    diff = abs(swing_highs[a] - swing_highs[b]) / swing_highs[a]
                    if diff <= 0.002:
                        eq_high = max(swing_highs[a], swing_highs[b])
                        break
            if eq_high:
                break

        if eq_high is None:
            continue

        if bars[i, H] <= eq_high:
            continue
        if bars[i, C] >= eq_high:
            continue
        if vm[i] > 0 and bars[i, V] < vm[i] * 1.5:
            continue

        sig[i] = True
    return sig


# ─── Regime masks ─────────────────────────────────────────────────────────────

def mask_trend(b4h: np.ndarray, ref: np.ndarray) -> np.ndarray:
    return map_series(b4h, adx(b4h) > 25, ref)


def mask_range(b4h: np.ndarray, ref: np.ndarray) -> np.ndarray:
    return map_series(b4h, adx(b4h) < 22, ref)


def mask_crash(b1d: np.ndarray, ref: np.ndarray) -> np.ndarray:
    e50   = ema(b1d[:, C], 50)
    below = b1d[:, C] < e50
    drop  = np.zeros(len(b1d), bool)
    for i in range(7, len(b1d)):
        prev = b1d[i - 7, C]
        if prev > 0:
            drop[i] = (b1d[i, C] - prev) / prev < -0.12
    return map_series(b1d, below & drop, ref)


def mask_weekly_long(b1w: np.ndarray | None, ref: np.ndarray) -> np.ndarray:
    if b1w is None or len(b1w) < 11:
        return np.ones(len(ref), bool)
    return map_series(b1w, b1w[:, C] > ema(b1w[:, C], 10), ref)


def mask_weekly_short(b1w: np.ndarray | None, ref: np.ndarray) -> np.ndarray:
    return ~mask_weekly_long(b1w, ref)


def mask_htf_bull(b4h: np.ndarray | None, ref: np.ndarray) -> np.ndarray:
    if b4h is None:
        return np.ones(len(ref), bool)
    return map_series(b4h, b4h[:, C] > ema(b4h[:, C], 21), ref)


def mask_htf_bear(b4h: np.ndarray | None, ref: np.ndarray) -> np.ndarray:
    if b4h is None:
        return np.ones(len(ref), bool)
    return map_series(b4h, b4h[:, C] < ema(b4h[:, C], 21), ref)


# ─── Trade simulation ─────────────────────────────────────────────────────────

@dataclass
class Trade:
    symbol:         str
    strategy:       str
    direction:      str
    bar_idx:        int
    entry:          float
    stop:           float
    tp:             float
    outcome:        str   = "TIMEOUT"
    pnl_r:          float = 0.0
    vol_ratio:      float = 0.0


def simulate(entry_idx: np.ndarray, bars: np.ndarray,
             atr_: np.ndarray, direction: str,
             symbol: str, strategy: str,
             max_hold: int = MAX_HOLD,
             mc_threshold: float = 0.0) -> list[Trade]:
    """Walk forward from each entry to find TP/SL hit. Prevents overlapping.

    Costs applied:
      - Slippage: entry price worsened by SLIP_FRAC (market order impact)
      - Taker fees: round-trip fee deducted from PnL (FEE_RT)
      - Funding: FUNDING_PER_BAR_1H × hold_bars deducted from PnL
    """
    trades, last_exit = [], -1

    for idx in entry_idx:
        idx = int(idx)
        if idx <= last_exit or idx + max_hold >= len(bars):
            continue

        raw_entry = bars[idx, C]
        atr_val   = atr_[idx]
        if raw_entry == 0.0 or atr_val == 0.0:
            continue

        # Slippage worsens entry: buy higher, sell lower
        if direction == "LONG":
            entry = raw_entry * (1.0 + SLIP_FRAC)
        else:
            entry = raw_entry * (1.0 - SLIP_FRAC)

        sl_dist = max(atr_val * SL_MULT, entry * MIN_SL)

        if direction == "LONG":
            stop, tp = entry - sl_dist, entry + sl_dist * RR
        else:
            stop, tp = entry + sl_dist, entry - sl_dist * RR

        # ── Volatility ratio gate ─────────────────────────────────────────────
        vr = 0.0
        if mc_threshold > 0.0:
            vr = _mc_vol_ratio(bars, idx)
            if vr > mc_threshold:
                continue
        # ─────────────────────────────────────────────────────────────────────

        outcome, pnl_r, exit_i = "TIMEOUT", 0.0, idx + max_hold
        hold_bars = max_hold

        for j in range(1, max_hold + 1):
            bi = idx + j
            if bi >= len(bars):
                break
            bar = bars[bi]
            if direction == "LONG":
                if bar[L] <= stop:
                    outcome, pnl_r, exit_i, hold_bars = "SL", -1.0, bi, j
                    break
                if bar[H] >= tp:
                    outcome, pnl_r, exit_i, hold_bars = "TP", RR, bi, j
                    break
            else:
                if bar[H] >= stop:
                    outcome, pnl_r, exit_i, hold_bars = "SL", -1.0, bi, j
                    break
                if bar[L] <= tp:
                    outcome, pnl_r, exit_i, hold_bars = "TP", RR, bi, j
                    break

        if outcome == "TIMEOUT":
            ep = bars[min(idx + max_hold, len(bars) - 1), C]
            pnl_r = (ep - entry) / sl_dist if direction == "LONG" \
                     else (entry - ep) / sl_dist

        # Deduct fees + funding
        funding_cost = FUNDING_PER_BAR_1H * hold_bars
        pnl_r = pnl_r - FEE_RT - funding_cost

        trades.append(Trade(
            symbol, strategy, direction, idx,
            entry, stop, tp, outcome, round(pnl_r, 4),
            vr,
        ))
        last_exit = exit_i

    return trades


def simulate_sweep(entry_idx: np.ndarray, bars: np.ndarray,
                   direction: str, symbol: str,
                   rr: float = 2.5,
                   sl_buffer: float = 0.001,
                   mc_threshold: float = 0.0) -> list[Trade]:
    """Simulate liq_sweep trades with wick-based SL.

    SL is placed just beyond the sweep wick — not ATR-based.
    Costs: slippage on entry, round-trip fees, funding per bar held.
    """
    trades, last_exit = [], -1

    for idx in entry_idx:
        idx = int(idx)
        if idx <= last_exit or idx + MAX_HOLD >= len(bars):
            continue

        raw_entry = bars[idx, C]
        if raw_entry == 0.0:
            continue

        # Slippage worsens entry
        if direction == "LONG":
            entry = raw_entry * (1.0 + SLIP_FRAC)
        else:
            entry = raw_entry * (1.0 - SLIP_FRAC)

        # Wick-based SL — not ATR
        if direction == "LONG":
            stop = bars[idx, L] * (1.0 - sl_buffer)
            sl_dist = entry - stop
        else:
            stop = bars[idx, H] * (1.0 + sl_buffer)
            sl_dist = stop - entry

        # Minimum SL distance — never tighter than 0.2%
        sl_dist = max(sl_dist, entry * 0.002)

        if direction == "LONG":
            stop = entry - sl_dist
            tp   = entry + sl_dist * rr
        else:
            stop = entry + sl_dist
            tp   = entry - sl_dist * rr

        # ── Volatility ratio gate ─────────────────────────────────────────────
        vr = 0.0
        if mc_threshold > 0.0:
            vr = _mc_vol_ratio(bars, idx)
            if vr > mc_threshold:
                continue
        # ─────────────────────────────────────────────────────────────────────

        outcome, pnl_r, exit_i = "TIMEOUT", 0.0, idx + MAX_HOLD
        hold_bars = MAX_HOLD

        for j in range(1, MAX_HOLD + 1):
            bi = idx + j
            if bi >= len(bars):
                break
            bar = bars[bi]
            if direction == "LONG":
                if bar[L] <= stop:
                    outcome, pnl_r, exit_i, hold_bars = "SL", -1.0, bi, j; break
                if bar[H] >= tp:
                    outcome, pnl_r, exit_i, hold_bars = "TP", rr, bi, j; break
            else:
                if bar[H] >= stop:
                    outcome, pnl_r, exit_i, hold_bars = "SL", -1.0, bi, j; break
                if bar[L] <= tp:
                    outcome, pnl_r, exit_i, hold_bars = "TP", rr, bi, j; break

        if outcome == "TIMEOUT":
            ep    = bars[min(idx + MAX_HOLD, len(bars) - 1), C]
            pnl_r = ((ep - entry) / sl_dist if direction == "LONG"
                     else (entry - ep) / sl_dist)

        # Deduct fees + funding
        funding_cost = FUNDING_PER_BAR_1H * hold_bars
        pnl_r = pnl_r - FEE_RT - funding_cost

        trades.append(Trade(symbol, "liq_sweep", direction,
                            idx, entry, stop, tp, outcome, round(pnl_r, 4),
                            vr))
        last_exit = exit_i

    return trades


# ─── Strategy runners ─────────────────────────────────────────────────────────

def run_fvg(symbol: str, data: dict, btc_data: dict | None,
            from_ts: int, to_ts: int,
            mc_threshold: float = 0.0) -> list[Trade]:
    b1h = data.get(f"{symbol}:1h")

    b4h = data.get(f"{symbol}:4h")
    b1d = data.get(f"{symbol}:1d")
    _btc = btc_data if btc_data is not None else {}
    b1w  = _btc.get("BTCUSDT:1w") if "BTCUSDT:1w" in _btc else data.get(f"{symbol}:1w")

    if b1h is None or len(b1h) < WARMUP + 10:
        return []

    period_mask = (b1h[:, TS] >= from_ts) & (b1h[:, TS] <= to_ts)
    warmup_mask = np.zeros(len(b1h), bool)
    warmup_mask[WARMUP:] = True
    valid = period_mask & warmup_mask

    atr1h   = atr(b1h)
    rsi1h   = rsi(b1h[:, C])
    fvg_l   = sig_fvg_long(b1h)
    fvg_s   = sig_fvg_short(b1h)
    wk_l    = mask_weekly_long(b1w, b1h)
    wk_s    = mask_weekly_short(b1w, b1h)
    htf_l   = mask_htf_bull(b4h, b1h)
    htf_s   = mask_htf_bear(b4h, b1h)
    trend   = mask_trend(b4h, b1h) if b4h is not None else np.ones(len(b1h), bool)
    crash   = mask_crash(b1d, b1h) if b1d is not None else np.zeros(len(b1h), bool)

    long_entries  = np.where(fvg_l & wk_l  & htf_l & (rsi1h < 45) & trend & valid)[0]
    short_entries = np.where(fvg_s & wk_s  & htf_s & (rsi1h > 55) & (trend | crash) & valid)[0]

    return (simulate(long_entries,  b1h, atr1h, "LONG",  symbol, "fvg",
                     mc_threshold=mc_threshold) +
            simulate(short_entries, b1h, atr1h, "SHORT", symbol, "fvg",
                     mc_threshold=mc_threshold))


def run_ema_pullback(symbol: str, data: dict, btc_data: dict | None,
                     from_ts: int, to_ts: int,
                     mc_threshold: float = 0.0) -> list[Trade]:
    b15m = data.get(f"{symbol}:15m")

    b4h  = data.get(f"{symbol}:4h")
    _btc = btc_data if btc_data is not None else {}
    b1w  = _btc.get("BTCUSDT:1w") if "BTCUSDT:1w" in _btc else data.get(f"{symbol}:1w")

    if b15m is None or len(b15m) < WARMUP + 10:
        return []

    period_mask = (b15m[:, TS] >= from_ts) & (b15m[:, TS] <= to_ts)
    warmup_mask = np.zeros(len(b15m), bool)
    warmup_mask[WARMUP:] = True
    valid = period_mask & warmup_mask

    atr15m  = atr(b15m)
    rsi15m  = rsi(b15m[:, C])
    e21     = ema(b15m[:, C], 21)
    e50     = ema(b15m[:, C], 50)
    wk_l    = mask_weekly_long(b1w, b15m)
    wk_s    = mask_weekly_short(b1w, b15m)
    htf_l   = mask_htf_bull(b4h, b15m)
    htf_s   = mask_htf_bear(b4h, b15m)

    long_entries  = np.where(
        sig_ema_pullback_long(b15m, e21, e50) & wk_l & htf_l
        & (rsi15m >= 30) & (rsi15m <= 50) & valid)[0]
    short_entries = np.where(
        sig_ema_pullback_short(b15m, e21, e50) & wk_s & htf_s
        & (rsi15m >= 50) & (rsi15m <= 70) & valid)[0]

    return (simulate(long_entries,  b15m, atr15m, "LONG",  symbol, "ema_pullback",
                     mc_threshold=mc_threshold) +
            simulate(short_entries, b15m, atr15m, "SHORT", symbol, "ema_pullback",
                     mc_threshold=mc_threshold))


def run_ema_pullback_short_only(symbol: str, data: dict,
                                 btc_data: dict | None,
                                 from_ts: int, to_ts: int,
                                 mc_threshold: float = 0.0) -> list[Trade]:
    """EMA pullback SHORT only — for bear/crash market testing.

    LONG entries removed entirely.
    Tests: is the SHORT ema_pullback profitable in bear conditions?
    Used to evaluate ETH SHORT strategy in 2025 bear market.
    """
    b15m = data.get(f"{symbol}:15m")
    b4h  = data.get(f"{symbol}:4h")

    _btc = btc_data if btc_data is not None else {}
    b1w  = _btc.get("BTCUSDT:1w") if "BTCUSDT:1w" in _btc \
           else data.get(f"{symbol}:1w")

    if b15m is None or len(b15m) < WARMUP + 10:
        return []

    period_mask = (b15m[:, TS] >= from_ts) & (b15m[:, TS] <= to_ts)
    warmup_mask = np.zeros(len(b15m), bool)
    warmup_mask[WARMUP:] = True
    valid = period_mask & warmup_mask

    atr15m  = atr(b15m)
    rsi15m  = rsi(b15m[:, C])
    e21     = ema(b15m[:, C], 21)
    e50     = ema(b15m[:, C], 50)
    wk_s    = mask_weekly_short(b1w, b15m)
    htf_s   = mask_htf_bear(b4h, b15m) if b4h is not None \
               else np.ones(len(b15m), bool)

    short_entries = np.where(
        sig_ema_pullback_short(b15m, e21, e50)
        & wk_s & htf_s
        & (rsi15m >= 50) & (rsi15m <= 70)
        & valid
    )[0]
    short_entries = short_entries[short_entries >= WARMUP]

    return simulate(short_entries, b15m, atr15m, "SHORT",
                    symbol, "ema_pullback_short",
                    mc_threshold=mc_threshold)


def run_ema_pullback_short_v2(symbol: str, data: dict,
                               btc_data: dict | None,
                               from_ts: int, to_ts: int,
                               mc_threshold: float = 0.0) -> list[Trade]:
    """EMA pullback SHORT with wick-based SL — improved version.

    SL = just above the pullback bar high (natural invalidation).
    If price goes above the pullback bar high after we short,
    the EMA rejection failed — exit immediately.
    Tighter SL = closer TP = higher WR.
    """
    b15m = data.get(f"{symbol}:15m")
    b4h  = data.get(f"{symbol}:4h")

    _btc = btc_data if btc_data is not None else {}
    b1w  = _btc.get("BTCUSDT:1w") if "BTCUSDT:1w" in _btc \
           else data.get(f"{symbol}:1w")

    if b15m is None or len(b15m) < WARMUP + 10:
        return []

    period_mask = (b15m[:, TS] >= from_ts) & (b15m[:, TS] <= to_ts)
    warmup_mask = np.zeros(len(b15m), bool)
    warmup_mask[WARMUP:] = True
    valid = period_mask & warmup_mask

    rsi15m  = rsi(b15m[:, C])
    e21     = ema(b15m[:, C], 21)
    e50     = ema(b15m[:, C], 50)
    wk_s    = mask_weekly_short(b1w, b15m)
    htf_s   = mask_htf_bear(b4h, b15m) if b4h is not None \
               else np.ones(len(b15m), bool)

    short_entries = np.where(
        sig_ema_pullback_short(b15m, e21, e50)
        & wk_s & htf_s
        & (rsi15m >= 50) & (rsi15m <= 70)
        & valid
    )[0]
    short_entries = short_entries[short_entries >= WARMUP]

    # Wick-based SL: SL = pullback bar high × 1.001
    return simulate_sweep(short_entries, b15m, "SHORT",
                          symbol, rr=2.5, sl_buffer=0.001,
                          mc_threshold=mc_threshold)


def run_vwap_band(symbol: str, data: dict, btc_data: dict | None,
                  from_ts: int, to_ts: int,
                  mc_threshold: float = 0.0) -> list[Trade]:
    b15m = data.get(f"{symbol}:15m")

    b4h  = data.get(f"{symbol}:4h")

    if b15m is None or len(b15m) < WARMUP + 10:
        return []

    period_mask = (b15m[:, TS] >= from_ts) & (b15m[:, TS] <= to_ts)
    warmup_mask = np.zeros(len(b15m), bool)
    warmup_mask[WARMUP:] = True
    valid = period_mask & warmup_mask

    atr15m  = atr(b15m)
    range_m = mask_range(b4h, b15m) if b4h is not None else np.ones(len(b15m), bool)

    long_entries = np.where(sig_vwap_long(b15m) & range_m & valid)[0]
    return simulate(long_entries, b15m, atr15m, "LONG", symbol, "vwap_band",
                    mc_threshold=mc_threshold)


def run_microrange(symbol: str, data: dict, btc_data: dict | None,
                   from_ts: int, to_ts: int,
                   mc_threshold: float = 0.0) -> list[Trade]:
    b5m = data.get(f"{symbol}:5m")

    b1d = data.get(f"{symbol}:1d")

    if b5m is None or len(b5m) < WARMUP + 10:
        return []

    period_mask = (b5m[:, TS] >= from_ts) & (b5m[:, TS] <= to_ts)
    warmup_mask = np.zeros(len(b5m), bool)
    warmup_mask[WARMUP:] = True
    valid = period_mask & warmup_mask

    atr5m   = atr(b5m)
    crash_m = mask_crash(b1d, b5m) if b1d is not None else np.zeros(len(b5m), bool)

    short_entries = np.where(sig_microrange_short(b5m) & crash_m & valid)[0]
    return simulate(short_entries, b5m, atr5m, "SHORT", symbol, "microrange",
                    max_hold=24, mc_threshold=mc_threshold)


def run_wyckoff_range(symbol: str, data: dict,
                      btc_data: dict | None,
                      from_ts: int, to_ts: int,
                      mc_threshold: float = 0.0) -> list[Trade]:
    """Wyckoff spring + upthrust range trading.
    Spring = LONG at range support. Upthrust = SHORT at range resistance.
    Works on any coin in RANGE regime.
    Uses 1H bars — daily timeframe signals.
    """
    b1h = data.get(f"{symbol}:1h")
    b4h = data.get(f"{symbol}:4h")


    if b1h is None or len(b1h) < WARMUP + 10:
        return []

    period_mask = (b1h[:, TS] >= from_ts) & (b1h[:, TS] <= to_ts)
    warmup_mask = np.zeros(len(b1h), bool)
    warmup_mask[WARMUP:] = True
    valid = period_mask & warmup_mask

    atr1h   = atr(b1h)
    rsi1h   = rsi(b1h[:, C])
    range_m = mask_range(b4h, b1h) if b4h is not None else np.ones(len(b1h), bool)

    spring   = sig_wyckoff_spring(b1h)
    upthrust = sig_wyckoff_upthrust(b1h)

    # Spring LONG: spring + range regime + RSI < 45 (oversold)
    long_entries  = np.where(spring   & range_m & (rsi1h < 45) & valid)[0]
    # Upthrust SHORT: upthrust + range regime + RSI > 55 (overbought)
    short_entries = np.where(upthrust & range_m & (rsi1h > 55) & valid)[0]

    long_entries  = long_entries[long_entries >= WARMUP]
    short_entries = short_entries[short_entries >= WARMUP]

    trades  = simulate(long_entries,  b1h, atr1h, "LONG",  symbol, "wyckoff_range",
                       mc_threshold=mc_threshold)
    trades += simulate(short_entries, b1h, atr1h, "SHORT", symbol, "wyckoff_range",
                       mc_threshold=mc_threshold)
    return trades


def _near_key_level_vec(bars_1d: np.ndarray, bars_1w: np.ndarray,
                         ref_bars: np.ndarray, tol: float = 0.005) -> np.ndarray:
    """Returns boolean array: True at ref_bar[i] if price is within tol
    of any PDH/PDL/PWH/PWL level at that point in time.
    Uses 0.5% tolerance (wider than live 0.3% to account for sweep depth).
    """
    n   = len(ref_bars)
    out = np.zeros(n, bool)

    for i in range(WARMUP, n):
        price = ref_bars[i, C]
        if price == 0:
            continue

        levels = []

        # PDH/PDL — prior day high/low
        if bars_1d is not None and len(bars_1d) >= 2:
            # Find daily bar before current time
            ts   = ref_bars[i, TS]
            didx = np.searchsorted(bars_1d[:, TS], ts, side="right") - 1
            if didx >= 2:
                levels.append(bars_1d[didx - 1, H])  # PDH
                levels.append(bars_1d[didx - 1, L])  # PDL

        # PWH/PWL — prior week high/low
        if bars_1w is not None and len(bars_1w) >= 2:
            ts   = ref_bars[i, TS]
            widx = np.searchsorted(bars_1w[:, TS], ts, side="right") - 1
            if widx >= 2:
                levels.append(bars_1w[widx - 1, H])  # PWH
                levels.append(bars_1w[widx - 1, L])  # PWL

        for lvl in levels:
            if lvl > 0 and abs(price - lvl) / lvl <= tol:
                out[i] = True
                break

    return out


def run_liq_sweep(symbol: str, data: dict,
                  btc_data: dict | None,
                  from_ts: int, to_ts: int,
                  mc_threshold: float = 0.0) -> list[Trade]:
    """Liquidity sweep reversal — improved institutional version.

    Changes vs previous:
    - Wick-based SL (not ATR) — natural invalidation level
    - Key level proximity filter — only sweeps near PDH/PDL/PWH/PWL
    - Improved equal lows: stricter tolerance + min separation + depth
    """
    b1h = data.get(f"{symbol}:1h")
    b4h = data.get(f"{symbol}:4h")
    b1d = data.get(f"{symbol}:1d")

    b1w = data.get(f"{symbol}:1w")
    _btc = btc_data if btc_data is not None else {}
    if "BTCUSDT:1w" in _btc:
        b1w = _btc["BTCUSDT:1w"]

    if b1h is None or len(b1h) < WARMUP + 10:
        return []

    period_mask = (b1h[:, TS] >= from_ts) & (b1h[:, TS] <= to_ts)
    warmup_mask = np.zeros(len(b1h), bool)
    warmup_mask[WARMUP:] = True
    valid = period_mask & warmup_mask

    rsi1h  = rsi(b1h[:, C])
    wk_l   = mask_weekly_long(b1w, b1h)
    wk_s   = mask_weekly_short(b1w, b1h)
    htf_l  = mask_htf_bull(b4h, b1h) if b4h is not None else np.ones(len(b1h), bool)
    htf_s  = mask_htf_bear(b4h, b1h) if b4h is not None else np.ones(len(b1h), bool)
    crash  = mask_crash(b1d, b1h)    if b1d is not None else np.zeros(len(b1h), bool)

    # Key level proximity filter — only sweeps near institutional levels
    key_lvl = _near_key_level_vec(b1d, b1w, b1h, tol=0.005)

    sweep_l = sig_liq_sweep_long(b1h)
    sweep_s = sig_liq_sweep_short(b1h)

    # LONG: sweep + near key level + macro bull + HTF bull + not overbought
    long_entries  = np.where(
        sweep_l & key_lvl & wk_l & htf_l & (rsi1h < 55) & valid
    )[0]

    # SHORT: sweep + near key level + macro bear + HTF bear + not oversold
    short_entries = np.where(
        sweep_s & key_lvl & wk_s & (htf_s | crash) & (rsi1h > 45) & valid
    )[0]

    long_entries  = long_entries[long_entries >= WARMUP]
    short_entries = short_entries[short_entries >= WARMUP]

    # Use wick-based SL simulation
    trades  = simulate_sweep(long_entries,  b1h, "LONG",  symbol,
                             mc_threshold=mc_threshold)
    trades += simulate_sweep(short_entries, b1h, "SHORT", symbol,
                             mc_threshold=mc_threshold)
    return trades


def run_wyckoff_spring_only(symbol: str, data: dict,
                             btc_data: dict | None,
                             from_ts: int, to_ts: int,
                             mc_threshold: float = 0.0) -> list[Trade]:
    """Pure Wyckoff spring LONG — works in RANGE and at crash bottoms.
    Best for BTC and ETH during consolidation and accumulation phases.
    """
    b1h = data.get(f"{symbol}:1h")
    b4h = data.get(f"{symbol}:4h")

    _btc = btc_data if btc_data is not None else {}
    b1w  = _btc.get("BTCUSDT:1w") if "BTCUSDT:1w" in _btc else data.get(f"{symbol}:1w")

    if b1h is None or len(b1h) < WARMUP + 10:
        return []

    period_mask = (b1h[:, TS] >= from_ts) & (b1h[:, TS] <= to_ts)
    warmup_mask = np.zeros(len(b1h), bool)
    warmup_mask[WARMUP:] = True
    valid = period_mask & warmup_mask

    atr1h  = atr(b1h)
    rsi1h  = rsi(b1h[:, C])
    wk_l   = mask_weekly_long(b1w, b1h)

    spring = sig_wyckoff_spring(b1h)

    # Spring LONG — no regime restriction (works in RANGE and CRASH bottoms)
    # Weekly gate: only long in macro bull
    long_entries = np.where(spring & wk_l & (rsi1h < 50) & valid)[0]
    long_entries = long_entries[long_entries >= WARMUP]

    return simulate(long_entries, b1h, atr1h, "LONG", symbol, "wyckoff_spring",
                    mc_threshold=mc_threshold)


def run_wyckoff_spring_v2(symbol: str, data: dict,
                           btc_data: dict | None,
                           from_ts: int, to_ts: int,
                           mc_threshold: float = 0.0) -> list[Trade]:
    """Wyckoff spring LONG with wick-based SL — improved version.

    Uses simulate_sweep() which places SL just below the spring wick
    instead of ATR-based SL. Natural invalidation: if price goes below
    the wick that was supposed to be the spring, the setup is invalid.

    TP = entry + SL_distance × 2.5 (closer target = more hits).
    """
    b1h = data.get(f"{symbol}:1h")
    b4h = data.get(f"{symbol}:4h")

    _btc = btc_data if btc_data is not None else {}
    b1w  = _btc.get("BTCUSDT:1w") if "BTCUSDT:1w" in _btc \
           else data.get(f"{symbol}:1w")

    if b1h is None or len(b1h) < WARMUP + 10:
        return []

    period_mask = (b1h[:, TS] >= from_ts) & (b1h[:, TS] <= to_ts)
    warmup_mask = np.zeros(len(b1h), bool)
    warmup_mask[WARMUP:] = True
    valid = period_mask & warmup_mask

    rsi1h   = rsi(b1h[:, C])
    wk_l    = mask_weekly_long(b1w, b1h)
    htf_l   = mask_htf_bull(b4h, b1h) if b4h is not None \
               else np.ones(len(b1h), bool)

    spring = sig_wyckoff_spring(b1h)

    # Spring LONG: weekly gate + HTF bull + RSI < 50
    long_entries = np.where(
        spring & wk_l & htf_l & (rsi1h < 50) & valid
    )[0]
    long_entries = long_entries[long_entries >= WARMUP]

    # Use wick-based SL simulation (same as liq_sweep)
    return simulate_sweep(long_entries, b1h, "LONG", symbol, rr=2.5, sl_buffer=0.001,
                          mc_threshold=mc_threshold)


def run_wyckoff_upthrust_v2(symbol: str, data: dict,
                              btc_data: dict | None,
                              from_ts: int, to_ts: int,
                              mc_threshold: float = 0.0) -> list[Trade]:
    """Wyckoff upthrust SHORT with wick-based SL.
    Mirror of spring_v2 — wick above range_high, close back inside.
    SL just above the wick high (natural invalidation).
    Works in RANGE and CRASH regimes for SHORT entries.
    """
    b1h = data.get(f"{symbol}:1h")
    b4h = data.get(f"{symbol}:4h")
    b1d = data.get(f"{symbol}:1d")

    _btc = btc_data if btc_data is not None else {}
    b1w  = _btc.get("BTCUSDT:1w") if "BTCUSDT:1w" in _btc \
           else data.get(f"{symbol}:1w")

    if b1h is None or len(b1h) < WARMUP + 10:
        return []

    period_mask = (b1h[:, TS] >= from_ts) & (b1h[:, TS] <= to_ts)
    warmup_mask = np.zeros(len(b1h), bool)
    warmup_mask[WARMUP:] = True
    valid = period_mask & warmup_mask

    rsi1h  = rsi(b1h[:, C])
    wk_s   = mask_weekly_short(b1w, b1h)
    htf_s  = mask_htf_bear(b4h, b1h) if b4h is not None \
              else np.ones(len(b1h), bool)
    crash  = mask_crash(b1d, b1h)    if b1d is not None \
              else np.zeros(len(b1h), bool)

    upthrust = sig_wyckoff_upthrust(b1h)

    short_entries = np.where(
        upthrust & wk_s & (htf_s | crash) & (rsi1h > 50) & valid
    )[0]
    short_entries = short_entries[short_entries >= WARMUP]

    return simulate_sweep(short_entries, b1h, "SHORT", symbol, rr=2.5, sl_buffer=0.001,
                          mc_threshold=mc_threshold)


def sig_cme_gap(bars_1h: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Detect CME gap fill opportunities.

    CME gap = difference between Friday 22:00 UTC close
    and Sunday 23:00 UTC open.

    Bullish CME gap: Sunday open ABOVE Friday close
      → price tends to fill DOWN to Friday close
      → SHORT entry when price is still above Friday close level

    Bearish CME gap: Sunday open BELOW Friday close
      → price tends to fill UP to Friday close
      → LONG entry when price is still below Friday close level

    Returns (long_signal_array, short_signal_array)
    Both are 1H bar boolean arrays.
    """
    n         = len(bars_1h)
    long_sig  = np.zeros(n, bool)
    short_sig = np.zeros(n, bool)

    from datetime import datetime, timezone

    friday_closes: list[tuple[int, float]] = []  # (bar_idx, price)

    for i in range(n):
        ts  = int(bars_1h[i, TS]) / 1000
        dt  = datetime.fromtimestamp(ts, tz=timezone.utc)
        # Friday = weekday 4, hour 21-22 UTC (CME closes ~22:00)
        if dt.weekday() == 4 and 20 <= dt.hour <= 22:
            friday_closes.append((i, bars_1h[i, C]))

    for fri_idx, fri_close in friday_closes:
        # Find the Sunday open — next Sunday bar after Friday
        # Sunday = weekday 6
        for j in range(fri_idx + 1, min(fri_idx + 72, n)):
            ts = int(bars_1h[j, TS]) / 1000
            dt = datetime.fromtimestamp(ts, tz=timezone.utc)
            if dt.weekday() == 6 and dt.hour >= 22:
                sun_open = bars_1h[j, O]
                if sun_open == 0:
                    break

                gap_pct = (sun_open - fri_close) / fri_close

                # Only trade gaps > 0.3% (meaningful gap)
                if abs(gap_pct) < 0.003:
                    break

                # For the next 48 bars after Sunday open,
                # signal when price hasn't filled the gap yet
                for k in range(j, min(j + 48, n)):
                    current_price = bars_1h[k, C]
                    if current_price == 0:
                        continue

                    if gap_pct > 0:
                        # Bullish gap (open above Friday close)
                        # SHORT signal when price still above Friday close
                        if current_price > fri_close:
                            short_sig[k] = True
                        else:
                            break  # gap filled
                    else:
                        # Bearish gap (open below Friday close)
                        # LONG signal when price still below Friday close
                        if current_price < fri_close:
                            long_sig[k] = True
                        else:
                            break  # gap filled
                break

    return long_sig, short_sig


def run_cme_gap(symbol: str, data: dict,
                _btc_data: dict | None,
                from_ts: int, to_ts: int,
                mc_threshold: float = 0.0) -> list[Trade]:
    """CME gap fill — BTC-specific strategy.
    Only valid for BTCUSDT (CME futures tracks BTC).
    """
    if symbol != "BTCUSDT":
        return []

    b1h = data.get(f"{symbol}:1h")
    b4h = data.get(f"{symbol}:4h")


    if b1h is None or len(b1h) < WARMUP + 10:
        return []

    period_mask = (b1h[:, TS] >= from_ts) & (b1h[:, TS] <= to_ts)
    warmup_mask = np.zeros(len(b1h), bool)
    warmup_mask[WARMUP:] = True
    valid = period_mask & warmup_mask

    atr1h = atr(b1h)
    rsi1h = rsi(b1h[:, C])
    htf_l = mask_htf_bull(b4h, b1h) if b4h is not None else np.ones(len(b1h), bool)
    htf_s = mask_htf_bear(b4h, b1h) if b4h is not None else np.ones(len(b1h), bool)

    long_gap, short_gap = sig_cme_gap(b1h)

    # Long gap fill: price below Friday close + HTF bullish
    long_entries  = np.where(long_gap  & htf_l & (rsi1h < 60) & valid)[0]
    # Short gap fill: price above Friday close + HTF bearish
    short_entries = np.where(short_gap & htf_s & (rsi1h > 40) & valid)[0]

    long_entries  = long_entries[long_entries >= WARMUP]
    short_entries = short_entries[short_entries >= WARMUP]

    trades  = simulate(long_entries,  b1h, atr1h, "LONG",  symbol, "cme_gap",
                       mc_threshold=mc_threshold)
    trades += simulate(short_entries, b1h, atr1h, "SHORT", symbol, "cme_gap",
                       mc_threshold=mc_threshold)
    return trades


def run_liq_sweep_short(symbol: str, data: dict,
                         btc_data: dict | None,
                         from_ts: int, to_ts: int,
                         mc_threshold: float = 0.0) -> list[Trade]:
    """Liquidity sweep SHORT only — confirmed edge in bear/crash.
    Used for BTC and ETH during CRASH regime.
    Does NOT include LONG entries — LONG sweeps have no edge on BTC/ETH.
    """
    b1h = data.get(f"{symbol}:1h")
    b4h = data.get(f"{symbol}:4h")
    b1d = data.get(f"{symbol}:1d")

    _btc = btc_data if btc_data is not None else {}
    b1w  = _btc.get("BTCUSDT:1w") if "BTCUSDT:1w" in _btc \
           else data.get(f"{symbol}:1w")

    if b1h is None or len(b1h) < WARMUP + 10:
        return []

    period_mask = (b1h[:, TS] >= from_ts) & (b1h[:, TS] <= to_ts)
    warmup_mask = np.zeros(len(b1h), bool)
    warmup_mask[WARMUP:] = True
    valid = period_mask & warmup_mask

    atr1h  = atr(b1h)
    rsi1h  = rsi(b1h[:, C])
    wk_s   = mask_weekly_short(b1w, b1h)
    htf_s  = mask_htf_bear(b4h, b1h) if b4h is not None \
              else np.ones(len(b1h), bool)
    crash  = mask_crash(b1d, b1h)    if b1d is not None \
              else np.zeros(len(b1h), bool)

    sweep_s = sig_liq_sweep_short(b1h)

    # SHORT only: macro bear + (HTF bear OR crash) + RSI not oversold
    short_entries = np.where(
        sweep_s & wk_s & (htf_s | crash) & (rsi1h > 45) & valid
    )[0]
    short_entries = short_entries[short_entries >= WARMUP]

    return simulate(short_entries, b1h, atr1h, "SHORT", symbol, "liq_sweep_short",
                    mc_threshold=mc_threshold)


# ─── Stats ────────────────────────────────────────────────────────────────────

def compute_stats(trades: list[Trade]) -> dict:
    if not trades:
        return dict(n=0, wins=0, losses=0, timeouts=0,
                    wr=0.0, pf=0.0, avg_r=0.0)
    wins    = [t for t in trades if t.outcome == "TP"]
    losses  = [t for t in trades if t.pnl_r < 0]
    gw      = sum(t.pnl_r for t in wins)
    gl      = abs(sum(t.pnl_r for t in losses))
    return dict(
        n        = len(trades),
        wins     = len(wins),
        losses   = len(losses),
        timeouts = len([t for t in trades if t.outcome == "TIMEOUT"]),
        wr       = round(len(wins) / len(trades) * 100, 1),
        pf       = round(gw / gl, 2) if gl > 0 else 9.99,
        avg_r    = round(sum(t.pnl_r for t in trades) / len(trades), 3),
    )


def _exhausted_4h(b4h, ts_ms, direction,
                   pct=0.015, bars=6):
    """Return True if 4H price already moved > pct in signal direction."""
    if b4h is None or len(b4h) < bars + 1:
        return False
    j = int(np.searchsorted(b4h[:, TS], int(ts_ms), side='right')) - 1
    start_j = j - bars
    if start_j < 0 or j < 0 or j >= len(b4h):
        return False
    start_price = b4h[start_j, O]
    end_price   = b4h[j, C]
    if start_price == 0:
        return False
    move = (end_price - start_price) / start_price
    if direction == "LONG"  and move > pct:
        return True
    if direction == "SHORT" and move < -pct:
        return True
    return False


def run_breakout_retest(symbol, data, btc_data,
                        from_ts, to_ts, rr_ratio=2.2, **_kw):
    b5m = data.get(f"{symbol}:5m")
    b1h = data.get(f"{symbol}:1h")
    b4h = data.get(f"{symbol}:4h")
    _btc = btc_data or {}
    b1w = _btc.get("BTCUSDT:1w")
    if b1w is None:
        b1w = data.get(f"{symbol}:1w")
    if b5m is None or len(b5m) < WARMUP + 20:
        return []
    _br_cfg = _CFG.get("breakout_retest", {})
    _exh_pct  = float(_br_cfg.get("exhaustion_pct",  0.015))
    _exh_bars = int(_br_cfg.get("exhaustion_bars",  6))
    _max_bt   = int(_br_cfg.get("max_boundary_touches", 2))
    _bk_conf  = bool(_br_cfg.get("require_breakout_confirm", True))
    _min_body = float(_br_cfg.get("min_retest_body_ratio", 0.40))
    _crash_pct    = float(_br_cfg.get("crash_cooldown_pct", 1.5)) / 100
    _crash_hours  = int(_br_cfg.get("crash_cooldown_hours", 4))
    _max_ent_30m  = int(_br_cfg.get("max_entries_per_30min", 2))
    _btc_confirm  = bool(_br_cfg.get("btc_confirm_for_alts", True))
    _choppy_mult  = float(_br_cfg.get("choppy_atr_mult", 2.0))
    # Max-hold mirrors live trade_monitor (_max_hold_exceeded).  4h = 48 × 5M bars.
    _max_hold_h    = float(_br_cfg.get("max_hold_hours", 4.0))
    _max_hold_5m   = max(1, int(_max_hold_h * 12))
    # Live-matching features
    _risk_cfg     = _CFG.get("risk", {})
    _max_open     = int(_risk_cfg.get("max_open_positions", 5))
    _max_same_dir = int(_risk_cfg.get("max_same_direction_positions", 3))
    _cooldown_ms  = float(_br_cfg.get("cooldown_mins", 15)) * 60 * 1000  # ms
    _max_day_trades = int(_br_cfg.get("max_trades_per_day", 6))
    _be_trigger_r = float(_risk_cfg.get("breakeven_trigger_r", 2.0))
    _be_disabled  = set(s.lower() for s in _risk_cfg.get("breakeven_disabled_strategies", []))
    _be_active    = "breakout_retest" not in _be_disabled
    n = len(b5m)
    pm = (b5m[:, TS] >= from_ts) & (b5m[:, TS] <= to_ts)
    wm = np.zeros(n, bool); wm[WARMUP:] = True
    valid = pm & wm
    wk_l = mask_weekly_long(b1w, b5m)
    wk_s = mask_weekly_short(b1w, b5m)
    hbull = np.ones(n, bool); hbear = np.ones(n, bool)
    if b1h is not None and len(b1h) > 20:
        e20 = ema(b1h[:, C], 20)
        for idx in range(n):
            j = int(np.searchsorted(b1h[:, TS],
                    int(b5m[idx, TS]), side='right')) - 1
            if 0 <= j < len(b1h) and j < len(e20):
                hbull[idx] = b1h[j, C] > e20[j]
                hbear[idx] = b1h[j, C] < e20[j]
    vm5 = vol_ma(b5m, 20)
    at5 = atr(b5m)
    # Pre-compute 1H ATR for choppy gate (Fix 4)
    at1h = atr(b1h) if b1h is not None and len(b1h) > 25 else None
    at1h_arr = None
    if b1h is not None and len(b1h) > 25:
        at1h_arr = np.zeros(len(b1h))
        for _ai in range(14, len(b1h)):
            _trs = [max(b1h[_aj,H]-b1h[_aj,L], abs(b1h[_aj,H]-b1h[_aj-1,C]),
                        abs(b1h[_aj,L]-b1h[_aj-1,C])) for _aj in range(_ai-13, _ai+1)]
            at1h_arr[_ai] = sum(_trs) / 14
    # Pre-compute BTC 1H candle changes for crash gate (Fix 1)
    btc_1h_changes = None
    if b1h is not None and len(b1h) > 1:
        btc_1h_changes = np.zeros(len(b1h))
        for _ci in range(len(b1h)):
            if b1h[_ci, O] > 0:
                btc_1h_changes[_ci] = (b1h[_ci, C] - b1h[_ci, O]) / b1h[_ci, O]
    # Anti-correlation tracker (Fix 2): recent entry timestamps
    _recent_dir_ts: list[tuple[float, str]] = []
    # Live-matching state
    _open_positions: list[dict] = []  # [{sym, dir, entry, stop, tp, entry_ts, be_moved}]
    _cooldown_until: dict[str, float] = {}  # symbol -> ts_ms when cooldown expires
    _day_trade_count: dict[str, tuple[str, int]] = {}  # symbol -> (date_str, count)
    trades = []; last_exit = -1
    i = WARMUP
    while i < n - 10:
        if not valid[i] or i <= last_exit:
            i += 1; continue
        if 14 <= _utc_hour(int(b5m[i, TS])) < 15:
            i += 1; continue
        cur_ts_ms = float(b5m[i, TS])
        cur_date = str(int(cur_ts_ms // 86_400_000))  # day key

        # ── Live-match: close positions that have reached their exit bar ──
        _open_positions = [p for p in _open_positions if p["close_bar"] > i]

        # ── Live-match: max open positions gate ──
        if len(_open_positions) >= _max_open:
            i += 1; continue

        # ── Live-match: cooldown gate ──
        if symbol in _cooldown_until and cur_ts_ms < _cooldown_until[symbol]:
            i += 1; continue

        # ── Live-match: daily trade cap ──
        _dtc = _day_trade_count.get(symbol, ("", 0))
        if _dtc[0] == cur_date and _dtc[1] >= _max_day_trades:
            i += 1; continue
        # Fix 4: Choppy market gate
        if at1h_arr is not None and b1h is not None:
            j1h = int(np.searchsorted(b1h[:, TS], int(b5m[i, TS]), side='right')) - 1
            if 25 <= j1h < len(at1h_arr) and at1h_arr[j1h] > 0:
                avg_24 = np.mean(at1h_arr[max(0,j1h-24):j1h])
                if avg_24 > 0 and at1h_arr[j1h] > avg_24 * _choppy_mult:
                    i += 1; continue
        ok, rh, rl = _detect_range(b5m, i, max_boundary_touches=_max_bt)
        if not ok:
            i += 1; continue
        vm = float(vm5[i]) if i < len(vm5) else 0.0
        # ── Live-match: max same direction gate ──
        _long_count = sum(1 for p in _open_positions if p["dir"] == "LONG")
        _short_count = sum(1 for p in _open_positions if p["dir"] == "SHORT")
        _allow_long_dir = _long_count < _max_same_dir
        _allow_short_dir = _short_count < _max_same_dir
        bl = _allow_long_dir and _is_breakout_long(b5m[i],  rh, 1.25, vm) and valid[i] and wk_l[i] and hbull[i]
        bs = _allow_short_dir and _is_breakout_short(b5m[i], rl, 1.25, vm) and valid[i] and wk_s[i] and hbear[i]
        # Fix 1: Post-crash cooldown — block LONGs after BTC 1H crash
        if bl and btc_1h_changes is not None and b1h is not None:
            j1h = int(np.searchsorted(b1h[:, TS], int(b5m[i, TS]), side='right')) - 1
            if j1h >= _crash_hours:
                for _ch in range(j1h - _crash_hours, j1h + 1):
                    if 0 <= _ch < len(btc_1h_changes) and btc_1h_changes[_ch] < -_crash_pct:
                        bl = False; break
        # Fix 3: BTC must confirm LONG direction for alt coins (not SHORT)
        if _btc_confirm and symbol != "BTCUSDT" and bl and b5m is not None:
            btc_5m = btc_data.get("BTCUSDT:5m") if btc_data else None
            if btc_5m is not None and len(btc_5m) > 10:
                bi_btc = int(np.searchsorted(btc_5m[:, TS], int(b5m[i, TS]), side='right')) - 1
                if bi_btc >= 10:
                    btc_recent = btc_5m[bi_btc-9:bi_btc+1]
                    btc_mid = (btc_recent[:, H].max() + btc_recent[:, L].min()) / 2
                    if btc_5m[bi_btc, C] < btc_mid:
                        bl = False
        # Fix 2: Anti-correlation — max entries in same direction per 30 min
        cur_ts = float(b5m[i, TS])
        cutoff = cur_ts - 1_800_000  # 30 min in ms
        _recent_dir_ts[:] = [(t, d) for t, d in _recent_dir_ts if t > cutoff]
        if bl and sum(1 for _, d in _recent_dir_ts if d == "LONG") >= _max_ent_30m:
            bl = False
        if bs and sum(1 for _, d in _recent_dir_ts if d == "SHORT") >= _max_ent_30m:
            bs = False
        # Exhaustion gate — skip breakout if 4H move already extended
        if bl and _exhausted_4h(b4h, b5m[i, TS], "LONG", _exh_pct, _exh_bars):
            bl = False
        if bs and _exhausted_4h(b4h, b5m[i, TS], "SHORT", _exh_pct, _exh_bars):
            bs = False
        if not bl and not bs:
            i += 1; continue
        direction = "LONG" if bl else "SHORT"
        flip = rh if bl else rl

        # Fix 2: Two-bar breakout confirmation
        if _bk_conf:
            ci = i + 1
            if ci >= n:
                i += 1; continue
            cb = b5m[ci]
            if direction == "LONG" and cb[C] <= flip:
                i += 1; continue  # breakout not confirmed
            if direction == "SHORT" and cb[C] >= flip:
                i += 1; continue  # breakout not confirmed
            # Confirmed — proceed to retest search from bar after confirmation
            retest_start = ci + 1
        else:
            retest_start = i + 1

        rf = False; eb = -1
        for j in range(retest_start, min(retest_start + 8, n)):
            bj = b5m[j]
            if 14 <= _utc_hour(int(bj[TS])) < 15: continue
            if direction == "LONG":
                if bj[L] <= flip*1.002 and bj[C] > flip:
                    rf=True; eb=j; break
                if bj[C] < flip*0.997: break
            else:
                if bj[H] >= flip*0.998 and bj[C] < flip:
                    rf=True; eb=j; break
                if bj[C] > flip*1.003: break
        if not rf or eb < 0:
            i += 1; continue
        # Fix 4: Retest bar quality — reject indecision/wick candles
        rb = b5m[eb]
        rb_body  = abs(rb[C] - rb[O])
        rb_range = rb[H] - rb[L]
        if rb_range > 0 and rb_body / rb_range < _min_body:
            i += 1; continue  # indecision candle
        # Exhaustion re-check at retest confirmation time
        if _exhausted_4h(b4h, b5m[eb, TS], direction, _exh_pct, _exh_bars):
            i += 1; continue
        # Anti-correlation re-check at FIRE (retest-confirm) time.  Between
        # the breakout bar and the retest bar other symbols can enter
        # AWAITING_RETEST and fire first, so without this re-check the 30-min
        # cap can be silently violated.  Use the entry bar timestamp as the
        # reference and count entries recorded in _recent_dir_ts.
        eb_ts = float(b5m[eb, TS])
        eb_cutoff = eb_ts - 1_800_000
        _rdts = [(t, d) for t, d in _recent_dir_ts if t > eb_cutoff]
        if sum(1 for _, d in _rdts if d == direction) >= _max_ent_30m:
            i += 1; continue
        av = float(at5[eb]) if eb < len(at5) else 0.0
        if av <= 0:
            i += 1; continue
        sd = max(av*1.3, flip*0.001)   # 0.1% floor — matches the validated baseline (PF 2.38)
        # Apply slippage to entry
        if direction == "LONG":
            entry_sl = flip * (1.0 + SLIP_FRAC)
        else:
            entry_sl = flip * (1.0 - SLIP_FRAC)
        if direction == "LONG":
            stop = entry_sl - sd; tp = entry_sl + sd*rr_ratio
        else:
            stop = entry_sl + sd; tp = entry_sl - sd*rr_ratio
        if stop <= 0 or tp <= 0:
            i += 1; continue
        fut = b5m[eb+1: eb+1+_max_hold_5m]
        outcome="TIMEOUT"; pnl=0.0; xb=eb+_max_hold_5m; hold_5m=_max_hold_5m
        for k, f in enumerate(fut):
            if direction == "LONG":
                if f[L] <= stop: outcome="SL"; pnl=-1.0; xb=eb+1+k; hold_5m=k+1; break
                if f[H] >= tp:   outcome="TP"; pnl=rr_ratio; xb=eb+1+k; hold_5m=k+1; break
            else:
                if f[H] >= stop: outcome="SL"; pnl=-1.0; xb=eb+1+k; hold_5m=k+1; break
                if f[L] <= tp:   outcome="TP"; pnl=rr_ratio; xb=eb+1+k; hold_5m=k+1; break
        if outcome == "TIMEOUT":
            lp = fut[-1,C] if len(fut) > 0 else entry_sl
            pnl = ((lp-entry_sl)/sd if direction=="LONG" else (entry_sl-lp)/sd)
        # Deduct fees + funding (5M bars)
        funding_cost = FUNDING_PER_BAR_5M * hold_5m
        pnl = pnl - FEE_RT - funding_cost
        trades.append(Trade(symbol=symbol, strategy="breakout_retest",
                            direction=direction, bar_idx=eb,
                            entry=entry_sl, stop=stop, tp=tp,
                            outcome=outcome, pnl_r=round(pnl,4)))
        # Fix 2: Record entry for anti-correlation
        _recent_dir_ts.append((float(b5m[eb, TS]), direction))
        # Live-match: track open position (opened at entry, will close at xb)
        _open_positions.append({
            "sym": symbol, "dir": direction,
            "entry": entry_sl, "stop": stop, "tp": tp,
            "entry_ts": float(b5m[eb, TS]), "close_bar": xb,
            "be_moved": False,
        })
        # Live-match: set cooldown
        _cooldown_until[symbol] = float(b5m[eb, TS]) + _cooldown_ms
        # Live-match: increment daily trade count
        _dtc2 = _day_trade_count.get(symbol, ("", 0))
        if _dtc2[0] == cur_date:
            _day_trade_count[symbol] = (cur_date, _dtc2[1] + 1)
        else:
            _day_trade_count[symbol] = (cur_date, 1)
        last_exit = xb; i = xb + 1
    return trades


RUNNERS: dict = {
    "fvg":                    run_fvg,
    "ema_pullback":           run_ema_pullback,
    "ema_pullback_short":     run_ema_pullback_short_only,
    "ema_pullback_short_v2":  run_ema_pullback_short_v2,
    "vwap_band":              run_vwap_band,
    "microrange":             run_microrange,
    "wyckoff_range":          run_wyckoff_range,
    "liq_sweep":              run_liq_sweep,
    "liq_sweep_short":        run_liq_sweep_short,
    "wyckoff_spring":         run_wyckoff_spring_only,
    "wyckoff_spring_v2":      run_wyckoff_spring_v2,
    "wyckoff_upthrust_v2":    run_wyckoff_upthrust_v2,
    "cme_gap":                run_cme_gap,
    "breakout_retest":        run_breakout_retest,
    "breakout_retest_tp1":    partial(run_breakout_retest, rr_ratio=1.5),
    "breakout_retest_tp2":    partial(run_breakout_retest, rr_ratio=3.0),
}


# Strategy name → config block name mapping (handles aliases)
_STRATEGY_CFG_KEY: dict[str, str] = {
    "fvg":                    "fvg",
    "ema_pullback":           "ema_pullback",
    "ema_pullback_short":     "ema_pullback",
    "ema_pullback_short_v2":  "ema_pullback",
    "vwap_band":              "vwap_band",
    "microrange":             "microrange",
    "wyckoff_range":          "wyckoff_spring",
    "liq_sweep":              "liq_sweep",
    "liq_sweep_short":        "liq_sweep",
    "wyckoff_spring":         "wyckoff_spring",
    "wyckoff_spring_v2":      "wyckoff_spring",
    "wyckoff_upthrust_v2":    "wyckoff_spring",
    "cme_gap":                "fvg",          # no dedicated block — shares fvg default
    "breakout_retest":        "microrange",   # shares microrange config block
    "breakout_retest_tp1":    "microrange",
    "breakout_retest_tp2":    "microrange",
}


def _resolve_mc_threshold(strategy: str,
                          cli_override: float = 0.0) -> float:
    """Return effective vol-ratio threshold for a strategy.

    Priority: CLI override > per-strategy config > 0.0 (disabled).
    """
    if cli_override > 0.0:
        return cli_override
    cfg_key = _STRATEGY_CFG_KEY.get(strategy, strategy)
    return float(_CFG.get(cfg_key, {}).get("mc_vol_ratio_max", 0.0))


def run_strategy(symbol: str, strategy: str, data: dict,
                 btc_data: dict | None,
                 from_ts: int, to_ts: int,
                 mc_threshold: float = 0.0) -> list[Trade]:
    """Dispatch to the named runner, passing mc_threshold through.

    mc_threshold= 0.0 → use per-strategy config (config.yaml mc_vol_ratio_max).
    mc_threshold> 0.0 → CLI override, applied to all strategies.
    mc_threshold=-1.0 → force disabled (no vol gate, ignores config).
    """
    runner = RUNNERS.get(strategy)
    if runner is None:
        return []
    if mc_threshold < 0:
        effective = 0.0   # explicitly disabled
    else:
        effective = _resolve_mc_threshold(strategy, mc_threshold)
    return runner(symbol, data, btc_data, from_ts, to_ts,
                  mc_threshold=effective)
