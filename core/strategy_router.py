"""Strategy router — returns active strategies for symbol + regime combination.

Reads strategy_routing from config.yaml.
Falls back to _default if symbol not found.
Respects individual strategy enabled: false flags.

Config is cached in memory and re-checked every 30 seconds by file mtime.
Only reloaded when the file has actually changed on disk.  If the file is
mid-write (empty or corrupt), the last good version is kept.
"""
import logging
import os
import time
import yaml

log = logging.getLogger(__name__)

_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "config.yaml")

_cfg_cache:      dict  = {}
_cfg_mtime:      float = 0.0
_cfg_last_check: float = 0.0
_CFG_CHECK_INTERVAL = 30.0   # re-check file every 30s, reload only if changed


def _load_cfg() -> dict:
    global _cfg_cache, _cfg_mtime, _cfg_last_check
    now = time.monotonic()
    if now - _cfg_last_check < _CFG_CHECK_INTERVAL:
        return _cfg_cache          # return cached — fast path
    _cfg_last_check = now
    try:
        mtime = os.path.getmtime(_CONFIG_PATH)
        if mtime != _cfg_mtime:
            with open(_CONFIG_PATH) as f:
                new_cfg = yaml.safe_load(f)
            if new_cfg:             # guard against empty file during write
                _cfg_cache = new_cfg
                _cfg_mtime = mtime
                log.info("config.yaml reloaded (mtime changed)")
    except Exception as exc:
        log.warning("config.yaml reload failed — using last good version: %s", exc)
    return _cfg_cache


def clear_cache() -> None:
    """Force reload on next call — used by tests."""
    global _cfg_mtime, _cfg_last_check
    _cfg_mtime      = 0.0
    _cfg_last_check = 0.0


def get_active_strategies(symbol: str, regime: str) -> list[str]:
    """Return list of strategy names active for this symbol + regime.

    Filters out strategies with enabled: false in their config block.
    Returns empty list if no strategies apply (bot stays flat).

    Example:
        get_active_strategies("SOLUSDT", "RANGE")
        → ["vwap_band", "sweep"]
    """
    cfg = _load_cfg()
    routing = cfg.get("strategy_routing", {})

    # Get symbol-specific routing, fall back to _default
    symbol_routes  = routing.get(symbol.upper(), routing.get("_default", {}))

    # Handle unknown/invalid regimes
    _VALID = {"TREND", "RANGE", "CRASH", "PUMP", "BREAKOUT"}
    if regime.upper() not in _VALID:
        log.warning("Unknown regime '%s' for %s — falling back to RANGE",
                    regime, symbol)
        regime = "RANGE"

    regime_strats  = symbol_routes.get(regime.upper(), [])

    # Filter out disabled strategies
    active = []
    for strat in regime_strats:
        strat_cfg = cfg.get(strat, {})
        if strat_cfg.get("enabled", True):
            active.append(strat)

    return active


def get_regime_summary() -> dict:
    """Return full routing table for all symbols — used by debug UI."""
    cfg     = _load_cfg()
    routing = cfg.get("strategy_routing", {})
    summary = {}
    for symbol, regimes in routing.items():
        if symbol == "_default":
            continue
        summary[symbol] = {}
        for regime, strategies in regimes.items():
            active = []
            for strat in strategies:
                strat_cfg = cfg.get(strat, {})
                if strat_cfg.get("enabled", True):
                    active.append(strat)
            summary[symbol][regime] = active
    return summary
