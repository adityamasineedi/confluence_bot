"""Wyckoff Upthrust scorer — PF 1.87–9.99 across 7 coins in bear regimes. NOT YET IMPLEMENTED.
Returns empty list until implemented. Referenced in strategy_routing for bear/crash regimes.
Build order: #2 priority — see CLAUDE.md.
"""
import logging
log = logging.getLogger(__name__)


async def score(symbol: str, cache) -> list[dict]:
    log.debug("wyckoff_upthrust scorer called for %s — NOT YET BUILT, returning []", symbol)
    return []
