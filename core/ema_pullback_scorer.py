"""15m EMA Pullback scorer — trend-continuation entries at EMA21.

Score components (each 0.25):
    htf_aligned     — 4H macro direction matches trade direction
    ema_structure   — 15m EMA21 > EMA50 (LONG) or < EMA50 (SHORT)
    pullback_touch  — price touched EMA21 on prior bar (the actual pullback signal)
    bounce_confirm  — current 15m candle closed above/below EMA21 with body ≥ 0.1%

Threshold: 0.75 (need 3 of 4). pullback_touch and htf_aligned are the most
critical — if both are True the entry has a minimum valid setup.
"""
import logging
import os
import yaml

from core.cooldown_store import CooldownStore

log = logging.getLogger(__name__)

_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "config.yaml")
with open(_CONFIG_PATH) as _f:
    _cfg = yaml.safe_load(_f)

_EP_CFG = _cfg.get("ema_pullback", {})

_COOLDOWN_SECS = float(_EP_CFG.get("cooldown_mins", 45)) * 60.0
_THRESHOLD     = float(_EP_CFG.get("fire_threshold", 0.75))

_cd = CooldownStore("EMA_PULLBACK")

_EMA_FAST   = 21
_EMA_SLOW   = 50
_TOUCH_PCT  = 0.004
_MIN_BODY_PCT = 0.001   # candle body must be ≥ 0.1% to confirm direction


def is_on_cooldown(symbol: str) -> bool:
    return _cd.is_active(symbol)


def set_cooldown(symbol: str) -> None:
    _cd.set(symbol, _COOLDOWN_SECS)


def _ema(closes: list[float], period: int) -> float:
    if len(closes) < period:
        return 0.0
    k   = 2.0 / (period + 1)
    ema = sum(closes[:period]) / period
    for c in closes[period:]:
        ema = c * k + ema * (1 - k)
    return ema


async def score(symbol: str, cache) -> list[dict]:
    """Score symbol for 15m EMA pullback setups."""
    from signals.trend.ema_pullback_15m import (
        check_ema15m_pullback_long,
        check_ema15m_pullback_short,
        get_ema15m_long_levels,
        get_ema15m_short_levels,
    )

    results  = []
    cool_ok  = not is_on_cooldown(symbol)

    bars_15m = cache.get_ohlcv(symbol, window=_EMA_SLOW + 10, tf="15m")
    bars_4h  = cache.get_ohlcv(symbol, window=_EMA_SLOW + 5,  tf="4h")

    if len(bars_15m) < _EMA_SLOW + 2:
        return []

    closes_15m = [b["c"] for b in bars_15m]
    ema21_15m  = _ema(closes_15m, _EMA_FAST)
    ema50_15m  = _ema(closes_15m, _EMA_SLOW)

    # ── LONG ─────────────────────────────────────────────────────────────────
    pullback_long = check_ema15m_pullback_long(symbol, cache)
    if pullback_long:
        htf_aligned    = True   # already checked inside check_ema15m_pullback_long
        ema_structure  = ema21_15m > ema50_15m if ema21_15m > 0 else False

        # Bounce confirmation: current candle bullish with meaningful body
        last_bar = bars_15m[-1]
        body_pct = (last_bar["c"] - last_bar["o"]) / last_bar["o"] if last_bar["o"] > 0 else 0
        bounce_ok = last_bar["c"] > last_bar["o"] and body_pct >= _MIN_BODY_PCT

        signals = {
            "htf_aligned":    htf_aligned,
            "ema_structure":  ema_structure,
            "pullback_touch": True,   # True by definition (pullback_long passed)
            "bounce_confirm": bounce_ok,
        }
        score_val = sum(0.25 for v in signals.values() if v)
        entry, stop, tp = get_ema15m_long_levels(symbol, cache)
        fire = score_val >= _THRESHOLD and cool_ok and entry > 0

        results.append({
            "symbol":    symbol,
            "regime":    "EMA_PULLBACK",
            "direction": "LONG",
            "score":     round(score_val, 4),
            "signals":   signals,
            "fire":      fire,
            "ep_stop":   stop,
            "ep_tp":     tp,
        })

    # ── SHORT ─────────────────────────────────────────────────────────────────
    pullback_short = check_ema15m_pullback_short(symbol, cache)
    if pullback_short:
        htf_aligned   = True
        ema_structure = ema21_15m < ema50_15m if ema21_15m > 0 else False

        last_bar = bars_15m[-1]
        body_pct = (last_bar["o"] - last_bar["c"]) / last_bar["o"] if last_bar["o"] > 0 else 0
        bounce_ok = last_bar["c"] < last_bar["o"] and body_pct >= _MIN_BODY_PCT

        signals = {
            "htf_aligned":    htf_aligned,
            "ema_structure":  ema_structure,
            "pullback_touch": True,
            "bounce_confirm": bounce_ok,
        }
        score_val = sum(0.25 for v in signals.values() if v)
        entry, stop, tp = get_ema15m_short_levels(symbol, cache)
        fire = score_val >= _THRESHOLD and cool_ok and entry > 0

        results.append({
            "symbol":    symbol,
            "regime":    "EMA_PULLBACK",
            "direction": "SHORT",
            "score":     round(score_val, 4),
            "signals":   signals,
            "fire":      fire,
            "ep_stop":   stop,
            "ep_tp":     tp,
        })

    return results
