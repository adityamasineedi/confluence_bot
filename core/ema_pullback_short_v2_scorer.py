"""EMA Pullback Short v2 scorer — XRP PF 1.58, 233 trades/3yr. NOT YET IMPLEMENTED.
Returns empty list until implemented. Referenced in strategy_routing for XRP+SOL.
Build order: #3 priority — see CLAUDE.md.
"""
import logging
log = logging.getLogger(__name__)


async def score(symbol: str, cache) -> list[dict]:
    log.debug("ema_pullback_short_v2 scorer called for %s — NOT YET BUILT, returning []", symbol)
    return []
