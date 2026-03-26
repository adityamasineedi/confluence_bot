"""1H inside bar flip scorer — live scoring with per-symbol cooldown.

Score components (equal-weighted, 0.25 each):
    compression_ok  — inside bar zone detected (≥ min_inside bars)
    entry_zone      — price within entry_zone_pct of zone boundary
    zone_tight      — zone_pct <= max_zone_pct (confirms real compression, not wide chop)
    cooldown_ok     — per-symbol cooldown not active
"""
import logging
import os
import time
import yaml

log = logging.getLogger(__name__)

_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "config.yaml")
with open(_CONFIG_PATH) as _f:
    _cfg = yaml.safe_load(_f)

_IB = _cfg.get("insidebar", {})

_MIN_INSIDE     = int(_IB.get("min_inside_bars",  2))
_MAX_ZONE_PCT   = float(_IB.get("max_zone_pct",   0.015))   # zone must be ≤1.5% wide
_ENTRY_ZONE_PCT = float(_IB.get("entry_zone_pct", 0.002))
_SL_BUFFER_PCT  = float(_IB.get("sl_buffer_pct",  0.002))
_RR_RATIO       = float(_IB.get("rr_ratio",        1.5))
_THRESHOLD      = float(_IB.get("fire_threshold",  0.50))
_COOLDOWN_SECS  = float(_IB.get("cooldown_mins",    60)) * 60.0

_cooldown_until: dict[str, float] = {}


def is_on_cooldown(symbol: str) -> bool:
    return time.monotonic() < _cooldown_until.get(symbol, 0.0)


def set_cooldown(symbol: str) -> None:
    _cooldown_until[symbol] = time.monotonic() + _COOLDOWN_SECS
    log.debug("InsideBar cooldown set for %s", symbol)


async def score(symbol: str, cache) -> list[dict]:
    """Score *symbol* for inside bar flip setups.

    Returns a list of score dicts (LONG and/or SHORT candidates).
    """
    from signals.insidebar.detector import (
        detect_compression,
        near_zone_low,
        near_zone_high,
        compute_levels,
    )

    bars = cache.get_ohlcv(symbol, window=16, tf="1h")
    if len(bars) < _MIN_INSIDE + 2:
        return []

    zone = detect_compression(bars, min_inside=_MIN_INSIDE)
    if zone is None:
        return []

    price   = bars[-1]["c"]
    cool_ok = not is_on_cooldown(symbol)
    zone_ok = zone["zone_pct"] <= _MAX_ZONE_PCT

    # ── Quality signals (vary per setup — give score real meaning) ────────────
    # strong_compression: run of 3+ inside bars (more reliable than 2)
    strong_compression = zone["bar_count"] >= 3
    # volume_declining: avg volume of inside bars < volume of the bar before the run
    inside_vols = [b.get("v", 0) for b in bars[-(zone["bar_count"] + 1):-1]]
    vol_before  = bars[-(zone["bar_count"] + 2)].get("v", 0) if len(bars) >= zone["bar_count"] + 3 else 0
    vol_declining = (sum(inside_vols) / len(inside_vols) < vol_before) if (inside_vols and vol_before > 0) else False
    # near_poc: price within 1% of zone POC (volume gravity — best entries)
    poc_dist = abs(price - zone["poc"]) / zone["poc"] if zone["poc"] > 0 else 1.0
    near_poc = poc_dist <= 0.01

    results = []

    # ── LONG: price near zone_low ──────────────────────────────────────────────
    if near_zone_low(price, zone["zone_low"], _ENTRY_ZONE_PCT):
        signals = {
            "entry_zone":        True,               # always True here (hard entry condition)
            "strong_compression": strong_compression, # 3+ inside bars
            "volume_declining":   vol_declining,      # quieting volume = genuine coil
            "near_poc":           near_poc,           # price near volume gravity
        }
        score_val = sum(0.25 for v in signals.values() if v)
        sl, tp = compute_levels("LONG", zone["zone_low"], zone["zone_high"],
                                _SL_BUFFER_PCT, _RR_RATIO, price)
        # Hard gates: zone must be tight + cooldown OK (not counted in score)
        fire = score_val >= _THRESHOLD and zone_ok and cool_ok
        results.append({
            "symbol":    symbol,
            "regime":    "INSIDEBAR",
            "direction": "LONG",
            "score":     round(score_val, 4),
            "signals":   signals,
            "fire":      fire,
            "ib_stop":   sl,
            "ib_tp":     tp,
            "zone_low":  zone["zone_low"],
            "zone_high": zone["zone_high"],
            "zone_pct":  round(zone["zone_pct"] * 100, 3),
            "poc":       zone["poc"],
            "bar_count": zone["bar_count"],
        })

    # ── SHORT: price near zone_high ────────────────────────────────────────────
    if near_zone_high(price, zone["zone_high"], _ENTRY_ZONE_PCT):
        signals = {
            "entry_zone":         True,
            "strong_compression": strong_compression,
            "volume_declining":   vol_declining,
            "near_poc":           near_poc,
        }
        score_val = sum(0.25 for v in signals.values() if v)
        sl, tp = compute_levels("SHORT", zone["zone_low"], zone["zone_high"],
                                _SL_BUFFER_PCT, _RR_RATIO, price)
        fire = score_val >= _THRESHOLD and zone_ok and cool_ok
        results.append({
            "symbol":    symbol,
            "regime":    "INSIDEBAR",
            "direction": "SHORT",
            "score":     round(score_val, 4),
            "signals":   signals,
            "fire":      fire,
            "ib_stop":   sl,
            "ib_tp":     tp,
            "zone_low":  zone["zone_low"],
            "zone_high": zone["zone_high"],
            "zone_pct":  round(zone["zone_pct"] * 100, 3),
            "poc":       zone["poc"],
            "bar_count": zone["bar_count"],
        })

    return results
