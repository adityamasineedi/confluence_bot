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

log = logging.getLogger(__name__)

_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "config.yaml")
with open(_CONFIG_PATH) as _f:
    _cfg = yaml.safe_load(_f)

_FVG_CFG = _cfg.get("fvg", {})

_COOLDOWN_SECS  = float(_FVG_CFG.get("cooldown_mins",   45))   * 60.0
_THRESHOLD      = 0.67          # 2 of 3 scored signals required
_SL_BUFFER_PCT  = float(_FVG_CFG.get("sl_buffer_pct",   0.002))
_RR_RATIO       = float(_FVG_CFG.get("rr_ratio",        2.0))
_LOOKBACK       = int(_FVG_CFG.get("lookback_bars",      50))
_RSI_LONG_MAX   = 45.0          # RSI ≤ 45 for LONG (oversold pullback into gap)
_RSI_SHORT_MIN  = 55.0          # RSI ≥ 55 for SHORT (overbought fill from above)
_RSI_PERIOD     = 14
_EMA_PERIOD     = 21            # 4H EMA period for HTF alignment

_cd = CooldownStore("FVG")


# ── Cooldown helpers ───────────────────────────────────────────────────────────

def is_on_cooldown(symbol: str) -> bool:
    return _cd.is_active(symbol)


def set_cooldown(symbol: str) -> None:
    _cd.set(symbol, _COOLDOWN_SECS)
    log.debug("FVG cooldown set for %s (%.0f min)", symbol, _COOLDOWN_SECS / 60)


def cooldown_remaining(symbol: str) -> float:
    return _cd.remaining(symbol)


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

    bars_5m = cache.get_ohlcv(symbol, window=25, tf="5m") or []

    from signals.volume_momentum import (
        volume_spike,
        volume_divergence_bearish,
        volume_divergence_bullish,
    )

    # ── LONG: bullish FVG ─────────────────────────────────────────────────────
    fvg_bull = check_fvg_bullish(symbol, cache)
    if fvg_bull:
        levels = get_fvg_levels(symbol, cache, "LONG")
        if levels is not None:
            gap_low, gap_high = levels

            htf_aligned = ema21_4h > 0.0 and price > ema21_4h
            rsi_ok      = rsi_val <= _RSI_LONG_MAX
            regime_gate = _regime_ok(symbol, "LONG", cache)

            vol_confirms    = volume_spike(bars_5m, lookback=20, mult=1.3)
            vol_not_dist    = not volume_divergence_bearish(bars_5m, lookback=5)

            signals = {
                "fvg_detected":  True,           # always True here (hard gate confirmed above)
                "htf_aligned":   htf_aligned,
                "rsi_confirm":   rsi_ok,
                "vol_confirm":   vol_confirms,
                "vol_not_dist":  vol_not_dist,
            }
            score_val = round(sum(1.0 / 3.0 for v in (
                signals["fvg_detected"],
                signals["htf_aligned"],
                signals["rsi_confirm"],
            ) if v), 4)

            sl   = gap_low * (1.0 - _SL_BUFFER_PCT)
            dist = abs(price - sl)
            tp   = price + dist * _RR_RATIO

            fire = (score_val >= _THRESHOLD
                    and regime_gate
                    and vol_confirms
                    and vol_not_dist
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

            htf_aligned = ema21_4h > 0.0 and price < ema21_4h
            rsi_ok      = rsi_val >= _RSI_SHORT_MIN
            regime_gate = _regime_ok(symbol, "SHORT", cache)

            vol_confirms    = volume_spike(bars_5m, lookback=20, mult=1.3)
            vol_not_accum   = not volume_divergence_bullish(bars_5m, lookback=5)

            signals = {
                "fvg_detected":   True,
                "htf_aligned":    htf_aligned,
                "rsi_confirm":    rsi_ok,
                "vol_confirm":    vol_confirms,
                "vol_not_accum":  vol_not_accum,
            }
            score_val = round(sum(1.0 / 3.0 for v in (
                signals["fvg_detected"],
                signals["htf_aligned"],
                signals["rsi_confirm"],
            ) if v), 4)

            sl   = gap_high * (1.0 + _SL_BUFFER_PCT)
            dist = abs(sl - price)
            tp   = price - dist * _RR_RATIO

            fire = (score_val >= _THRESHOLD
                    and regime_gate
                    and vol_confirms
                    and vol_not_accum
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
