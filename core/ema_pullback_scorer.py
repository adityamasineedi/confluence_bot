"""15m EMA Pullback scorer — trend-continuation entries at EMA21.

Score components (each 0.25):
    htf_aligned     — 4H macro direction matches trade direction
    ema_structure   — 15m EMA21 > EMA50 (LONG) or < EMA50 (SHORT)
    pullback_touch  — price touched EMA21 on prior bar (the actual pullback signal)

Hard gates (not scored — block fire if they fail):
    bounce_confirm  — close ≥ 0.2% above/below EMA21 AND candle body ≥ 0.2%
    vol_confirm     — bounce bar volume > pullback bar volume (buyers/sellers stepping in)

Threshold: 0.75. pullback_touch is always True when scorer runs → htf_aligned +
ema_structure must both pass (0.25 + 0.25 + 0.25 = 0.75 minimum).
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

_EMA_FAST        = 21
_EMA_SLOW        = 50
_TOUCH_PCT       = float(_EP_CFG.get("pullback_touch_pct",   0.002))  # within 0.2% of EMA21
_MIN_BODY_PCT    = float(_EP_CFG.get("min_bounce_body_pct",  0.002))  # body ≥ 0.2%
_MIN_EMA_DIST    = float(_EP_CFG.get("min_ema_dist_pct",     0.002))  # close ≥ 0.2% from EMA21


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
        _htf_bullish,
        _htf_bearish,
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
        htf_aligned    = _htf_bullish(symbol, cache)
        ema_structure  = ema21_15m > ema50_15m if ema21_15m > 0 else False

        # Hard gate 1: bounce candle body ≥ 0.2% AND close ≥ 0.2% above EMA21
        last_bar = bars_15m[-1]
        body_pct = (last_bar["c"] - last_bar["o"]) / last_bar["o"] if last_bar["o"] > 0 else 0
        ema_dist = (last_bar["c"] - ema21_15m) / ema21_15m if ema21_15m > 0 else 0
        bounce_ok = (last_bar["c"] > last_bar["o"]
                     and body_pct >= _MIN_BODY_PCT
                     and ema_dist >= _MIN_EMA_DIST)

        # Hard gate 2: bounce bar volume > pullback bar volume
        prev_bar  = bars_15m[-2] if len(bars_15m) >= 2 else None
        vol_ok = prev_bar is not None and last_bar["v"] > prev_bar["v"]

        signals = {
            "htf_aligned":    htf_aligned,
            "ema_structure":  ema_structure,
            "pullback_touch": True,   # True by definition (pullback_long passed)
        }
        score_val = sum(0.25 for v in signals.values() if v)
        entry, stop, tp = get_ema15m_long_levels(symbol, cache)
        # bounce_ok and vol_ok are hard gates — not scored, but block fire if False
        fire = score_val >= _THRESHOLD and bounce_ok and vol_ok and cool_ok and entry > 0

        results.append({
            "symbol":    symbol,
            "regime":    "EMA_PULLBACK",
            "direction": "LONG",
            "score":     round(score_val, 4),
            "signals":   {**signals, "bounce_confirm": bounce_ok, "vol_confirm": vol_ok},
            "fire":      fire,
            "ep_stop":   stop,
            "ep_tp":     tp,
        })

    # ── SHORT ─────────────────────────────────────────────────────────────────
    pullback_short = check_ema15m_pullback_short(symbol, cache)
    if pullback_short:
        htf_aligned   = _htf_bearish(symbol, cache)
        ema_structure = ema21_15m < ema50_15m if ema21_15m > 0 else False

        # Hard gate 1: bounce candle body ≥ 0.2% AND close ≥ 0.2% below EMA21
        last_bar = bars_15m[-1]
        body_pct = (last_bar["o"] - last_bar["c"]) / last_bar["o"] if last_bar["o"] > 0 else 0
        ema_dist = (ema21_15m - last_bar["c"]) / ema21_15m if ema21_15m > 0 else 0
        bounce_ok = (last_bar["c"] < last_bar["o"]
                     and body_pct >= _MIN_BODY_PCT
                     and ema_dist >= _MIN_EMA_DIST)

        # Hard gate 2: bounce bar volume > pullback bar volume
        prev_bar  = bars_15m[-2] if len(bars_15m) >= 2 else None
        vol_ok = prev_bar is not None and last_bar["v"] > prev_bar["v"]

        signals = {
            "htf_aligned":    htf_aligned,
            "ema_structure":  ema_structure,
            "pullback_touch": True,
        }
        score_val = sum(0.25 for v in signals.values() if v)
        entry, stop, tp = get_ema15m_short_levels(symbol, cache)
        # bounce_ok and vol_ok are hard gates — not scored, but block fire if False
        fire = score_val >= _THRESHOLD and bounce_ok and vol_ok and cool_ok and entry > 0

        results.append({
            "symbol":    symbol,
            "regime":    "EMA_PULLBACK",
            "direction": "SHORT",
            "score":     round(score_val, 4),
            "signals":   {**signals, "bounce_confirm": bounce_ok, "vol_confirm": vol_ok},
            "fire":      fire,
            "ep_stop":   stop,
            "ep_tp":     tp,
        })

    return results
