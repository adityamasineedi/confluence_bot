"""Order book imbalance signals — L2 bid/ask wall detection.

Uses the @depth5 WebSocket snapshot (top 5 price levels).

Bullish wall: total bid qty within _WALL_PCT of price >= _IMBALANCE_RATIO × ask qty
Bearish wall: total ask qty within _WALL_PCT of price >= _IMBALANCE_RATIO × bid qty

Both signals return False gracefully when no L2 snapshot has arrived yet.
"""

# Window around current price to sum bid/ask volume (0.5%)
_WALL_PCT         = 0.005
# Bid/ask volume ratio to qualify as a wall (2:1)
_IMBALANCE_RATIO  = 2.0
# Max age of the order book snapshot before we treat it as stale (seconds)
_MAX_STALE_S      = 5.0


def _imbalance(symbol: str, cache) -> tuple[float, float]:
    """Return (bid_qty_near, ask_qty_near) within _WALL_PCT of current price.

    Returns (0.0, 0.0) if no book or stale snapshot.
    """
    import time
    book = cache.get_order_book(symbol)
    if not book:
        return 0.0, 0.0
    if time.time() - book.get("ts", 0) > _MAX_STALE_S:
        return 0.0, 0.0

    price = cache.get_last_price(symbol)
    if price == 0.0:
        return 0.0, 0.0

    threshold = price * _WALL_PCT
    bid_qty = sum(qty for p, qty in book.get("bids", []) if abs(p - price) <= threshold)
    ask_qty = sum(qty for p, qty in book.get("asks", []) if abs(p - price) <= threshold)
    return bid_qty, ask_qty


def check_order_book_bid_wall(symbol: str, cache) -> bool:
    """Large bid wall near price — buy-side absorption, bullish pressure.

    True when bid qty >= 2× ask qty within 0.5% of current price.
    """
    bid_qty, ask_qty = _imbalance(symbol, cache)
    if ask_qty == 0.0:
        return False
    return bid_qty / ask_qty >= _IMBALANCE_RATIO


def check_order_book_ask_wall(symbol: str, cache) -> bool:
    """Large ask wall near price — sell-side absorption, bearish pressure.

    True when ask qty >= 2× bid qty within 0.5% of current price.
    """
    bid_qty, ask_qty = _imbalance(symbol, cache)
    if bid_qty == 0.0:
        return False
    return ask_qty / bid_qty >= _IMBALANCE_RATIO
