"""FVG Fill scorer — entries into unfilled 1H Fair Value Gap zones.

Strategy logic
--------------
1. Detect a virgin (never-entered) unfilled bullish or bearish FVG on the 1H chart.
2. Score three equally-weighted signals (0.33 each, max 1.0):
       fvg_detected  — price currently inside a virgin unfilled gap (HARD gate)
       htf_aligned   — 4H close above EMA21 for LONG / below for SHORT
       rsi_confirm   — RSI(14) ≤ 45 for LONG (oversold into gap), ≥ 55 for SHORT
3. Hard gates (block fire, not scored):
       fvg_detected  — must be True; also blocks when get_fvg_levels() returns None
       regime_ok     — CRASH blocks LONG, PUMP blocks SHORT
4. Fire when score ≥ 0.67 (2 of 3 signals), RR ≥ 2.0×, not on cooldown.

SL/TP placement:
    LONG:  SL = gap_low  × (1 − sl_buffer_pct)
           TP = entry + |entry − SL| × rr_ratio
    SHORT: SL = gap_high × (1 + sl_buffer_pct)
           TP = entry − |SL − entry| × rr_ratio

Output dict keys (in addition to the standard set):
    fvg_stop, fvg_tp  — executor reads these via the "FVG" preset-levels key
"""
import logging
import os
import yaml

from core.cooldown_store import CooldownStore
from core.filter import passes_trend_long_filters, passes_trend_short_filters, atr_spike_ok
from core.vol_ratio import compute_vol_ratio

log = logging.getLogger(__name__)

_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "config.yaml")
with open(_CONFIG_PATH) as _f:
    _cfg = yaml.safe_load(_f)

_FVG_CFG = _cfg.get("fvg", {})

_COOLDOWN_SECS  = float(_FVG_CFG.get("cooldown_mins",   45))   * 60.0
_THRESHOLD      = 0.67          # fvg(0.30) + htf(0.25) + rsi(0.20) = 0.75 >= 0.67 → fires
                                # irb_confirm(0.10) raises ceiling; vol_confirm/rvol_ok reduced to compensate

_SIGNAL_WEIGHTS = {
    "fvg_detected": 0.30,   # hard gate — primary signal
    "htf_aligned":  0.25,   # HTF direction alignment
    "rsi_confirm":  0.20,   # RSI zone
    "vol_confirm":  0.05,   # reduced from 0.10 to make room for irb_confirm
    "vol_not_dist": 0.10,   # no distribution/accumulation pattern
    "irb_confirm":  0.10,   # IRB: 2-bar pullback + close in top/bottom 25% of range
    "oi_div_long":  0.10,   # OI rising while price falls = squeeze fuel
    "oi_div_short": 0.10,   # OI falling while price rises = fake breakout
    # rvol_ok removed from weights (kept in signals for logging) — freed 0.05 for irb_confirm
}
_SL_BUFFER_PCT  = float(_FVG_CFG.get("sl_buffer_pct",   0.002))
_RR_RATIO       = float(_FVG_CFG.get("rr_ratio",        2.0))
_LOOKBACK       = int(_FVG_CFG.get("lookback_bars",      50))
_RSI_LONG_MAX   = 45.0          # RSI ≤ 45 for LONG (oversold pullback into gap)
_RSI_SHORT_MIN  = 55.0          # RSI ≥ 55 for SHORT (overbought fill from above)
_RSI_PERIOD     = 14
_EMA_PERIOD     = 21            # 4H EMA period for HTF alignment

_MC_VOL_MAX     = float(_FVG_CFG.get("mc_vol_ratio_max", 0.0))

_cd = CooldownStore("FVG")


# ── Cooldown helpers ───────────────────────────────────────────────────────────

def is_on_cooldown(symbol: str) -> bool:
    return _cd.is_active(symbol)


def set_cooldown(symbol: str) -> None:
    _cd.set(symbol, _COOLDOWN_SECS)
    log.debug("FVG cooldown set for %s (%.0f min)", symbol, _COOLDOWN_SECS / 60)


def cooldown_remaining(symbol: str) -> float:
    return _cd.remaining(symbol)


def clear_cooldown(symbol: str) -> None:
    """Clear scorer cooldown for symbol — used by backtest engines."""
    _cd.clear(symbol)


# ── Math helpers ───────────────────────────────────────────────────────────────

def _ema(closes: list[float], period: int) -> float:
    """Exponential moving average over closes list.  Returns 0.0 on insufficient data."""
    if len(closes) < period:
        return 0.0
    k   = 2.0 / (period + 1)
    val = sum(closes[:period]) / period
    for c in closes[period:]:
        val = c * k + val * (1.0 - k)
    return val


def _rsi(closes: list[float], period: int = _RSI_PERIOD) -> float:
    """Wilder RSI.  Returns 50.0 when insufficient data."""
    if len(closes) < period + 1:
        return 50.0
    gains, losses = [], []
    for i in range(1, len(closes)):
        chg = closes[i] - closes[i - 1]
        gains.append(max(chg, 0.0))
        losses.append(max(-chg, 0.0))
    avg_g = sum(gains[:period]) / period
    avg_l = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_g = (avg_g * (period - 1) + gains[i]) / period
        avg_l = (avg_l * (period - 1) + losses[i]) / period
    if avg_l == 0.0:
        return 100.0
    return 100.0 - (100.0 / (1.0 + avg_g / avg_l))


def _regime_ok(symbol: str, direction: str, cache) -> bool:
    """False when macro regime fundamentally contradicts the trade direction."""
    try:
        from core.regime_detector import detect_regime
        regime = str(detect_regime(symbol, cache))
        if regime == "CRASH" and direction == "LONG":
            return False
        if regime == "PUMP" and direction == "SHORT":
            return False
    except Exception:
        pass
    return True


# ── Scorer ────────────────────────────────────────────────────────────────────

async def score(symbol: str, cache) -> list[dict]:
    """Score *symbol* for FVG fill setups.

    Returns a list of score dicts (LONG and/or SHORT candidates).
    Each dict follows the standard format:
        symbol, regime, direction, score, signals, fire
    Plus FVG-specific keys:
        fvg_stop, fvg_tp  — gap-anchored SL/TP for executor
    """
    from signals.trend.fvg import check_fvg_bullish, check_fvg_bearish, get_fvg_levels
    from signals.trend.irb import check_irb_long, check_irb_short
    from signals.trend.oi_divergence import check_oi_divergence_long, check_oi_divergence_short
    from signals.volume_momentum import extreme_volatility

    # Extreme volatility gate — flat during flash crashes (gaps past SL, huge slippage)
    _ev_cfg = _cfg.get("risk", {}).get("extreme_vol_gate", {})
    if _ev_cfg.get("enabled", True):
        bars_4h_ev = cache.get_ohlcv(symbol, window=55, tf="4h")
        if extreme_volatility(bars_4h_ev,
                              lookback=int(_ev_cfg.get("lookback_bars", 50)),
                              spike_mult=float(_ev_cfg.get("spike_multiplier", 3.0))):
            log.info("FVG blocked — extreme volatility on %s (flash crash protection)", symbol)
            return []

    # Vol ratio gate — block during vol spikes beyond threshold
    if _MC_VOL_MAX > 0:
        bars_vr = cache.get_ohlcv(symbol, window=60, tf="1h")
        if bars_vr and len(bars_vr) >= 54:
            vr = compute_vol_ratio(bars_vr)
            if vr > _MC_VOL_MAX:
                log.debug("FVG blocked — vol ratio %.2f > %.1f on %s",
                          vr, _MC_VOL_MAX, symbol)
                return []

    cool_ok   = not is_on_cooldown(symbol)

    bars_1h = cache.get_ohlcv(symbol, window=_LOOKBACK + 5, tf="1h")
    bars_4h = cache.get_ohlcv(symbol, window=_EMA_PERIOD + 5, tf="4h")

    if not bars_1h or len(bars_1h) < 10:
        return []

    closes_1h = [b["c"] for b in bars_1h]
    rsi_val   = _rsi(closes_1h)
    price     = closes_1h[-1]

    closes_4h = [b["c"] for b in bars_4h] if bars_4h else []
    ema21_4h  = _ema(closes_4h, _EMA_PERIOD)

    results: list[dict] = []

    from signals.volume_momentum import VolumeContext, get_volume_params

    # Dynamic volume params — FVG fills are 1H signals; use 1H bars for volume checks
    vol_ctx    = VolumeContext(symbol=symbol, regime="FVG", timeframe="1h", cache=cache)
    vol_params = get_volume_params(vol_ctx)

    # Hard gate: large recent liquidation cascade → spike threshold raised;
    # liq_spike_mult > spike_mult means a liq event was detected.
    liq_vol_ok = vol_params.liq_spike_mult == vol_params.spike_mult

    # ── LONG: bullish FVG ─────────────────────────────────────────────────────
    fvg_bull = check_fvg_bullish(symbol, cache)
    if fvg_bull:
        levels = get_fvg_levels(symbol, cache, "LONG")
        if levels is not None:
            gap_low, gap_high = levels

            htf_aligned  = ema21_4h > 0.0 and price > ema21_4h
            rsi_ok       = rsi_val <= _RSI_LONG_MAX
            regime_gate  = _regime_ok(symbol, "LONG", cache)

            # Dynamic volume signals on 1H bars (gap fills should show real volume)
            vol_confirms = vol_params.spike(bars_1h)
            vol_not_dist = not vol_params.bearish_divergence(bars_1h)
            rvol_ok      = vol_params.rvol_ok(bars_1h)

            signals = {
                "fvg_detected": True,          # always True here (hard gate confirmed)
                "htf_aligned":  htf_aligned,
                "rsi_confirm":  rsi_ok,
                "vol_confirm":  vol_confirms,
                "vol_not_dist": vol_not_dist,
                "rvol_ok":      rvol_ok,       # informational only — weight removed
                "irb_confirm":  check_irb_long(symbol, cache),
                "oi_div_long":  check_oi_divergence_long(symbol, cache),
            }
            score_val = round(sum(
                _SIGNAL_WEIGHTS.get(k, 0.0) for k, v in signals.items() if v
            ), 4)

            at_key_level = cache.near_key_level(symbol, price, 0.003)
            signals["at_key_level"] = at_key_level
            if at_key_level:
                score_val = min(score_val + 0.15, 1.0)

            sl   = gap_low * (1.0 - _SL_BUFFER_PCT)
            dist = abs(price - sl)
            tp   = price + dist * _RR_RATIO

            from core.weekly_trend_gate import weekly_allows_long
            trend_long_ok = passes_trend_long_filters(symbol, cache)
            spike_ok      = atr_spike_ok(symbol, cache)
            weekly_ok     = weekly_allows_long("fvg", cache)
            signals["trend_long_filter"] = trend_long_ok
            signals["atr_spike_ok"]      = spike_ok
            signals["weekly_gate_ok"]    = weekly_ok
            fire = (score_val >= _THRESHOLD
                    and regime_gate
                    and trend_long_ok          # DI edge + ADX rising + BTC macro
                    and spike_ok
                    and weekly_ok
                    and liq_vol_ok
                    and cool_ok)

            results.append({
                "symbol":    symbol,
                "regime":    "FVG",
                "direction": "LONG",
                "score":     score_val,
                "signals":   signals,
                "fire":      fire,
                "fvg_stop":  round(sl, 8),
                "fvg_tp":    round(tp, 8),
            })

    # ── SHORT: bearish FVG ────────────────────────────────────────────────────
    fvg_bear = check_fvg_bearish(symbol, cache)
    if fvg_bear:
        levels = get_fvg_levels(symbol, cache, "SHORT")
        if levels is not None:
            gap_low, gap_high = levels

            htf_aligned  = ema21_4h > 0.0 and price < ema21_4h
            rsi_ok       = rsi_val >= _RSI_SHORT_MIN
            regime_gate  = _regime_ok(symbol, "SHORT", cache)

            vol_confirms  = vol_params.spike(bars_1h)
            vol_not_accum = not vol_params.bullish_divergence(bars_1h)
            rvol_ok       = vol_params.rvol_ok(bars_1h)

            signals = {
                "fvg_detected":  True,
                "htf_aligned":   htf_aligned,
                "rsi_confirm":   rsi_ok,
                "vol_confirm":   vol_confirms,
                "vol_not_dist":  vol_not_accum,   # reuse shared weight key
                "rvol_ok":       rvol_ok,          # informational only — weight removed
                "irb_confirm":   check_irb_short(symbol, cache),
                "oi_div_short":  check_oi_divergence_short(symbol, cache),
            }
            score_val = round(sum(
                _SIGNAL_WEIGHTS.get(k, 0.0) for k, v in signals.items() if v
            ), 4)

            at_key_level = cache.near_key_level(symbol, price, 0.003)
            signals["at_key_level"] = at_key_level
            if at_key_level:
                score_val = min(score_val + 0.15, 1.0)

            sl   = gap_high * (1.0 + _SL_BUFFER_PCT)
            dist = abs(sl - price)
            tp   = price - dist * _RR_RATIO

            from core.weekly_trend_gate import weekly_allows_short
            trend_short_ok = passes_trend_short_filters(symbol, cache)
            spike_ok_s     = atr_spike_ok(symbol, cache)
            weekly_ok      = weekly_allows_short("fvg", cache)
            signals["trend_short_filter"] = trend_short_ok
            signals["atr_spike_ok"]       = spike_ok_s
            signals["weekly_gate_ok"]     = weekly_ok
            fire = (score_val >= _THRESHOLD
                    and regime_gate
                    and trend_short_ok         # BTC below EMA200 + -DI > +DI
                    and spike_ok_s
                    and weekly_ok
                    and liq_vol_ok
                    and cool_ok)

            results.append({
                "symbol":    symbol,
                "regime":    "FVG",
                "direction": "SHORT",
                "score":     score_val,
                "signals":   signals,
                "fire":      fire,
                "fvg_stop":  round(sl, 8),
                "fvg_tp":    round(tp, 8),
            })

    return results
