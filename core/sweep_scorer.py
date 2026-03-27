"""Liquidity Sweep Reversal scorer.

Fires when a 15m swing high/low is swept (stop-hunt) then price reverses.
Works in ANY regime — does not require BTC above EMA200 or trending conditions.
This fills the bear-market gap where MAIN strategy fires almost nothing.

Score components (each 0.25):
    sweep_detected  — wick through swing level, close back inside
    volume_spike    — sweep candle volume ≥ 1.4× average (institutional)
    rsi_zone        — RSI confirms entry direction (< 50 for long, > 50 for short)
    htf_no_block    — 4H structure does not strongly oppose the entry
                      (neutral or same direction — weak filter, not a blocker)

Threshold: 0.75 (3 of 4 signals) — sweep_detected is always True when this
scorer is called, so effectively 2 of the remaining 3 confirmations are needed.
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

_RSI_PERIOD   = 14
_RSI_LONG_MAX = 50
_RSI_SHORT_MIN = 50
_VOL_SPIKE_MULT = 1.4


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
    """4H is not strongly bearish (ADX-based -DI dominance)."""
    bars_4h = cache.get_ohlcv(symbol, window=30, tf="4h")
    if len(bars_4h) < 20:
        return True   # no data → don't block
    # Simple: last 4H close above EMA20 = bullish bias or neutral
    closes = [b["c"] for b in bars_4h]
    k = 2.0 / 21
    ema = sum(closes[:20]) / 20
    for c in closes[20:]:
        ema = c * k + ema * (1 - k)
    # Allow LONG if 4H is not in a strong downtrend (close not far below EMA20)
    return closes[-1] >= ema * 0.985   # within 1.5% below ema = not strongly bearish


def _htf_no_block_short(symbol: str, cache) -> bool:
    """4H is not strongly bullish."""
    bars_4h = cache.get_ohlcv(symbol, window=30, tf="4h")
    if len(bars_4h) < 20:
        return True
    closes = [b["c"] for b in bars_4h]
    k = 2.0 / 21
    ema = sum(closes[:20]) / 20
    for c in closes[20:]:
        ema = c * k + ema * (1 - k)
    return closes[-1] <= ema * 1.015   # within 1.5% above ema = not strongly bullish


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

        # Volume spike on the sweep candle
        vol_spike = False
        if len(bars) >= 21:
            avg_vol = sum(b["v"] for b in bars[-21:-1]) / 20
            vol_spike = avg_vol > 0 and bars[-1]["v"] >= avg_vol * _VOL_SPIKE_MULT

        rsi_ok  = _rsi(closes) <= _RSI_LONG_MAX if closes else False
        htf_ok  = _htf_no_block_long(symbol, cache)

        signals = {
            "sweep_detected": True,
            "volume_spike":   vol_spike,
            "rsi_zone":       rsi_ok,
            "htf_no_block":   htf_ok,
        }
        score_val = sum(0.25 for v in signals.values() if v)

        entry, stop, tp = get_sweep_long_levels(symbol, cache)
        fire = score_val >= _THRESHOLD and cool_ok and entry > 0

        results.append({
            "symbol":    symbol,
            "regime":    "SWEEP",
            "direction": "LONG",
            "score":     round(score_val, 4),
            "signals":   signals,
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
            "htf_no_block":   htf_ok,
        }
        score_val = sum(0.25 for v in signals.values() if v)

        entry, stop, tp = get_sweep_short_levels(symbol, cache)
        fire = score_val >= _THRESHOLD and cool_ok and entry > 0

        results.append({
            "symbol":    symbol,
            "regime":    "SWEEP",
            "direction": "SHORT",
            "score":     round(score_val, 4),
            "signals":   signals,
            "fire":      fire,
            "sw_stop":   stop,
            "sw_tp":     tp,
        })

    return results
