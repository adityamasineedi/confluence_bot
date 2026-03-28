"""VWAP Band Reversion scorer — mean-reversion entries at ±2σ VWAP bands on 15m.

Strategy logic
--------------
1. Price touches the ±2σ band and closes back inside on 15m (band rejection).
2. Score three equally-weighted signals (0.33 each, max 1.0):
       band_touch      — price touched ±2σ and closed back inside (HARD gate)
       rsi_confirm     — RSI(14) ≤ 35 (LONG) or ≥ 65 (SHORT) on 15m
       regime_aligned  — regime is RANGE or TREND (mean reversion works in both)
3. Hard gates (block fire, not scored):
       band_touch  — must be True (checked via check_vwap_long/short)
       regime_ok   — CRASH blocks LONG, PUMP blocks SHORT
       not_trending — 4H ADX < adx_max (blocks entries in strong trends where
                      bands keep expanding and reversion fails systematically)
4. Fire when score ≥ 0.67 (2 of 3), RR ≥ 1.5× (VWAP must be ≥ 1.5× SL dist), cooldown clear.

SL/TP (VWAP-anchored):
    LONG:  SL = lower_2_band × (1 − sl_buffer_pct)
           TP = vwap_mid   (the mean-reversion target)
    SHORT: SL = upper_2_band × (1 + sl_buffer_pct)
           TP = vwap_mid
    Min RR: |vwap - entry| / |entry - SL| ≥ 1.5 — if VWAP is too close, skip.

Output dict keys: symbol, regime, direction, score, signals, fire, vb_stop, vb_tp
"""
import logging
import os
import yaml

from core.cooldown_store import CooldownStore

log = logging.getLogger(__name__)

_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "config.yaml")
with open(_CONFIG_PATH) as _f:
    _cfg = yaml.safe_load(_f)

_VB_CFG = _cfg.get("vwap_band", {})

_COOLDOWN_SECS = float(_VB_CFG.get("cooldown_mins",  30)) * 60.0
_THRESHOLD     = 0.67   # 2 of 3 scored signals required
_SL_BUFFER_PCT = float(_VB_CFG.get("sl_buffer_pct",  0.002))
_MIN_RR        = 1.5    # VWAP must be at least 1.5× SL distance away
_ADX_MAX       = float(_VB_CFG.get("adx_max",        30.0))

_cd = CooldownStore("VWAPBAND")

# Regimes where mean reversion at VWAP bands works reliably
_GOOD_REGIMES = frozenset({"RANGE", "TREND"})


# ── Cooldown helpers ───────────────────────────────────────────────────────────

def is_on_cooldown(symbol: str) -> bool:
    return _cd.is_active(symbol)


def set_cooldown(symbol: str) -> None:
    _cd.set(symbol, _COOLDOWN_SECS)
    log.debug("VWAP Band cooldown set for %s (%.0f min)", symbol, _COOLDOWN_SECS / 60)


def cooldown_remaining(symbol: str) -> float:
    return _cd.remaining(symbol)


# ── Gate helpers ───────────────────────────────────────────────────────────────

def _regime_str(symbol: str, cache) -> str:
    try:
        from core.regime_detector import detect_regime
        return str(detect_regime(symbol, cache))
    except Exception:
        return ""


def _regime_ok(regime: str, direction: str) -> bool:
    """False when macro regime fundamentally opposes the trade direction."""
    if regime == "CRASH" and direction == "LONG":
        return False
    if regime == "PUMP" and direction == "SHORT":
        return False
    return True


def _adx_below_max(symbol: str, cache) -> bool:
    """True when 4H ADX < adx_max — confirms we are NOT in a strong trend.

    In strong trends (ADX > 30), the VWAP bands keep expanding outward and price
    rarely reverts — fading band extensions loses money systematically.
    Falls back to True (allow trade) when ADX data is unavailable.
    """
    try:
        from core.regime_detector import get_adx_info
        info = get_adx_info(symbol, cache, tf="4h")
        adx  = info.get("adx", 0.0)
        if adx == 0.0:
            return True   # no data — conservative: allow trade
        return adx < _ADX_MAX
    except Exception:
        return True


# ── Scorer ────────────────────────────────────────────────────────────────────

async def score(symbol: str, cache) -> list[dict]:
    """Score *symbol* for VWAP band reversion setups on 15m.

    Returns a list of score dicts (up to one LONG + one SHORT candidate).
    Standard keys: symbol, regime, direction, score, signals, fire
    Strategy keys: vb_stop, vb_tp  (executor reads via VWAPBAND preset-levels)
    """
    from signals.range.vwap_bands import (
        check_vwap_long,
        check_vwap_short,
        get_vwap_levels,
    )

    cool_ok    = not is_on_cooldown(symbol)
    regime     = _regime_str(symbol, cache)
    adx_ok     = _adx_below_max(symbol, cache)

    # Current price for RR calculation
    closes_1m = cache.get_closes(symbol, window=1, tf="1m")
    price = closes_1m[-1] if closes_1m else 0.0
    if price == 0.0:
        bars_15m = cache.get_ohlcv(symbol, window=1, tf="15m")
        price = bars_15m[-1]["c"] if bars_15m else 0.0
    if price == 0.0:
        return []

    results: list[dict] = []

    # ── LONG: lower_2 band touch + rejection ─────────────────────────────────
    band_long = check_vwap_long(symbol, cache)
    if band_long:
        levels = get_vwap_levels(symbol, cache, "LONG")
        if levels is not None:
            vwap_mid, lower_2, std_dev = levels

            regime_gate  = _regime_ok(regime, "LONG")
            regime_align = regime in _GOOD_REGIMES

            # RSI already baked into check_vwap_long, surface it as named signal
            # Re-derive RSI value for the signals dict
            bars_15m  = cache.get_ohlcv(symbol, window=35, tf="15m")
            closes_15m = [b["c"] for b in bars_15m] if bars_15m else []
            rsi_val   = _rsi_from_closes(closes_15m)
            rsi_ok    = rsi_val <= float(_VB_CFG.get("rsi_long_max", 35.0))

            signals = {
                "band_touch":     True,         # always True here (hard gate confirmed)
                "rsi_confirm":    rsi_ok,
                "regime_aligned": regime_align,
            }
            score_val = round(sum(1.0 / 3.0 for v in signals.values() if v), 4)

            sl   = lower_2 * (1.0 - _SL_BUFFER_PCT)
            tp   = vwap_mid
            dist = abs(price - sl)
            rr   = abs(tp - price) / dist if dist > 0 else 0.0

            fire = (
                score_val >= _THRESHOLD
                and regime_gate
                and adx_ok
                and rr >= _MIN_RR
                and cool_ok
            )

            results.append({
                "symbol":    symbol,
                "regime":    "VWAPBAND",
                "direction": "LONG",
                "score":     score_val,
                "signals":   {**signals,
                              "regime_ok":   regime_gate,
                              "not_trending": adx_ok},
                "fire":      fire,
                "vb_stop":   round(sl, 8),
                "vb_tp":     round(tp, 8),
            })

    # ── SHORT: upper_2 band touch + rejection ─────────────────────────────────
    band_short = check_vwap_short(symbol, cache)
    if band_short:
        levels = get_vwap_levels(symbol, cache, "SHORT")
        if levels is not None:
            vwap_mid, upper_2, std_dev = levels

            regime_gate  = _regime_ok(regime, "SHORT")
            regime_align = regime in _GOOD_REGIMES

            bars_15m   = cache.get_ohlcv(symbol, window=35, tf="15m")
            closes_15m = [b["c"] for b in bars_15m] if bars_15m else []
            rsi_val    = _rsi_from_closes(closes_15m)
            rsi_ok     = rsi_val >= float(_VB_CFG.get("rsi_short_min", 65.0))

            signals = {
                "band_touch":     True,
                "rsi_confirm":    rsi_ok,
                "regime_aligned": regime_align,
            }
            score_val = round(sum(1.0 / 3.0 for v in signals.values() if v), 4)

            sl   = upper_2 * (1.0 + _SL_BUFFER_PCT)
            tp   = vwap_mid
            dist = abs(sl - price)
            rr   = abs(price - tp) / dist if dist > 0 else 0.0

            fire = (
                score_val >= _THRESHOLD
                and regime_gate
                and adx_ok
                and rr >= _MIN_RR
                and cool_ok
            )

            results.append({
                "symbol":    symbol,
                "regime":    "VWAPBAND",
                "direction": "SHORT",
                "score":     score_val,
                "signals":   {**signals,
                              "regime_ok":    regime_gate,
                              "not_trending": adx_ok},
                "fire":      fire,
                "vb_stop":   round(sl, 8),
                "vb_tp":     round(tp, 8),
            })

    return results


# ── RSI helper (scorer-local, avoids re-importing signal module) ──────────────

def _rsi_from_closes(closes: list[float], period: int = 14) -> float:
    """Wilder RSI from closes list.  Returns 50.0 on insufficient data."""
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
