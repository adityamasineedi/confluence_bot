"""Micro-range flip scorer — mean-reversion entries inside a tight 5m consolidation box.

Strategy logic
--------------
1. Detect a tight consolidation box on the last N completed 5m bars.
2. If price is near range_low → score a LONG (bounce off floor).
3. If price is near range_high → score a SHORT (rejection from ceiling).
4. RSI filter confirms direction (optional but enabled by default).
5. Volume filter rejects entries on high-volume bars (potential breakout).
6. Per-symbol cooldown (time.monotonic) prevents re-entering the same move.

SL/TP are boundary-anchored (not entry-relative), so RR stays ~2.5 regardless
of where exactly inside the entry zone the fill lands.

Score components (equal-weighted, 0.25 each):
    box_detected    — tight range box confirmed on last window_bars
    entry_zone      — price within entry_zone_pct of range_low / range_high
    volume_ok       — current bar not a volume spike (quiet consolidation)
    rsi_aligned     — RSI confirms mean-reversion direction
"""
import logging
import os
import time
import yaml

log = logging.getLogger(__name__)

_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "config.yaml")
with open(_CONFIG_PATH) as _f:
    _cfg = yaml.safe_load(_f)

_MR = _cfg.get("microrange", {})

_COOLDOWN_SECS = float(_MR.get("cooldown_mins", 20)) * 60.0

from core.cooldown_store import CooldownStore
_cd = CooldownStore("MICRORANGE")


# ── Cooldown helpers ───────────────────────────────────────────────────────────

def is_on_cooldown(symbol: str) -> bool:
    return _cd.is_active(symbol)


def set_cooldown(symbol: str) -> None:
    _cd.set(symbol, _COOLDOWN_SECS)
    log.debug("MicroRange cooldown set for %s (%.0f min)", symbol, _COOLDOWN_SECS / 60)


def cooldown_remaining(symbol: str) -> float:
    return _cd.remaining(symbol)


# ── Scorer ─────────────────────────────────────────────────────────────────────

async def score(symbol: str, cache) -> list[dict]:
    """Score *symbol* for micro-range flip setups.

    Returns a list of score dicts (may contain both LONG and SHORT candidates,
    though in practice only one side fires at a time — you can't be at both
    range_low and range_high simultaneously).

    Each dict follows the standard format:
        symbol, regime, direction, score, signals, fire
    Plus micro-range-specific keys:
        mr_stop, mr_tp   — boundary-anchored levels (executor reads these)
        range_low, range_high, range_width_pct
    """
    from core.symbol_config import get_dynamic_config
    cfg = get_dynamic_config(symbol, "microrange", cache)
    _WINDOW_BARS    = int(cfg.get("window_bars",      10))
    _RANGE_MAX_PCT  = float(cfg.get("range_max_pct",  0.010))
    _ENTRY_ZONE_PCT = float(cfg.get("entry_zone_pct", 0.002))
    _STOP_PCT       = float(cfg.get("stop_pct",       0.003))
    _TP_RATIO       = float(cfg.get("tp_ratio",       0.75))
    _MAX_VOL_RATIO  = float(cfg.get("max_vol_ratio",  1.3))
    _RSI_LONG_MAX   = float(cfg.get("rsi_long_max",   40.0))
    _RSI_SHORT_MIN  = float(cfg.get("rsi_short_min",  60.0))
    _THRESHOLD      = float(cfg.get("fire_threshold", 0.75))
    _MAX_HOLD       = int(cfg.get("max_hold_bars",    6))
    _MIN_BOX_PCT    = _STOP_PCT * 2.0

    from signals.microrange.detector import (
        detect_micro_range,
        near_range_low,
        near_range_high,
        low_volume,
        rsi_supports_long,
        rsi_supports_short,
        compute_levels,
        count_boundary_touches,
    )

    from signals.volume_momentum import VolumeContext, get_volume_params

    bars = cache.get_ohlcv(symbol, window=_WINDOW_BARS + 22, tf="5m")
    if len(bars) < _WINDOW_BARS + 2:
        return []

    box = detect_micro_range(bars, _WINDOW_BARS, _RANGE_MAX_PCT)
    if box is None:
        return []

    # Reject boxes too narrow to achieve minimum RR — avoids marginal 1.5× trades
    if box["range_width_pct"] < _MIN_BOX_PCT:
        return []

    # Box establishment gate — both boundaries must be tested enough times
    min_touches = int(cfg.get("min_box_touches", 2))
    if not count_boundary_touches(
        bars[-(  _WINDOW_BARS + 1):-1],
        box["range_low"], box["range_high"],
        min_touches=min_touches,
    ):
        return []

    price   = bars[-1]["c"]
    closes  = [b["c"] for b in bars]
    vol_ok  = low_volume(bars, _MAX_VOL_RATIO)
    cool_ok = not is_on_cooldown(symbol)

    # Dynamic volume params — 5m microrange, regime-aware thresholds
    from core.regime_detector import _detector
    regime = str(_detector.detect(symbol, cache))

    vol_ctx    = VolumeContext(symbol=symbol, regime=regime, timeframe="5m", cache=cache)
    vol_params = get_volume_params(vol_ctx)

    # Hard block: increasing volume means range is breaking out, not reverting
    if vol_params.increasing(bars):
        log.debug("MicroRange blocked — increasing volume (breakout risk) %s", symbol)
        return []

    long_blocked  = vol_params.bearish_divergence(bars)   # distribution: block LONG
    short_blocked = vol_params.bullish_divergence(bars)   # accumulation: block SHORT

    results = []

    # ── LONG: price near range_low ─────────────────────────────────────────────
    if near_range_low(price, box["range_low"], _ENTRY_ZONE_PCT):
        # Hard block: bearish divergence (distribution) into range low
        if long_blocked:
            pass   # skip — fall through without appending
        else:
            rsi_ok = rsi_supports_long(closes, _RSI_LONG_MAX)
            signals = {
                "box_detected":      True,
                "entry_zone":        True,
                "volume_ok":         vol_ok,
                "rsi_aligned":       rsi_ok,
                "vol_divergence_ok": not long_blocked,   # True since not blocked
            }
            score_val = sum(0.25 for v in (
                signals["box_detected"],
                signals["entry_zone"],
                signals["volume_ok"],
                signals["rsi_aligned"],
            ) if v)
            sl, tp = compute_levels(
                "LONG",
                box["range_low"], box["range_high"], box["range_width"],
                _STOP_PCT, _TP_RATIO,
            )
            fire = score_val >= _THRESHOLD and cool_ok
            results.append({
                "symbol":          symbol,
                "regime":          "MICRORANGE",
                "direction":       "LONG",
                "score":           round(score_val, 4),
                "signals":         signals,
                "fire":            fire,
                "mr_stop":         sl,
                "mr_tp":           tp,
                "range_low":       box["range_low"],
                "range_high":      box["range_high"],
                "range_width_pct": round(box["range_width_pct"] * 100, 4),
                "_atr_pct":        cfg.get("_atr_pct", 0),
                "_tier":           cfg.get("_tier", "base"),
                "_dynamic":        cfg.get("_dynamic", False),
            })

    # ── SHORT: price near range_high ───────────────────────────────────────────
    if near_range_high(price, box["range_high"], _ENTRY_ZONE_PCT):
        # Hard block: bullish divergence (accumulation) into range high
        if short_blocked:
            pass   # skip — fall through without appending
        else:
            rsi_ok = rsi_supports_short(closes, _RSI_SHORT_MIN)
            signals = {
                "box_detected":      True,
                "entry_zone":        True,
                "volume_ok":         vol_ok,
                "rsi_aligned":       rsi_ok,
                "vol_divergence_ok": not short_blocked,   # True since not blocked
            }
            score_val = sum(0.25 for v in (
                signals["box_detected"],
                signals["entry_zone"],
                signals["volume_ok"],
                signals["rsi_aligned"],
            ) if v)
            sl, tp = compute_levels(
                "SHORT",
                box["range_low"], box["range_high"], box["range_width"],
                _STOP_PCT, _TP_RATIO,
            )
            fire = score_val >= _THRESHOLD and cool_ok
            results.append({
                "symbol":          symbol,
                "regime":          "MICRORANGE",
                "direction":       "SHORT",
                "score":           round(score_val, 4),
                "signals":         signals,
                "fire":            fire,
                "mr_stop":         sl,
                "mr_tp":           tp,
                "range_low":       box["range_low"],
                "range_high":      box["range_high"],
                "range_width_pct": round(box["range_width_pct"] * 100, 4),
                "_atr_pct":        cfg.get("_atr_pct", 0),
                "_tier":           cfg.get("_tier", "base"),
                "_dynamic":        cfg.get("_dynamic", False),
            })

    return results
