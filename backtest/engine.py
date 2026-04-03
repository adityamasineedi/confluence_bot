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

import numpy as np

DATA_DIR  = os.path.join(os.path.dirname(__file__), "data")
WARMUP    = 210      # bars skipped for indicator warmup
MAX_HOLD  = 48       # 1H bars max — force close after 2 days
RR        = 2.5      # reward:risk ratio
SL_MULT   = 1.5      # stop = entry +/- ATR x SL_MULT
MIN_SL    = 0.005    # minimum 0.5% stop distance (noise floor)
FEE_RT    = 0.001    # 0.10% round-trip taker fee

# numpy column indices
O, H, L, C, V, TS = 0, 1, 2, 3, 4, 5


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
    """Liquidity sweep LONG with RSI divergence + volume confirmation.

    Added filters vs previous version:
    1. RSI divergence: price makes lower low at sweep but RSI is higher
       than it was at previous swing low (seller exhaustion)
    2. Confirmation bar: the bar AFTER the sweep must close above sweep close
       (momentum confirming reversal)
    3. Volume on sweep bar must be > 2.0× average (raised from 1.5×)
    """
    n     = len(bars)
    vm    = vol_ma(bars, 20)
    rsi_  = rsi(bars[:, C])
    sig   = np.zeros(n, bool)

    for i in range(lookback + 25, n - 1):
        window = bars[i - lookback: i]
        lows   = window[:, L]

        # Find equal lows
        swing_lows = []
        for j in range(2, len(lows) - 2):
            if lows[j] == lows[max(0,j-2):j+3].min():
                swing_lows.append((j, lows[j]))

        if len(swing_lows) < 2:
            continue

        eq_low_val = None
        prev_low_idx = None
        for a in range(len(swing_lows)):
            for b in range(a+1, len(swing_lows)):
                idx_a, val_a = swing_lows[a]
                idx_b, val_b = swing_lows[b]
                if val_a > 0 and abs(val_a - val_b) / val_a <= 0.002:
                    eq_low_val    = min(val_a, val_b)
                    prev_low_idx  = i - lookback + max(idx_a, idx_b)
                    break
            if eq_low_val:
                break

        if eq_low_val is None or prev_low_idx is None:
            continue

        # Current bar sweeps below equal lows
        if bars[i, L] >= eq_low_val:
            continue
        # Current bar closes ABOVE the equal low (reversal body)
        if bars[i, C] <= eq_low_val:
            continue

        # Volume must be strong (2× not 1.5×)
        if vm[i] > 0 and bars[i, V] < vm[i] * 2.0:
            continue

        # RSI divergence — current sweep RSI > previous swing RSI
        if (prev_low_idx >= 0 and prev_low_idx < len(rsi_) and i < len(rsi_)):
            prev_rsi = rsi_[prev_low_idx]
            curr_rsi = rsi_[i]
            # Price made lower low (sweep) but RSI higher = bullish divergence
            if curr_rsi <= prev_rsi:
                continue  # no divergence — skip

        # Next bar must confirm (close above sweep bar close)
        if i + 1 < n and bars[i+1, C] < bars[i, C]:
            continue  # no follow-through

        sig[i] = True
    return sig


def sig_liq_sweep_short(bars: np.ndarray,
                         lookback: int = 20) -> np.ndarray:
    """Liquidity sweep SHORT with RSI divergence + volume + confirmation bar."""
    n     = len(bars)
    vm    = vol_ma(bars, 20)
    rsi_  = rsi(bars[:, C])
    sig   = np.zeros(n, bool)

    for i in range(lookback + 25, n - 1):
        window = bars[i - lookback: i]
        highs  = window[:, H]

        swing_highs = []
        for j in range(2, len(highs) - 2):
            if highs[j] == highs[max(0,j-2):j+3].max():
                swing_highs.append((j, highs[j]))

        if len(swing_highs) < 2:
            continue

        eq_high_val   = None
        prev_high_idx = None
        for a in range(len(swing_highs)):
            for b in range(a+1, len(swing_highs)):
                idx_a, val_a = swing_highs[a]
                idx_b, val_b = swing_highs[b]
                if val_a > 0 and abs(val_a - val_b) / val_a <= 0.002:
                    eq_high_val   = max(val_a, val_b)
                    prev_high_idx = i - lookback + max(idx_a, idx_b)
                    break
            if eq_high_val:
                break

        if eq_high_val is None or prev_high_idx is None:
            continue

        if bars[i, H] <= eq_high_val:
            continue
        if bars[i, C] >= eq_high_val:
            continue

        if vm[i] > 0 and bars[i, V] < vm[i] * 2.0:
            continue

        # RSI bearish divergence: price made higher high but RSI lower
        if (prev_high_idx >= 0 and prev_high_idx < len(rsi_) and i < len(rsi_)):
            prev_rsi = rsi_[prev_high_idx]
            curr_rsi = rsi_[i]
            if curr_rsi >= prev_rsi:
                continue  # no bearish divergence

        # Confirmation bar: next bar closes below sweep bar close
        if i + 1 < n and bars[i+1, C] > bars[i, C]:
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
    symbol:    str
    strategy:  str
    direction: str
    bar_idx:   int
    entry:     float
    stop:      float
    tp:        float
    outcome:   str   = "TIMEOUT"
    pnl_r:     float = 0.0


def simulate(entry_idx: np.ndarray, bars: np.ndarray,
             atr_: np.ndarray, direction: str,
             symbol: str, strategy: str,
             max_hold: int = MAX_HOLD) -> list[Trade]:
    """Walk forward from each entry to find TP/SL hit. Prevents overlapping."""
    trades, last_exit = [], -1

    for idx in entry_idx:
        idx = int(idx)
        if idx <= last_exit or idx + max_hold >= len(bars):
            continue

        entry   = bars[idx, C]
        atr_val = atr_[idx]
        if entry == 0.0 or atr_val == 0.0:
            continue

        sl_dist = max(atr_val * SL_MULT, entry * MIN_SL)

        if direction == "LONG":
            stop, tp = entry - sl_dist, entry + sl_dist * RR
        else:
            stop, tp = entry + sl_dist, entry - sl_dist * RR

        outcome, pnl_r, exit_i = "TIMEOUT", 0.0, idx + max_hold

        for j in range(1, max_hold + 1):
            bi = idx + j
            if bi >= len(bars):
                break
            bar = bars[bi]
            if direction == "LONG":
                if bar[L] <= stop:
                    outcome, pnl_r, exit_i = "SL", -1.0 - FEE_RT, bi
                    break
                if bar[H] >= tp:
                    outcome, pnl_r, exit_i = "TP", RR - FEE_RT, bi
                    break
            else:
                if bar[H] >= stop:
                    outcome, pnl_r, exit_i = "SL", -1.0 - FEE_RT, bi
                    break
                if bar[L] <= tp:
                    outcome, pnl_r, exit_i = "TP", RR - FEE_RT, bi
                    break

        if outcome == "TIMEOUT":
            ep = bars[min(idx + max_hold, len(bars) - 1), C]
            raw = (ep - entry) / sl_dist if direction == "LONG" \
                  else (entry - ep) / sl_dist
            pnl_r = raw - FEE_RT

        trades.append(Trade(
            symbol, strategy, direction, idx,
            entry, stop, tp, outcome, round(pnl_r, 4)
        ))
        last_exit = exit_i

    return trades


# ─── Strategy runners ─────────────────────────────────────────────────────────

def run_fvg(symbol: str, data: dict, btc_data: dict | None,
            from_ts: int, to_ts: int) -> list[Trade]:
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

    return (simulate(long_entries,  b1h, atr1h, "LONG",  symbol, "fvg") +
            simulate(short_entries, b1h, atr1h, "SHORT", symbol, "fvg"))


def run_ema_pullback(symbol: str, data: dict, btc_data: dict | None,
                     from_ts: int, to_ts: int) -> list[Trade]:
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

    return (simulate(long_entries,  b15m, atr15m, "LONG",  symbol, "ema_pullback") +
            simulate(short_entries, b15m, atr15m, "SHORT", symbol, "ema_pullback"))


def run_vwap_band(symbol: str, data: dict, btc_data: dict | None,
                  from_ts: int, to_ts: int) -> list[Trade]:
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
    return simulate(long_entries, b15m, atr15m, "LONG", symbol, "vwap_band")


def run_microrange(symbol: str, data: dict, btc_data: dict | None,
                   from_ts: int, to_ts: int) -> list[Trade]:
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
                    max_hold=24)


def run_wyckoff_range(symbol: str, data: dict,
                      btc_data: dict | None,
                      from_ts: int, to_ts: int) -> list[Trade]:
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

    trades  = simulate(long_entries,  b1h, atr1h, "LONG",  symbol, "wyckoff_range")
    trades += simulate(short_entries, b1h, atr1h, "SHORT", symbol, "wyckoff_range")
    return trades


def run_liq_sweep(symbol: str, data: dict,
                  btc_data: dict | None,
                  from_ts: int, to_ts: int) -> list[Trade]:
    """Liquidity sweep reversal — equal highs/lows stop hunt then reverse.
    The #1 institutional entry pattern on BTC, ETH, and high-liquidity alts.
    Works on 1H bars across all market conditions.
    """
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

    atr1h  = atr(b1h)
    rsi1h  = rsi(b1h[:, C])
    wk_l   = mask_weekly_long(b1w, b1h)
    wk_s   = mask_weekly_short(b1w, b1h)
    htf_l  = mask_htf_bull(b4h, b1h) if b4h is not None else np.ones(len(b1h), bool)
    htf_s  = mask_htf_bear(b4h, b1h) if b4h is not None else np.ones(len(b1h), bool)
    crash  = mask_crash(b1d, b1h)    if b1d is not None else np.zeros(len(b1h), bool)

    sweep_l = sig_liq_sweep_long(b1h)
    sweep_s = sig_liq_sweep_short(b1h)

    # LONG sweep: macro bull allowed + HTF bullish + RSI not overbought
    long_entries  = np.where(sweep_l & wk_l & htf_l & (rsi1h < 55) & valid)[0]
    # SHORT sweep: macro bear + HTF bearish OR crash + RSI not oversold
    short_entries = np.where(sweep_s & wk_s & (htf_s | crash) & (rsi1h > 45) & valid)[0]

    long_entries  = long_entries[long_entries >= WARMUP]
    short_entries = short_entries[short_entries >= WARMUP]

    trades  = simulate(long_entries,  b1h, atr1h, "LONG",  symbol, "liq_sweep")
    trades += simulate(short_entries, b1h, atr1h, "SHORT", symbol, "liq_sweep")
    return trades


def run_wyckoff_spring_only(symbol: str, data: dict,
                             btc_data: dict | None,
                             from_ts: int, to_ts: int) -> list[Trade]:
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

    return simulate(long_entries, b1h, atr1h, "LONG", symbol, "wyckoff_spring")


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


RUNNERS: dict = {
    "fvg":             run_fvg,
    "ema_pullback":    run_ema_pullback,
    "vwap_band":       run_vwap_band,
    "microrange":      run_microrange,
    "wyckoff_range":   run_wyckoff_range,
    "liq_sweep":       run_liq_sweep,
    "wyckoff_spring":  run_wyckoff_spring_only,
}
