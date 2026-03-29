"""Regime classifier for backtest — pure function, no cache dependency.

Classifies each bar's regime from raw OHLCV dicts.
Mirrors core/regime_detector.py logic exactly.
Used by all backtest engines to gate strategy entry.
"""


def _wilder_smooth(values: list[float], period: int) -> list[float]:
    """Wilder's smoothed MA seeded with SMA of first `period` bars."""
    out = [0.0] * len(values)
    if len(values) < period:
        return out
    out[period - 1] = sum(values[:period]) / period
    for i in range(period, len(values)):
        out[i] = (out[i - 1] * (period - 1) + values[i]) / period
    return out


def _ema_final(closes: list[float], period: int) -> float:
    """Return the last EMA value (multiplier 2/(period+1)), seeded with SMA."""
    if len(closes) < period:
        return 0.0
    k   = 2.0 / (period + 1)
    ema = sum(closes[:period]) / period
    for c in closes[period:]:
        ema = c * k + ema * (1.0 - k)
    return ema


def _calc_adx(
    highs:  list[float],
    lows:   list[float],
    closes: list[float],
    period: int = 14,
) -> dict:
    """ADX / +DI / -DI via Wilder smoothing.  Returns zeros on insufficient data."""
    n = len(closes)
    _zero = {"adx": 0.0, "plus_di": 0.0, "minus_di": 0.0}
    if n < period * 2 + 1:
        return _zero
    tr_v, pdm_v, mdm_v = [], [], []
    for i in range(1, n):
        tr = max(highs[i] - lows[i],
                 abs(highs[i] - closes[i - 1]),
                 abs(lows[i]  - closes[i - 1]))
        up = highs[i] - highs[i - 1]
        dn = lows[i - 1] - lows[i]
        tr_v.append(tr)
        pdm_v.append(up if up > dn and up > 0 else 0.0)
        mdm_v.append(dn if dn > up and dn > 0 else 0.0)
    s_tr  = _wilder_smooth(tr_v,  period)
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
    return {
        "adx":      adx,
        "plus_di":  100.0 * s_pdm[-1] / last_atr,
        "minus_di": 100.0 * s_mdm[-1] / last_atr,
    }


def classify_regime(
    closes_4h: list[float],
    highs_4h:  list[float],
    lows_4h:   list[float],
    closes_1d: list[float],
    adx_period:     int   = 14,
    adx_range_thr:  float = 20.0,
    adx_trend_thr:  float = 25.0,
    range_size_max: float = 0.12,
    crash_drop_thr: float = -0.12,
    pump_gain_thr:  float = 0.12,
    ema_period:     int   = 50,
) -> str:
    """Return regime string: TREND, RANGE, CRASH, PUMP, BREAKOUT.

    Uses same detection order as live bot:
    1. CRASH — EMA50 cross + 7-day drop + no recovery
    2. PUMP  — EMA50 above + 7-day gain
    3. RANGE — ADX below threshold + tight price range
    4. BREAKOUT — ADX rising after a tight-range window (stateless approximation)
    5. TREND — default

    Note: BREAKOUT detection here is a stateless approximation — it checks if the
    prior 20 bars formed a range and price just broke out.  The live bot uses a
    stateful 3-bar countdown after range exit; this is slightly less precise.

    Handles None and empty lists gracefully — returns "TREND" on insufficient data.
    """
    if not closes_4h or not highs_4h or not lows_4h:
        return "TREND"

    # ── 1. CRASH ─────────────────────────────────────────────────────────────
    if len(closes_1d) >= ema_period + 8:
        ema50      = _ema_final(closes_1d, ema_period)
        price_1d   = closes_1d[-1]
        base_7d    = closes_1d[-8]
        change_7d  = (price_1d - base_7d) / base_7d if base_7d != 0 else 0.0
        recent_min = min(closes_1d[-5:-1])
        if ema50 > 0 and price_1d < ema50 and change_7d < crash_drop_thr and price_1d < recent_min:
            return "CRASH"

    # ── 2. PUMP ──────────────────────────────────────────────────────────────
    if len(closes_1d) >= ema_period + 8:
        ema50      = _ema_final(closes_1d, ema_period)
        price_1d   = closes_1d[-1]
        base_7d    = closes_1d[-8]
        change_7d  = (price_1d - base_7d) / base_7d if base_7d != 0 else 0.0
        recent_max = max(closes_1d[-5:-1])
        if ema50 > 0 and price_1d > ema50 and change_7d > pump_gain_thr and price_1d > recent_max:
            return "PUMP"

    # ── 3. ADX on 4H ─────────────────────────────────────────────────────────
    adx_info = _calc_adx(highs_4h, lows_4h, closes_4h, adx_period)
    adx      = adx_info["adx"]

    # ── 4. RANGE — low ADX + tight price range ───────────────────────────────
    if adx < adx_range_thr:
        range_window = min(20, len(highs_4h))
        if range_window >= 5:
            rng_high = max(highs_4h[-range_window:])
            rng_low  = min(lows_4h[-range_window:])
            mid      = (rng_high + rng_low) / 2.0
            if mid > 0 and (rng_high - rng_low) / mid <= range_size_max:
                return "RANGE"

    # ── 5. BREAKOUT — ADX trending after a prior tight range ─────────────────
    # Stateless approximation: if the prior 20 bars formed a range AND ADX is
    # now above trend threshold AND price broke outside that range → BREAKOUT.
    if adx >= adx_trend_thr and len(highs_4h) >= 22 and len(lows_4h) >= 22:
        prev_high = max(highs_4h[-21:-1])
        prev_low  = min(lows_4h[-21:-1])
        prev_mid  = (prev_high + prev_low) / 2.0
        if prev_mid > 0:
            prev_range_pct = (prev_high - prev_low) / prev_mid
            if prev_range_pct <= range_size_max:
                price = closes_4h[-1]
                if price > prev_high * 1.003 or price < prev_low * 0.997:
                    return "BREAKOUT"

    return "TREND"


def get_trend_direction(
    closes_4h: list[float],
    ema200_1d: float,
) -> str:
    """Return LONG, SHORT, or NEUTRAL based on DI alignment and EMA200.

    Uses price position relative to EMA200 as the primary direction signal.
    LONG  when price > 2% above EMA200 (confirmed uptrend baseline).
    SHORT when price > 2% below EMA200 (confirmed downtrend baseline).
    NEUTRAL when price is within ±2% of EMA200 (no clear bias).
    """
    if not closes_4h or ema200_1d <= 0:
        return "NEUTRAL"
    price    = closes_4h[-1]
    dist_pct = (price - ema200_1d) / ema200_1d
    if dist_pct > 0.02:
        return "LONG"
    elif dist_pct < -0.02:
        return "SHORT"
    return "NEUTRAL"
