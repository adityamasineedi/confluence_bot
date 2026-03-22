"""Whale flow signal — large aggTrade imbalance as trend long confirmation."""

_WHALE_NOTIONAL_USD = 500_000    # single trade notional threshold for "whale"
_NET_BUY_THRESHOLD  = 2_000_000  # whale_buys - whale_sells must exceed $2 M
_WINDOW_SECONDS     = 3_600      # look back 1 hour of aggTrades


def check_whale_flow(symbol: str, cache) -> bool:
    """True when whale net buying exceeds $2 M over the last hour.

    A trade is "whale" if its notional value (price × qty) > $500 k.
    Net flow = whale_buys_usd - whale_sells_usd.
    is_buyer_maker=False  → aggressive buy (taker hit the ask).
    is_buyer_maker=True   → aggressive sell (taker hit the bid).
    """
    trades = cache.get_agg_trades(symbol, window_seconds=_WINDOW_SECONDS)
    if not trades:
        return False

    whale_buys  = 0.0
    whale_sells = 0.0

    for t in trades:
        notional = t["price"] * t["qty"]
        if notional < _WHALE_NOTIONAL_USD:
            continue
        if t["is_buyer_maker"]:
            whale_sells += notional
        else:
            whale_buys  += notional

    return (whale_buys - whale_sells) > _NET_BUY_THRESHOLD
