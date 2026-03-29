"""Returns merged strategy config for a specific symbol.
Tier config overrides base config. Base config is the fallback."""

import os
import yaml

_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "config.yaml")

_cfg_cache: dict = {}


def _load_cfg() -> dict:
    global _cfg_cache
    if not _cfg_cache:
        with open(_CONFIG_PATH) as f:
            _cfg_cache = yaml.safe_load(f)
    return _cfg_cache


def clear_config_cache() -> None:
    global _cfg_cache
    _cfg_cache = {}


def get_symbol_config(symbol: str, strategy: str) -> dict:
    """Return strategy params for this symbol, merged from tier + base config.

    Priority: tier config > base strategy config > hardcoded defaults
    """
    cfg = _load_cfg()

    base = cfg.get(strategy, {}).copy()
    tiers = cfg.get("symbol_tiers", {})

    for tier_name, tier_data in tiers.items():
        if symbol.upper() in [s.upper() for s in tier_data.get("symbols", [])]:
            tier_overrides = tier_data.get(strategy, {})
            base.update(tier_overrides)
            base["_tier"] = tier_name
            return base

    base["_tier"] = "base"
    return base


def get_symbol_tier(symbol: str) -> str:
    """Return tier name for symbol: 'tier1', 'tier2', 'tier3', or 'base'."""
    cfg = _load_cfg()
    tiers = cfg.get("symbol_tiers", {})
    for tier_name, tier_data in tiers.items():
        if symbol.upper() in [s.upper() for s in tier_data.get("symbols", [])]:
            return tier_name
    return "base"


# ── ATR helpers ───────────────────────────────────────────────────────────────

def _calc_atr(bars: list[dict], period: int = 14) -> float:
    """True Range = max(high-low, |high-prev_close|, |low-prev_close|)"""
    if len(bars) < period + 1:
        return 0.0
    trs = []
    for i in range(1, len(bars)):
        h, l, pc = bars[i]["h"], bars[i]["l"], bars[i - 1]["c"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    atr = sum(trs[-period:]) / period
    return atr


def _microrange_dynamic(base: dict, tier: str, atr_pct: float) -> dict:
    if atr_pct < 0.0004:
        vol_regime = "calm"
    elif atr_pct < 0.0008:
        vol_regime = "normal"
    else:
        vol_regime = "volatile"

    # (box_mult, stop_mult, tp_ratio, rsi_long_max, rsi_short_min, max_hold)
    params_table = {
        "tier1": {
            "calm":     (1.0, 0.6, 0.75, 33, 67, 3),
            "normal":   (1.2, 0.8, 0.70, 35, 65, 3),
            "volatile": (1.5, 1.0, 0.65, 38, 62, 4),
        },
        "tier2": {
            "calm":     (1.2, 0.8, 0.80, 38, 62, 4),
            "normal":   (1.5, 1.0, 0.75, 40, 60, 4),
            "volatile": (1.8, 1.2, 0.70, 43, 57, 5),
        },
        "tier3": {
            "calm":     (1.5, 0.9, 0.85, 42, 58, 4),
            "normal":   (2.0, 1.2, 0.80, 45, 55, 5),
            "volatile": (2.5, 1.5, 0.75, 48, 52, 6),
        },
        "base": {
            "calm":     (1.2, 0.8, 0.75, 38, 62, 4),
            "normal":   (1.5, 1.0, 0.70, 40, 60, 4),
            "volatile": (1.8, 1.2, 0.65, 43, 57, 5),
        },
    }
    tier_params = params_table.get(tier, params_table["base"])
    box_mult, stop_mult, tp_ratio, rsi_long_max, rsi_short_min, max_hold = tier_params[vol_regime]

    tier_box_limits = {
        "tier1": (0.003, 0.010),
        "tier2": (0.005, 0.018),
        "tier3": (0.008, 0.030),
        "base":  (0.005, 0.020),
    }
    box_lo, box_hi = tier_box_limits.get(tier, (0.005, 0.020))
    range_max_pct = round(max(box_lo, min(box_hi, atr_pct * box_mult)), 4)

    raw_stop = atr_pct * stop_mult
    max_stop = range_max_pct / 2.2
    stop_pct = round(max(0.001, min(max_stop, raw_stop)), 4)

    entry_zone_pct = round(max(0.0005, min(0.005, stop_pct * 0.3)), 4)

    tp_distance = range_max_pct * tp_ratio
    actual_rr   = tp_distance / stop_pct if stop_pct > 0 else 0
    if actual_rr < 1.5:
        tp_ratio = round(min(0.95, (stop_pct * 1.5) / range_max_pct), 2)

    dynamic = base.copy()
    dynamic["range_max_pct"]  = range_max_pct
    dynamic["stop_pct"]       = stop_pct
    dynamic["entry_zone_pct"] = entry_zone_pct
    dynamic["tp_ratio"]       = tp_ratio
    dynamic["rsi_long_max"]   = rsi_long_max
    dynamic["rsi_short_min"]  = rsi_short_min
    dynamic["max_hold_bars"]  = max_hold
    dynamic["_atr_pct"]       = round(atr_pct * 100, 4)
    dynamic["_vol_regime"]    = vol_regime
    dynamic["_dynamic"]       = True
    final_tp = dynamic["range_max_pct"] * dynamic["tp_ratio"]
    dynamic["_expected_rr"] = round(final_tp / dynamic["stop_pct"], 2) if dynamic["stop_pct"] > 0 else 0
    return dynamic


def _ema_pullback_dynamic(base: dict, tier: str, atr_pct: float) -> dict:
    if atr_pct < 0.0004:
        vol_regime = "calm"
    elif atr_pct < 0.0008:
        vol_regime = "normal"
    else:
        vol_regime = "volatile"

    # (touch_mult, body_mult, rr_ratio, max_hold)
    params_table = {
        "tier1": {
            "calm":     (0.5, 0.4, 1.5,  6),
            "normal":   (0.6, 0.5, 1.8,  8),
            "volatile": (0.8, 0.6, 2.0, 10),
        },
        "tier2": {
            "calm":     (0.7, 0.5, 1.8,  6),
            "normal":   (0.8, 0.6, 2.0,  8),
            "volatile": (1.0, 0.7, 2.2, 10),
        },
        "tier3": {
            "calm":     (0.9, 0.6, 2.0,  8),
            "normal":   (1.1, 0.8, 2.2, 10),
            "volatile": (1.4, 1.0, 2.5, 12),
        },
        "base": {
            "calm":     (0.7, 0.5, 1.8,  6),
            "normal":   (0.8, 0.6, 2.0,  8),
            "volatile": (1.0, 0.7, 2.2, 10),
        },
    }
    tier_params = params_table.get(tier, params_table["base"])
    touch_mult, body_mult, rr_ratio, max_hold = tier_params[vol_regime]

    dynamic = base.copy()
    dynamic["pullback_touch_pct"]  = round(max(0.001, min(0.008, atr_pct * touch_mult)), 4)
    dynamic["min_bounce_body_pct"] = round(max(0.001, min(0.006, atr_pct * body_mult)),  4)
    dynamic["rr_ratio"]            = rr_ratio
    dynamic["max_hold_bars"]       = max_hold
    dynamic["_atr_pct"]            = round(atr_pct * 100, 4)
    dynamic["_vol_regime"]         = vol_regime
    dynamic["_dynamic"]            = True
    return dynamic


# ── Dynamic config (live — uses cache) ───────────────────────────────────────

def get_dynamic_config(symbol: str, strategy: str, cache) -> dict:
    """Return strategy params scaled to current symbol volatility via ATR.

    ATR multipliers are tier-specific — tier1 uses tighter multipliers
    than tier3 because tier1 symbols have more predictable mean reversion.

    Falls back to static tier config if ATR data unavailable.
    """
    base = get_symbol_config(symbol, strategy)
    tier = base.get("_tier", "base")

    bars = cache.get_ohlcv(symbol, window=30, tf="5m")
    if not bars or len(bars) < 20:
        return base

    atr = _calc_atr(bars, period=14)
    if atr <= 0:
        return base

    price = bars[-1]["c"]
    if price <= 0:
        return base

    atr_pct = atr / price

    if strategy == "microrange":
        return _microrange_dynamic(base, tier, atr_pct)
    if strategy == "ema_pullback":
        return _ema_pullback_dynamic(base, tier, atr_pct)

    return base
