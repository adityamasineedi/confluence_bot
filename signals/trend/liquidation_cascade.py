"""Liquidation cascade signals — real-time forced-liquidation events.

Sources the !forceOrder@arr WebSocket stream via cache.get_recent_liquidations().

Binance forceOrder side convention:
  'BUY'  = a SHORT position was liquidated (engine buys to close → bullish pressure)
  'SELL' = a LONG  position was liquidated (engine sells to close → bearish pressure)

Short squeeze (bullish): >= _MIN_LIQ_USD of SHORT liquidations in last _WINDOW_S
Long flush   (bearish): >= _MIN_LIQ_USD of LONG  liquidations in last _WINDOW_S
"""

_WINDOW_S    = 300      # 5-minute rolling window
_MIN_LIQ_USD = 100_000  # $100k minimum notional to qualify


def check_liq_short_squeeze(symbol: str, cache) -> bool:
    """Large SHORT liquidation cascade — forced buying creates upward price pressure.

    True when >= $100k of short positions were force-liquidated in the last 5 min.
    """
    liqs = cache.get_recent_liquidations(symbol, window_seconds=_WINDOW_S)
    if not liqs:
        return False
    # BUY side = short was liquidated
    short_liq_usd = sum(e["qty"] * e["price"] for e in liqs if e["side"] == "BUY")
    return short_liq_usd >= _MIN_LIQ_USD


def check_liq_long_flush(symbol: str, cache) -> bool:
    """Large LONG liquidation cascade — forced selling creates downward price pressure.

    True when >= $100k of long positions were force-liquidated in the last 5 min.
    """
    liqs = cache.get_recent_liquidations(symbol, window_seconds=_WINDOW_S)
    if not liqs:
        return False
    # SELL side = long was liquidated
    long_liq_usd = sum(e["qty"] * e["price"] for e in liqs if e["side"] == "SELL")
    return long_liq_usd >= _MIN_LIQ_USD
