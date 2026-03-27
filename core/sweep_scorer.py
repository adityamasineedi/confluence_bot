"""Liquidity Sweep Reversal scorer.

Fires when a 15m swing high/low is swept (stop-hunt) then price reverses.
Works in ANY regime — does not require BTC above EMA200 or trending conditions.
This fills the bear-market gap where MAIN strategy fires almost nothing.

Score components (each 0.33):
    sweep_detected  — wick ≥ 0.5% through swing level, close back inside
    volume_spike    — sweep candle volume ≥ 2.0× average (institutional only)
    rsi_zone        — RSI confirms entry direction (< 50 for long, > 50 for short)

Hard gate (not scored — blocks fire if it fails):
    htf_no_block    — 4H price within 1.0% of EMA21 (not in a strong opposing trend).
                      No 4H data → gate FAILS (conservative: skip rather than guess).

Threshold: 0.75 (effectively all 3 scored signals must fire, since sweep_detected
is always True when scorer is called, leaving vol_spike + rsi_zone needed).
"""
import logging
import os
import time
import yaml

from core.cooldown_store import CooldownStore

log = logging.getLogger(__name__)

_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "config.yaml")
with open(_CONFIG_PATH) as _f:
    _cfg = yaml.safe_load(_f)

_SW = _cfg.get("sweep", {})

_COOLDOWN_SECS = float(_SW.get("cooldown_mins", 30)) * 60.0
_THRESHOLD     = float(_SW.get("fire_threshold", 0.75))

_cd = CooldownStore("SWEEP")

_RSI_PERIOD     = 14
_RSI_LONG_MAX   = 50
_RSI_SHORT_MIN  = 50
_VOL_SPIKE_MULT = float(_SW.get("volume_spike_mult", 2.0))   # 2.0× = institutional only
_HTF_BLOCK_PCT  = float(_SW.get("htf_no_block_pct",  0.010)) # 1.0% EMA deviation = strong trend


def is_on_cooldown(symbol: str) -> bool:
    return _cd.is_active(symbol)


def set_cooldown(symbol: str) -> None:
    _cd.set(symbol, _COOLDOWN_SECS)


def _rsi(closes: list[float], period: int = _RSI_PERIOD) -> float:
    if len(closes) < period + 1:
        return 50.0
    gains, losses = [], []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gains.append(max(diff, 0.0))
        losses.append(max(-diff, 0.0))
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0.0:
        return 100.0
    return 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)


def _htf_no_block_long(symbol: str, cache) -> bool:
    """4H is not strongly bearish — hard gate, False when data is missing.

    Returns False (block LONG) when:
    - 4H data unavailable (conservative: no data → no trade)
    - 4H close is more than _HTF_BLOCK_PCT below EMA21 (strong downtrend)
    """
    bars_4h = cache.get_ohlcv(symbol, window=30, tf="4h")
    if len(bars_4h) < 20:
        return False   # no data → block (was: True; flipped to hard gate)
    closes = [b["c"] for b in bars_4h]
    k   = 2.0 / 21
    ema = sum(closes[:20]) / 20
    for c in closes[20:]:
        ema = c * k + ema * (1 - k)
    return closes[-1] >= ema * (1 - _HTF_BLOCK_PCT)   # within 1.0% below ema


def _htf_no_block_short(symbol: str, cache) -> bool:
    """4H is not strongly bullish — hard gate, False when data is missing."""
    bars_4h = cache.get_ohlcv(symbol, window=30, tf="4h")
    if len(bars_4h) < 20:
        return False   # no data → block (was: True; flipped to hard gate)
    closes = [b["c"] for b in bars_4h]
    k   = 2.0 / 21
    ema = sum(closes[:20]) / 20
    for c in closes[20:]:
        ema = c * k + ema * (1 - k)
    return closes[-1] <= ema * (1 + _HTF_BLOCK_PCT)   # within 1.0% above ema


async def score(symbol: str, cache) -> list[dict]:
    """Score symbol for liquidity sweep reversal setups.

    Returns list of score dicts (LONG and/or SHORT), same format as other scorers.
    Adds keys: sw_stop, sw_tp for executor preset levels.
    """
    from signals.trend.sweep_reversal import (
        check_sweep_long,
        check_sweep_short,
        get_sweep_long_levels,
        get_sweep_short_levels,
    )

    results = []
    cool_ok = not is_on_cooldown(symbol)

    # ── LONG sweep ────────────────────────────────────────────────────────────
    if check_sweep_long(symbol, cache):
        bars = cache.get_ohlcv(symbol, window=25, tf="15m")
        closes = [b["c"] for b in bars] if bars else []

        vol_spike = False
        if len(bars) >= 21:
            avg_vol = sum(b["v"] for b in bars[-21:-1]) / 20
            vol_spike = avg_vol > 0 and bars[-1]["v"] >= avg_vol * _VOL_SPIKE_MULT

        rsi_ok = _rsi(closes) <= _RSI_LONG_MAX if closes else False

        # htf_no_block is a HARD GATE — not counted in score, but blocks fire if False.
        # No 4H data → gate fails → no trade (conservative).
        htf_ok = _htf_no_block_long(symbol, cache)

        signals = {
            "sweep_detected": True,
            "volume_spike":   vol_spike,
            "rsi_zone":       rsi_ok,
        }
        score_val = sum(0.25 for v in signals.values() if v)

        entry, stop, tp = get_sweep_long_levels(symbol, cache)
        fire = score_val >= _THRESHOLD and htf_ok and cool_ok and entry > 0

        results.append({
            "symbol":    symbol,
            "regime":    "SWEEP",
            "direction": "LONG",
            "score":     round(score_val, 4),
            "signals":   {**signals, "htf_no_block": htf_ok},   # shown in UI, not scored
            "fire":      fire,
            "sw_stop":   stop,
            "sw_tp":     tp,
        })

    # ── SHORT sweep ───────────────────────────────────────────────────────────
    if check_sweep_short(symbol, cache):
        bars = cache.get_ohlcv(symbol, window=25, tf="15m")
        closes = [b["c"] for b in bars] if bars else []

        vol_spike = False
        if len(bars) >= 21:
            avg_vol = sum(b["v"] for b in bars[-21:-1]) / 20
            vol_spike = avg_vol > 0 and bars[-1]["v"] >= avg_vol * _VOL_SPIKE_MULT

        rsi_ok = _rsi(closes) >= _RSI_SHORT_MIN if closes else False
        htf_ok = _htf_no_block_short(symbol, cache)

        signals = {
            "sweep_detected": True,
            "volume_spike":   vol_spike,
            "rsi_zone":       rsi_ok,
        }
        score_val = sum(0.25 for v in signals.values() if v)

        entry, stop, tp = get_sweep_short_levels(symbol, cache)
        fire = score_val >= _THRESHOLD and htf_ok and cool_ok and entry > 0

        results.append({
            "symbol":    symbol,
            "regime":    "SWEEP",
            "direction": "SHORT",
            "score":     round(score_val, 4),
            "signals":   {**signals, "htf_no_block": htf_ok},   # shown in UI, not scored
            "fire":      fire,
            "sw_stop":   stop,
            "sw_tp":     tp,
        })

    return results
