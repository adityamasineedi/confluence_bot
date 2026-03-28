"""Returns merged strategy config for a specific symbol.
Tier config overrides base config. Base config is the fallback."""

import os
import yaml

_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "config.yaml")


def get_symbol_config(symbol: str, strategy: str) -> dict:
    """Return strategy params for this symbol, merged from tier + base config.

    Priority: tier config > base strategy config > hardcoded defaults
    """
    with open(_CONFIG_PATH) as f:
        cfg = yaml.safe_load(f)

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
    with open(_CONFIG_PATH) as f:
        cfg = yaml.safe_load(f)
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
    """Scale microrange params to current ATR.

    Tier multipliers:
      tier1: tight — BTC, predictable reversion
      tier2: medium — ETH/SOL/BNB, moderate volatility
      tier3: wide — DOGE/AVAX, high volatility
    """
    multipliers = {
        "tier1": {"box": 1.2, "stop": 0.8, "zone": 0.4},
        "tier2": {"box": 1.5, "stop": 1.0, "zone": 0.5},
        "tier3": {"box": 2.0, "stop": 1.2, "zone": 0.6},
        "base":  {"box": 1.5, "stop": 1.0, "zone": 0.5},
    }
    m = multipliers.get(tier, multipliers["base"])

    dynamic = base.copy()

    # Box width = ATR × box_multiplier, clamped to tier min/max
    tier_limits = {
        "tier1": (0.003, 0.010),
        "tier2": (0.005, 0.015),
        "tier3": (0.008, 0.025),
        "base":  (0.005, 0.020),
    }
    lo, hi = tier_limits.get(tier, (0.005, 0.020))
    dynamic["range_max_pct"]  = round(max(lo, min(hi, atr_pct * m["box"])), 4)
    dynamic["stop_pct"]       = round(max(0.001, min(0.008, atr_pct * m["stop"])), 4)
    dynamic["entry_zone_pct"] = round(max(0.001, min(0.005, atr_pct * m["zone"])), 4)

    # Validate invariant: stop_pct × 2 must be ≤ range_max_pct
    if dynamic["stop_pct"] * 2 > dynamic["range_max_pct"]:
        dynamic["stop_pct"] = round(dynamic["range_max_pct"] / 2.5, 4)

    dynamic["_atr_pct"] = round(atr_pct * 100, 4)
    dynamic["_dynamic"] = True
    return dynamic


def _ema_pullback_dynamic(base: dict, tier: str, atr_pct: float) -> dict:
    """Scale EMA pullback params to current ATR."""
    multipliers = {
        "tier1": {"touch": 0.6, "body": 0.5},
        "tier2": {"touch": 0.8, "body": 0.6},
        "tier3": {"touch": 1.2, "body": 0.8},
        "base":  {"touch": 0.8, "body": 0.6},
    }
    m = multipliers.get(tier, multipliers["base"])

    dynamic = base.copy()
    dynamic["pullback_touch_pct"]  = round(max(0.001, min(0.008, atr_pct * m["touch"])), 4)
    dynamic["min_bounce_body_pct"] = round(max(0.001, min(0.006, atr_pct * m["body"])), 4)
    dynamic["_atr_pct"] = round(atr_pct * 100, 4)
    dynamic["_dynamic"] = True
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

    bars = cache.get_ohlcv(symbol, window=20, tf="5m")
    if not bars or len(bars) < 15:
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
