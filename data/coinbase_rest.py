"""Coinbase Advanced Trade REST client — spot price for perp/spot basis signal.

The basis signal compares Binance futures price vs Coinbase spot price.
All endpoints used here are public — no API key required.

Spot API: https://api.coinbase.com/api/v3/brokerage/market/products/{product_id}
"""
import asyncio
import logging
import urllib.request
import json as _json

log = logging.getLogger(__name__)

_BASE_URL   = "https://api.coinbase.com/api/v3/brokerage/market/products"
_TIMEOUT_S  = 8
_POLL_SECS  = 30   # refresh every 30 s (called by main.py _periodic)

# BTCUSDT → BTC-USD (Coinbase uses spot pairs, not perpetuals)
_SYMBOL_MAP: dict[str, str] = {
    "BTCUSDT":  "BTC-USD",
    "ETHUSDT":  "ETH-USD",
    "SOLUSDT":  "SOL-USD",
    "BNBUSDT":  "BNB-USD",
    "AVAXUSDT": "AVAX-USD",
    "LINKUSDT": "LINK-USD",
    "ADAUSDT":  "ADA-USD",
    "XRPUSDT":  "XRP-USD",
}


def _fetch_spot(product_id: str) -> float:
    """Blocking fetch of spot mid-price from Coinbase public API."""
    url = f"{_BASE_URL}/{product_id}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "confluence_bot/1.0"})
        with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as resp:
            data = _json.loads(resp.read())
        # Response has price field directly
        price = data.get("price") or data.get("mid_market_price", "0")
        return float(price)
    except Exception as exc:
        log.debug("Coinbase spot fetch %s failed: %s", product_id, exc)
        return 0.0


async def get_spot_price(symbol: str) -> float:
    """Return current Coinbase spot mid-price for a symbol (e.g. BTCUSDT → BTC-USD).

    Returns 0.0 if the symbol is not mapped or the request fails.
    """
    product_id = _SYMBOL_MAP.get(symbol.upper())
    if not product_id:
        return 0.0
    return await asyncio.to_thread(_fetch_spot, product_id)


async def refresh_cache(symbols: list[str], cache) -> None:
    """Fetch one round of spot prices and push basis values to cache.

    Called every 30 s by main.py via _periodic(refresh_cache, ..., interval=30).

    Basis = (futures_price - spot_price) / spot_price
    Stored in cache.push_basis(symbol, basis_value) for check_perp_basis().
    """
    for symbol in symbols:
        try:
            spot = await get_spot_price(symbol)
            if spot <= 0.0:
                continue

            # Futures price from cache (last 1m candle close)
            futures = cache.get_last_price(symbol)
            if futures <= 0.0:
                continue

            basis = (futures - spot) / spot
            cache.push_basis(symbol, basis)
            log.debug("Basis %s: %.6f (futures=%.4f spot=%.4f)", symbol, basis, futures, spot)

        except Exception as exc:
            log.warning("Coinbase refresh(%s) error: %s", symbol, exc)
