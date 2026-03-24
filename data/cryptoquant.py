"""Whale flow via large aggTrade heuristic — no paid API key required.

Uses Binance aggTrade data already cached by the WebSocket stream.
Large trades (> $100k notional by default) are treated as institutional flow:

  is_buyer_maker=True  → taker SOLD → sell-initiated = coins flowing TO exchange
  is_buyer_maker=False → taker BOUGHT → buy-initiated = accumulation / outflow

Net inflow proxy:
    net_inflow = large_sell_usd - large_buy_usd

  positive → more large selling → bearish (exchange inflow)
  negative → more large buying  → bullish (accumulation)

Pushed to cache.push_inflow() every call (wired as a 30s periodic in main.py).
"""
import logging
import os
import time

log = logging.getLogger(__name__)

# Minimum USD notional to classify a trade as "large" / whale
_LARGE_TRADE_MIN_USD = float(os.environ.get("LARGE_TRADE_MIN_USD", "100000"))

# Lookback window for scanning large trades
_LOOKBACK_S = 300  # 5 minutes


async def refresh_cache(symbols: list[str], cache) -> None:
    """
    Scan last 5 minutes of aggTrades for each symbol.
    Compute net large-trade inflow proxy and push to cache.
    """
    now_ms = int(time.time() * 1000)

    for symbol in symbols:
        try:
            trades = cache.get_agg_trades(symbol, window_secs=_LOOKBACK_S)
            if not trades:
                continue

            net_buy_usd  = 0.0
            net_sell_usd = 0.0

            for t in trades:
                notional = t["price"] * t["qty"]
                if notional < _LARGE_TRADE_MIN_USD:
                    continue
                # is_buyer_maker=True → taker sold (sell-initiated)
                if t["is_buyer_maker"]:
                    net_sell_usd += notional
                else:
                    net_buy_usd += notional

            total = net_sell_usd + net_buy_usd
            if total == 0:
                continue

            net_inflow = net_sell_usd - net_buy_usd
            cache.push_inflow(symbol, now_ms, net_inflow)

            log.debug(
                "Whale flow %s: net_inflow=%+.0f USD  "
                "(large_sell=%.0f  large_buy=%.0f  threshold=$%.0fk)",
                symbol, net_inflow, net_sell_usd, net_buy_usd,
                _LARGE_TRADE_MIN_USD / 1000,
            )

        except Exception as exc:
            log.debug("refresh_cache(%s) failed: %s", symbol, exc)
