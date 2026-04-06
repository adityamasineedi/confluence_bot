"""CME Gap scorer — BTC only, PF 2.74, 135 trades/3yr. NOT YET IMPLEMENTED.
Returns empty list until implemented. Referenced in BTCUSDT strategy_routing.
Build order: #1 priority — see CLAUDE.md.
"""
import logging
log = logging.getLogger(__name__)


async def score(symbol: str, cache) -> list[dict]:
    log.debug("cme_gap scorer called for %s — NOT YET BUILT, returning []", symbol)
    return []
