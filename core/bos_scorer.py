"""BOS/CHoCH scorer — entries on confirmed 1H structure breaks.

Strategy logic
--------------
1. Detect a Break of Structure on the 1H chart (close beyond confirmed swing point
   with volume confirmation).
2. Score four signals at different weights:
       bos_confirmed   0.50  — BOS fired (HARD gate — blocks fire if False)
       htf_4h_aligned  0.25  — 4H also showing HH+HL (LONG) or LH+LL (SHORT)
       choch_confirm   0.15  — a CHoCH preceded the BOS (double confirmation)
       volume_spike    0.10  — break bar volume ≥ vol_confirm_mult × 20-bar avg
3. Hard gates (not scored — block fire when they fail):
       bos_confirmed   — must be True
       regime_ok       — CRASH blocks LONG, PUMP blocks SHORT
       not_extended    — price must not be >max_extension_pct beyond break level
4. Fire when score ≥ 0.75 (bos + at least one other signal), RR ≥ 2.5×, not on cooldown.

SL/TP placement (structure-anchored):
    LONG:  SL = prior_swing_low  × (1 − sl_buffer_pct)
           TP = entry + |entry − SL| × rr_ratio
    SHORT: SL = prior_swing_high × (1 + sl_buffer_pct)
           TP = entry − |SL − entry| × rr_ratio

Output dict keys: symbol, regime, direction, score, signals, fire,
                  bos_stop, bos_tp, break_level
"""
import logging
import os
import yaml

from core.cooldown_store import CooldownStore
from signals.trend.bos import detect_swing_points

log = logging.getLogger(__name__)

_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "config.yaml")
with open(_CONFIG_PATH) as _f:
    _cfg = yaml.safe_load(_f)

_BOS_CFG = _cfg.get("bos", {})

_COOLDOWN_SECS    = float(_BOS_CFG.get("cooldown_mins",      60))  * 60.0
_THRESHOLD        = 0.75     # bos(0.50) + one more signal (≥ 0.10) must pass
_SL_BUFFER_PCT    = float(_BOS_CFG.get("sl_buffer_pct",       0.001))
_RR_RATIO         = float(_BOS_CFG.get("rr_ratio",            2.5))
_MAX_EXTENSION    = float(_BOS_CFG.get("max_extension_pct",   0.02))
_PIVOT_N          = int(_BOS_CFG.get("pivot_n",               3))
_LOOKBACK_4H      = 60   # 4H candles for HTF structure check (~10 days)

# Signal weights — must sum to 1.0
_W_BOS    = 0.50
_W_HTF    = 0.25
_W_CHOCH  = 0.15
_W_VOL    = 0.10

_cd = CooldownStore("BOS")


# ── Cooldown helpers ───────────────────────────────────────────────────────────

def is_on_cooldown(symbol: str) -> bool:
    return _cd.is_active(symbol)


def set_cooldown(symbol: str) -> None:
    _cd.set(symbol, _COOLDOWN_SECS)
    log.debug("BOS cooldown set for %s (%.0f min)", symbol, _COOLDOWN_SECS / 60)


def cooldown_remaining(symbol: str) -> float:
    return _cd.remaining(symbol)


# ── HTF structure helper ───────────────────────────────────────────────────────

def _htf_structure_bullish(symbol: str, cache) -> bool:
    """True when 4H is showing HH + HL (confirmed uptrend structure)."""
    bars_4h = cache.get_ohlcv(symbol, window=_LOOKBACK_4H, tf="4h")
    if not bars_4h or len(bars_4h) < _PIVOT_N * 2 + 4:
        return False
    swings = detect_swing_points(bars_4h, _PIVOT_N)
    ph = swings["pivot_highs"]
    pl = swings["pivot_lows"]
    hh = len(ph) >= 2 and ph[-1] > ph[-2]
    hl = len(pl) >= 2 and pl[-1] > pl[-2]
    return hh and hl


def _htf_structure_bearish(symbol: str, cache) -> bool:
    """True when 4H is showing LH + LL (confirmed downtrend structure)."""
    bars_4h = cache.get_ohlcv(symbol, window=_LOOKBACK_4H, tf="4h")
    if not bars_4h or len(bars_4h) < _PIVOT_N * 2 + 4:
        return False
    swings = detect_swing_points(bars_4h, _PIVOT_N)
    ph = swings["pivot_highs"]
    pl = swings["pivot_lows"]
    lh = len(ph) >= 2 and ph[-1] < ph[-2]
    ll = len(pl) >= 2 and pl[-1] < pl[-2]
    return lh and ll


# ── Volume spike helper ────────────────────────────────────────────────────────

def _vol_spike_ok(symbol: str, cache) -> bool:
    """True when the last 1H bar volume ≥ vol_confirm_mult × 20-bar average."""
    bars = cache.get_ohlcv(symbol, window=22, tf="1h")
    if not bars or len(bars) < 22:
        return False
    vol_mult = float(_BOS_CFG.get("vol_confirm_mult", 1.3))
    avg = sum(b["v"] for b in bars[-21:-1]) / 20.0
    return avg > 0.0 and bars[-1]["v"] >= vol_mult * avg


# ── Regime gate ────────────────────────────────────────────────────────────────

def _regime_ok(symbol: str, direction: str, cache) -> bool:
    """False when macro regime fundamentally opposes the BOS direction."""
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
    """Score *symbol* for BOS/CHoCH setups on 1H.

    Returns a list of score dicts (up to one LONG + one SHORT candidate).
    Each dict follows the standard format:
        symbol, regime, direction, score, signals, fire
    Plus BOS-specific keys:
        bos_stop, bos_tp   — structure-anchored SL/TP for executor
        break_level        — the swing point that was broken (for reference)
    """
    from signals.trend.bos import (
        check_bos_bullish,
        check_bos_bearish,
        check_choch_bullish,
        check_choch_bearish,
    )

    cool_ok = not is_on_cooldown(symbol)

    # Current price for extension check and SL/TP calc
    closes_1m = cache.get_closes(symbol, window=1, tf="1m")
    price = closes_1m[-1] if closes_1m else 0.0
    if price == 0.0:
        bars_1h = cache.get_ohlcv(symbol, window=1, tf="1h")
        price = bars_1h[-1]["c"] if bars_1h else 0.0
    if price == 0.0:
        return []

    results: list[dict] = []

    # ── LONG: bullish BOS ─────────────────────────────────────────────────────
    bos_bull = check_bos_bullish(symbol, cache)
    if bos_bull is not None:
        bos_fired    = bos_bull["fired"]
        break_level  = bos_bull["break_level"]
        sl_anchor    = bos_bull["prior_swing_low"]

        # Hard gate: BOS must have fired and SL anchor must be valid
        if bos_fired and break_level > 0.0 and sl_anchor > 0.0:
            htf_ok    = _htf_structure_bullish(symbol, cache)
            choch_ok  = check_choch_bullish(symbol, cache)
            vol_ok    = _vol_spike_ok(symbol, cache)
            regime_ok = _regime_ok(symbol, "LONG", cache)

            # Not-extended gate: price must be within max_extension_pct of break
            extension = (price - break_level) / break_level if break_level > 0 else 1.0
            not_extended = extension <= _MAX_EXTENSION

            signals = {
                "bos_confirmed":  True,     # True by definition here
                "htf_4h_aligned": htf_ok,
                "choch_confirm":  choch_ok,
                "volume_spike":   vol_ok,
            }
            score_val = round(
                _W_BOS * signals["bos_confirmed"]
                + _W_HTF  * signals["htf_4h_aligned"]
                + _W_CHOCH * signals["choch_confirm"]
                + _W_VOL  * signals["volume_spike"],
                4,
            )

            sl = sl_anchor * (1.0 - _SL_BUFFER_PCT)
            dist = abs(price - sl)
            tp   = price + dist * _RR_RATIO

            fire = (
                score_val >= _THRESHOLD
                and regime_ok
                and not_extended
                and cool_ok
            )

            results.append({
                "symbol":      symbol,
                "regime":      "BOS",
                "direction":   "LONG",
                "score":       score_val,
                "signals":     {**signals,
                                "regime_ok":    regime_ok,
                                "not_extended": not_extended},
                "fire":        fire,
                "bos_stop":    round(sl, 8),
                "bos_tp":      round(tp, 8),
                "break_level": round(break_level, 8),
            })

    # ── SHORT: bearish BOS ────────────────────────────────────────────────────
    bos_bear = check_bos_bearish(symbol, cache)
    if bos_bear is not None:
        bos_fired    = bos_bear["fired"]
        break_level  = bos_bear["break_level"]
        sl_anchor    = bos_bear["prior_swing_high"]

        if bos_fired and break_level > 0.0 and sl_anchor > 0.0:
            htf_ok    = _htf_structure_bearish(symbol, cache)
            choch_ok  = check_choch_bearish(symbol, cache)
            vol_ok    = _vol_spike_ok(symbol, cache)
            regime_ok = _regime_ok(symbol, "SHORT", cache)

            extension = (break_level - price) / break_level if break_level > 0 else 1.0
            not_extended = extension <= _MAX_EXTENSION

            signals = {
                "bos_confirmed":  True,
                "htf_4h_aligned": htf_ok,
                "choch_confirm":  choch_ok,
                "volume_spike":   vol_ok,
            }
            score_val = round(
                _W_BOS * signals["bos_confirmed"]
                + _W_HTF  * signals["htf_4h_aligned"]
                + _W_CHOCH * signals["choch_confirm"]
                + _W_VOL  * signals["volume_spike"],
                4,
            )

            sl = sl_anchor * (1.0 + _SL_BUFFER_PCT)
            dist = abs(sl - price)
            tp   = price - dist * _RR_RATIO

            fire = (
                score_val >= _THRESHOLD
                and regime_ok
                and not_extended
                and cool_ok
            )

            results.append({
                "symbol":      symbol,
                "regime":      "BOS",
                "direction":   "SHORT",
                "score":       score_val,
                "signals":     {**signals,
                                "regime_ok":    regime_ok,
                                "not_extended": not_extended},
                "fire":        fire,
                "bos_stop":    round(sl, 8),
                "bos_tp":      round(tp, 8),
                "break_level": round(break_level, 8),
            })

    return results
