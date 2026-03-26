"""Regime detector — classifies market as TREND, RANGE, or CRASH."""
import os
from enum import StrEnum
from typing import Literal

import numpy as np
import yaml

_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "config.yaml")

with open(_CONFIG_PATH) as _f:
    _cfg = yaml.safe_load(_f)


# ── Regime type ───────────────────────────────────────────────────────────────

class Regime(StrEnum):
    """Market regime.  StrEnum so comparisons against plain strings still work."""
    TREND    = "TREND"
    RANGE    = "RANGE"
    CRASH    = "CRASH"
    PUMP     = "PUMP"       # parabolic upside — mirror of CRASH
    BREAKOUT = "BREAKOUT"   # price just left a RANGE boundary with momentum


# ── Numpy math helpers ────────────────────────────────────────────────────────

def _np_wilder_smooth(arr: np.ndarray, period: int) -> np.ndarray:
    """Wilder's smoothed MA (α = 1/period).  Seeded with SMA of first `period` bars."""
    out = np.zeros(len(arr))
    if len(arr) < period:
        return out
    out[period - 1] = arr[:period].mean()
    for i in range(period, len(arr)):
        out[i] = (out[i - 1] * (period - 1) + arr[i]) / period
    return out


def _np_ema(arr: np.ndarray, period: int) -> np.ndarray:
    """Standard EMA with multiplier 2/(period+1), seeded with SMA."""
    out = np.zeros(len(arr))
    if len(arr) < period:
        return out
    k = 2.0 / (period + 1)
    out[period - 1] = arr[:period].mean()
    for i in range(period, len(arr)):
        out[i] = arr[i] * k + out[i - 1] * (1.0 - k)
    return out


def _np_calc_adx(
    highs: np.ndarray,
    lows: np.ndarray,
    closes: np.ndarray,
    period: int = 14,
) -> dict:
    """ADX / +DI / -DI via pure numpy.  Returns dict with zeros on insufficient data."""
    _zero = {"adx": 0.0, "plus_di": 0.0, "minus_di": 0.0}
    n = len(closes)
    if n < period * 2 + 1:
        return _zero

    # True Range (vectorised)
    prev_c = closes[:-1]
    h1, l1 = highs[1:], lows[1:]
    tr = np.maximum(h1 - l1, np.maximum(np.abs(h1 - prev_c), np.abs(l1 - prev_c)))

    # Directional movement (vectorised)
    up = highs[1:] - highs[:-1]
    dn = lows[:-1] - lows[1:]
    plus_dm  = np.where((up > dn) & (up > 0), up, 0.0)
    minus_dm = np.where((dn > up) & (dn > 0), dn, 0.0)

    # Wilder smoothing
    s_tr   = _np_wilder_smooth(tr, period)
    s_pdm  = _np_wilder_smooth(plus_dm, period)
    s_mdm  = _np_wilder_smooth(minus_dm, period)

    # DI at each bar
    with np.errstate(divide="ignore", invalid="ignore"):
        plus_di  = np.where(s_tr > 0, 100.0 * s_pdm / s_tr, 0.0)
        minus_di = np.where(s_tr > 0, 100.0 * s_mdm / s_tr, 0.0)

    # DX series (only valid from index period-1 onward where smoothed values are non-zero)
    di_sum = plus_di + minus_di
    with np.errstate(divide="ignore", invalid="ignore"):
        dx = np.where(di_sum > 0, 100.0 * np.abs(plus_di - minus_di) / di_sum, 0.0)

    # ADX = Wilder smooth of DX (computed over the valid portion)
    dx_valid = dx[period - 1:]
    s_adx    = _np_wilder_smooth(dx_valid, period)
    adx      = float(s_adx[-1]) if len(s_adx) > 0 else 0.0

    last_atr = float(s_tr[-1])
    if last_atr == 0.0:
        return _zero

    return {
        "adx":      adx,
        "plus_di":  float(plus_di[-1]),
        "minus_di": float(minus_di[-1]),
    }


# ── Legacy pure-Python helpers (kept for backward compat) ────────────────────
# direction_router and get_adx_info() use these; they remain unchanged.

def _wilder_smooth(values: list[float], period: int) -> list[float]:
    out = [0.0] * len(values)
    if len(values) < period:
        return out
    out[period - 1] = sum(values[:period]) / period
    for i in range(period, len(values)):
        out[i] = (out[i - 1] * (period - 1) + values[i]) / period
    return out


def _calc_adx(
    highs: list[float],
    lows: list[float],
    closes: list[float],
    period: int = 14,
) -> dict:
    n = len(closes)
    _zero = {"adx": 0.0, "plus_di": 0.0, "minus_di": 0.0}
    if n < period * 2 + 1:
        return _zero
    tr_v, pdm_v, mdm_v = [], [], []
    for i in range(1, n):
        tr = max(highs[i] - lows[i], abs(highs[i] - closes[i-1]), abs(lows[i] - closes[i-1]))
        up = highs[i] - highs[i-1]
        dn = lows[i-1] - lows[i]
        tr_v.append(tr)
        pdm_v.append(up if up > dn and up > 0 else 0.0)
        mdm_v.append(dn if dn > up and dn > 0 else 0.0)
    s_tr  = _wilder_smooth(tr_v, period)
    s_pdm = _wilder_smooth(pdm_v, period)
    s_mdm = _wilder_smooth(mdm_v, period)
    dx_v  = []
    for i in range(period - 1, len(s_tr)):
        atr = s_tr[i]
        if atr == 0.0:
            dx_v.append(0.0)
            continue
        pdi   = 100.0 * s_pdm[i] / atr
        mdi   = 100.0 * s_mdm[i] / atr
        denom = pdi + mdi
        dx_v.append(100.0 * abs(pdi - mdi) / denom if denom > 0 else 0.0)
    s_adx    = _wilder_smooth(dx_v, period)
    adx      = s_adx[-1] if s_adx else 0.0
    last_atr = s_tr[-1]
    if last_atr == 0.0:
        return _zero
    return {"adx": adx, "plus_di": 100.0 * s_pdm[-1] / last_atr, "minus_di": 100.0 * s_mdm[-1] / last_atr}


# ── RegimeDetector class ──────────────────────────────────────────────────────

class RegimeDetector:
    """Stateful regime classifier with ADX hysteresis.

    State per symbol:
    - ``_adx_history``  : last 3 ADX readings (deque-like list)
    - ``_in_range``     : whether the symbol is currently locked into RANGE mode

    Detection order (highest priority first):
        1. CRASH  — EMA50 cross + 7-day drop + no recovery
        2. RANGE  — ADX hysteresis confirmed + tight price range
        3. TREND  — default when neither above triggers

    Config keys (all under ``regime:`` in config.yaml):
        adx_range_threshold : ADX below this to enter RANGE  (default 20)
        adx_trend_threshold : ADX above this to exit  RANGE  (default 25)
        range_size_max_pct  : max high-low / mid to confirm RANGE (default 0.12)
        crash_weekly_drop   : 7-day % change threshold for CRASH (default -0.12)
        ema_crash_period    : EMA period for crash baseline (default 50)
    """

    _ADX_PERIOD     = 14
    _4H_WINDOW      = 30   # candles fed to ADX (2*period+1 = 29, use 30 for margin)
    _RANGE_4H_BARS  = 20   # candles for range-size check
    _DAILY_WINDOW   = 60   # daily candles for crash / EMA

    _BREAKOUT_WINDOW = 3   # bars after range exit where BREAKOUT can fire (tight window)

    def __init__(self) -> None:
        # symbol -> list of last 3 ADX float readings (oldest first)
        self._adx_history: dict[str, list[float]] = {}
        # symbol -> True while locked in RANGE mode
        self._in_range: dict[str, bool] = {}
        # Breakout tracking — populated when we exit RANGE
        self._range_exit_countdown: dict[str, int]                = {}
        self._range_bounds_at_exit: dict[str, tuple[float, float]] = {}
        self._breakout_direction:   dict[str, str]                = {}
        # PUMP: transition-based — only fires on the bar where pump starts
        self._pump_active: dict[str, bool] = {}
        # Minimum dwell: bars since last regime change (prevents rapid flipping)
        self._regime_dwell: dict[str, int] = {}
        self._regime_current: dict[str, str] = {}

    # ── Public API ────────────────────────────────────────────────────────────

    def detect(self, symbol: str, cache) -> Regime:
        """Classify the current regime.  Never raises; returns TREND on data gaps."""
        rcfg = _cfg.get("regime", {})

        adx_range_thr    = float(rcfg.get("adx_range_threshold",  20.0))
        adx_trend_thr    = float(rcfg.get("adx_trend_threshold",   25.0))
        min_dwell        = int(rcfg.get("regime_min_dwell_bars",    3))
        range_size_max   = float(rcfg.get("range_size_max_pct",     0.12))
        crash_drop_thr   = float(rcfg.get("crash_weekly_drop",     -0.12))
        pump_gain_thr    = float(rcfg.get("pump_weekly_gain",        0.12))
        ema_period       = int(rcfg.get("ema_crash_period",           50))

        # ── Step 1: PUMP — transition-based (only fires on the FIRST bar of a
        #            new pump cycle to avoid re-firing every bar during bull runs) ─
        was_pumping = self._pump_active.get(symbol, False)
        is_pumping  = self._is_pump(symbol, cache, ema_period, pump_gain_thr)
        self._pump_active[symbol] = is_pumping
        if is_pumping and not was_pumping:
            # Just crossed into pump territory — fire PUMP this bar only
            return Regime.PUMP
        # If still pumping after first bar → fall through to TREND (handled as TREND LONG)

        # ── Step 2: Crash check ───────────────────────────────────────────────
        if self._is_crash(symbol, cache, ema_period, crash_drop_thr):
            return Regime.CRASH

        # ── Step 3: ADX on 4H ────────────────────────────────────────────────
        adx_info = self._get_adx(symbol, cache)
        adx      = adx_info["adx"]

        # ── Step 4: ADX hysteresis — track was_ranging BEFORE update ─────────
        was_ranging = self._in_range.get(symbol, False)
        self._update_adx_history(symbol, adx)
        history = self._adx_history[symbol]

        # Minimum dwell: increment bars-in-regime counter; block changes until met
        dwell = self._regime_dwell.get(symbol, min_dwell)
        self._regime_dwell[symbol] = dwell + 1
        dwell_ok = dwell >= min_dwell

        currently_ranging = was_ranging   # start from prior state

        if dwell_ok:
            if not was_ranging:
                # Enter RANGE only when all 3 recent readings are below entry threshold
                if len(history) == 3 and all(v < adx_range_thr for v in history):
                    self._in_range[symbol] = True
                    currently_ranging = True
                    self._regime_dwell[symbol] = 0
            else:
                # Exit RANGE only when ≥2 of last 3 readings are above trend threshold
                # (symmetric with entry — prevents single-spike exits)
                if len(history) >= 2 and sum(v > adx_trend_thr for v in history) >= 2:
                    self._in_range[symbol] = False
                    currently_ranging = False
                    self._regime_dwell[symbol] = 0

        # ── Step 5: Capture range bounds on range exit (for breakout window) ─
        if was_ranging and not currently_ranging:
            rng_high = cache.get_range_high(symbol)
            rng_low  = cache.get_range_low(symbol)
            if rng_high is not None and rng_low is not None:
                self._range_exit_countdown[symbol] = self._BREAKOUT_WINDOW
                self._range_bounds_at_exit[symbol] = (float(rng_high), float(rng_low))

        # ── Step 6: Range size confirmation ──────────────────────────────────
        if currently_ranging and self._is_tight_range(symbol, cache, range_size_max):
            return Regime.RANGE

        # ── Step 7: Breakout window (only active for _BREAKOUT_WINDOW bars
        #            after exiting a confirmed RANGE) ──────────────────────────
        if self._check_breakout(symbol, cache, rcfg):
            return Regime.BREAKOUT

        return Regime.TREND

    # ── Crash detection ───────────────────────────────────────────────────────

    def _is_crash(
        self,
        symbol: str,
        cache,
        ema_period: int,
        crash_drop_thr: float,
    ) -> bool:
        """True when EMA cross + 7-day dump + no recovery align."""
        closes_1d = np.array(cache.get_closes(symbol, window=self._DAILY_WINDOW, tf="1d"))
        if len(closes_1d) < ema_period + 1:
            return False

        ema = _np_ema(closes_1d, ema_period)
        ema50 = ema[-1]
        if ema50 == 0.0:
            return False

        price = closes_1d[-1]

        # 7-day percentage change (need at least 8 bars)
        if len(closes_1d) < 8:
            return False
        change_7d = (price - closes_1d[-8]) / closes_1d[-8]

        # Close below the min of the prior 4 candles (no recovery)
        if len(closes_1d) < 5:
            return False
        recent_min = float(closes_1d[-5:-1].min())

        return (
            price < ema50
            and change_7d < crash_drop_thr
            and price < recent_min
        )

    # ── Pump detection (mirror of crash) ─────────────────────────────────────

    def _is_pump(
        self,
        symbol: str,
        cache,
        ema_period: int,
        pump_gain_thr: float,
    ) -> bool:
        """True when price is in a parabolic upside move.

        Conditions (all must hold):
        1. Price above the EMA-50 on 1D (confirmed uptrend baseline)
        2. 7-day % gain > pump_gain_thr (e.g. +12%) — violent upside momentum
        3. Price making new highs vs prior 4 daily bars (no exhaustion / reversal)
        """
        closes_1d = np.array(cache.get_closes(symbol, window=self._DAILY_WINDOW, tf="1d"))
        if len(closes_1d) < ema_period + 1:
            return False

        ema   = _np_ema(closes_1d, ema_period)
        ema50 = ema[-1]
        if ema50 == 0.0:
            return False

        price = closes_1d[-1]

        # Must be above EMA50 (trending up, not a dead-cat bounce)
        if price <= ema50:
            return False

        # 7-day % gain must exceed threshold
        if len(closes_1d) < 8:
            return False
        change_7d = (price - closes_1d[-8]) / closes_1d[-8]
        if change_7d < pump_gain_thr:
            return False

        # Still making new highs (momentum not exhausted)
        if len(closes_1d) < 5:
            return False
        recent_max = float(closes_1d[-5:-1].max())
        return price > recent_max

    # ── Breakout detection ─────────────────────────────────────────────────────

    def _check_breakout(self, symbol: str, cache, rcfg: dict) -> bool:
        """Return True if we're within the breakout window and price confirmed the break.

        The breakout window opens for _BREAKOUT_WINDOW bars after a RANGE is exited.
        Direction is stored in _breakout_direction[symbol] for the scorer to read.
        """
        countdown = self._range_exit_countdown.get(symbol, 0)
        if countdown <= 0:
            return False

        # Decrement countdown — regardless of whether breakout fires this bar
        self._range_exit_countdown[symbol] = countdown - 1

        bounds = self._range_bounds_at_exit.get(symbol)
        if not bounds:
            return False

        rng_high, rng_low = bounds
        margin = float(rcfg.get("breakout_margin_pct", 0.003))

        # Use closes on 1h for the most current price
        price_data = cache.get_closes(symbol, window=1, tf="1h")
        if not price_data:
            return False
        price = price_data[-1]

        if price > rng_high * (1.0 + margin):
            self._breakout_direction[symbol] = "LONG"
            return True
        elif price < rng_low * (1.0 - margin):
            self._breakout_direction[symbol] = "SHORT"
            return True

        return False

    def get_breakout_direction(self, symbol: str) -> str:
        """Return 'LONG', 'SHORT', or 'NEUTRAL' for the most recent breakout."""
        return self._breakout_direction.get(symbol, "NEUTRAL")

    # ── ADX computation ───────────────────────────────────────────────────────

    def _get_adx(self, symbol: str, cache) -> dict:
        """Compute ADX/+DI/-DI from 4H candles.  Returns zeros on data gap."""
        ohlcv = cache.get_ohlcv(symbol, window=self._4H_WINDOW, tf="4h")
        if len(ohlcv) < self._ADX_PERIOD * 2 + 1:
            return {"adx": 0.0, "plus_di": 0.0, "minus_di": 0.0}
        highs  = np.array([c["h"] for c in ohlcv])
        lows   = np.array([c["l"] for c in ohlcv])
        closes = np.array([c["c"] for c in ohlcv])
        return _np_calc_adx(highs, lows, closes, period=self._ADX_PERIOD)

    # ── ADX history (hysteresis state) ────────────────────────────────────────

    def _update_adx_history(self, symbol: str, adx: float) -> None:
        """Append latest ADX reading, keeping only the last 3."""
        hist = self._adx_history.setdefault(symbol, [])
        hist.append(adx)
        if len(hist) > 3:
            self._adx_history[symbol] = hist[-3:]

    # ── Range size check ──────────────────────────────────────────────────────

    def _is_tight_range(self, symbol: str, cache, max_pct: float) -> bool:
        """True when the 20-bar 4H high-low range is ≤ max_pct of mid-price.
        Sets range_high, range_low, and range_start_timestamp in cache.
        """
        ohlcv = cache.get_ohlcv(symbol, window=self._RANGE_4H_BARS, tf="4h")
        if len(ohlcv) < 10:
            return False

        highs = [c["h"] for c in ohlcv]
        lows  = [c["l"] for c in ohlcv]
        rng_high = max(highs)
        rng_low  = min(lows)
        mid      = (rng_high + rng_low) / 2.0

        if mid == 0.0 or (rng_high - rng_low) / mid > max_pct:
            return False

        # Persist range bounds so range scorers can read them
        cache.set_range_high(symbol, rng_high)
        cache.set_range_low(symbol, rng_low)
        cache.set_range_start_timestamp(symbol, ohlcv[0]["ts"])
        return True

    # ── Introspection helpers ─────────────────────────────────────────────────

    def adx_history(self, symbol: str) -> list[float]:
        """Return the last ≤3 ADX readings for *symbol* (oldest first)."""
        return list(self._adx_history.get(symbol, []))

    def is_in_range(self, symbol: str) -> bool:
        """True when the symbol is currently locked into RANGE mode."""
        return self._in_range.get(symbol, False)

    def reset(self, symbol: str) -> None:
        """Clear all state for *symbol* (e.g. after a data reload)."""
        self._adx_history.pop(symbol, None)
        self._in_range.pop(symbol, None)
        self._range_exit_countdown.pop(symbol, None)
        self._range_bounds_at_exit.pop(symbol, None)
        self._breakout_direction.pop(symbol, None)
        self._pump_active.pop(symbol, None)


# ── Module-level singleton + backward-compat functions ───────────────────────
# direction_router and core/__init__.py call these; they delegate to the class.

_detector = RegimeDetector()


def detect_regime(symbol: str, cache) -> Regime:
    """Module-level convenience — delegates to the shared RegimeDetector instance."""
    return _detector.detect(symbol, cache)


def get_trend_bias(symbol: str, cache) -> Literal["LONG", "SHORT", "NEUTRAL"]:
    """Directional bias within a TREND regime using +DI vs -DI (4H)."""
    rcfg      = _cfg.get("regime", {})
    trend_tf  = rcfg.get("trend_tf", "4h")
    period    = 14
    needed    = period * 2 + 1

    ohlcv = cache.get_ohlcv(symbol, window=needed, tf=trend_tf)
    if len(ohlcv) < needed:
        return "NEUTRAL"

    info = _calc_adx(
        [c["h"] for c in ohlcv],
        [c["l"] for c in ohlcv],
        [c["c"] for c in ohlcv],
        period=period,
    )
    if abs(info["plus_di"] - info["minus_di"]) < 5.0:
        return "NEUTRAL"
    return "LONG" if info["plus_di"] > info["minus_di"] else "SHORT"


def get_adx_info(symbol: str, cache, tf: str = "4h") -> dict:
    """Return raw {adx, plus_di, minus_di} for a symbol/tf. Useful for debugging."""
    period = 14
    needed = period * 2 + 1
    ohlcv  = cache.get_ohlcv(symbol, window=needed, tf=tf)
    if len(ohlcv) < needed:
        return {"adx": 0.0, "plus_di": 0.0, "minus_di": 0.0}
    return _calc_adx(
        [c["h"] for c in ohlcv],
        [c["l"] for c in ohlcv],
        [c["c"] for c in ohlcv],
        period=period,
    )


def get_adx_series(symbol: str, cache, tf: str = "4h", n: int = 4) -> list[float]:
    """Return the last *n* ADX readings (oldest first).  Empty list on insufficient data.

    Used to compute ADX slope — whether momentum is accelerating or exhausting.
    Requires window = n + 2*period bars, so for n=4, period=14: 32 bars.
    """
    period = 14
    window = n + 2 * period
    ohlcv  = cache.get_ohlcv(symbol, window=window, tf=tf)
    if len(ohlcv) < period * 2 + 1:
        return []

    h = np.array([c["h"] for c in ohlcv])
    l = np.array([c["l"] for c in ohlcv])
    c = np.array([c["c"] for c in ohlcv])

    prev_c = c[:-1]
    h1, l1 = h[1:], l[1:]
    tr       = np.maximum(h1 - l1, np.maximum(np.abs(h1 - prev_c), np.abs(l1 - prev_c)))
    up       = h[1:] - h[:-1]
    dn       = l[:-1] - l[1:]
    plus_dm  = np.where((up > dn) & (up > 0), up, 0.0)
    minus_dm = np.where((dn > up) & (dn > 0), dn, 0.0)

    s_tr  = _np_wilder_smooth(tr, period)
    s_pdm = _np_wilder_smooth(plus_dm, period)
    s_mdm = _np_wilder_smooth(minus_dm, period)

    with np.errstate(divide="ignore", invalid="ignore"):
        plus_di  = np.where(s_tr > 0, 100.0 * s_pdm / s_tr, 0.0)
        minus_di = np.where(s_tr > 0, 100.0 * s_mdm / s_tr, 0.0)

    di_sum = plus_di + minus_di
    with np.errstate(divide="ignore", invalid="ignore"):
        dx = np.where(di_sum > 0, 100.0 * np.abs(plus_di - minus_di) / di_sum, 0.0)

    dx_valid = dx[period - 1:]
    s_adx    = _np_wilder_smooth(dx_valid, period)
    valid    = s_adx[period - 1:]   # values with full Wilder warmup

    if len(valid) == 0:
        return []
    return list(valid[-n:] if len(valid) >= n else valid)
