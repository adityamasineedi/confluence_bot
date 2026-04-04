"""Strategy router — returns active strategies for symbol + regime combination.

Reads strategy_routing from config.yaml.
Falls back to _default if symbol not found.
Respects individual strategy enabled: false flags.
"""
import logging
import os
import yaml

log = logging.getLogger(__name__)

_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "config.yaml")

_cfg_cache: dict = {}


def _load_cfg() -> dict:
    global _cfg_cache
    if not _cfg_cache:
        with open(_CONFIG_PATH) as f:
            _cfg_cache = yaml.safe_load(f)
    return _cfg_cache


def clear_cache() -> None:
    global _cfg_cache
    _cfg_cache = {}


def get_active_strategies(symbol: str, regime: str) -> list[str]:
    """Return list of strategy names active for this symbol + regime.

    Filters out strategies with enabled: false in their config block.
    Returns empty list if no strategies apply (bot stays flat).

    Example:
        get_active_strategies("SOLUSDT", "RANGE")
        → ["vwap_band", "sweep"]
    """
    clear_cache()   # always read fresh config — enables hot updates without restart
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
