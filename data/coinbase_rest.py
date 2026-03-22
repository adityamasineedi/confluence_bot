"""Coinbase Advanced Trade REST client — spot price reference and order book."""
import os
import aiohttp

_BASE_URL = "https://api.coinbase.com/api/v3/brokerage"
_API_KEY = os.environ.get("COINBASE_API_KEY", "")
_API_SECRET = os.environ.get("COINBASE_API_SECRET", "")


async def get_best_bid_ask(product_id: str) -> dict:
    """Fetch best bid/ask for a spot product (e.g., BTC-USD).

    Endpoint: GET /best_bid_ask?product_ids=BTC-USD

    TODO: implement authenticated or public GET request
    TODO: return {"bid": float, "ask": float, "mid": float}
    """
    # TODO: async with aiohttp.ClientSession() as s:
    # TODO:     resp = await s.get(f"{_BASE_URL}/best_bid_ask", params={"product_ids": product_id})
    # TODO:     data = (await resp.json())["pricebooks"][0]
    # TODO:     return {"bid": float(data["bids"][0]["price"]), "ask": float(data["asks"][0]["price"])}
    return {"bid": 0.0, "ask": 0.0, "mid": 0.0}


async def get_spot_price(product_id: str) -> float:
    """Return current spot mid-price for a product.

    Used to compute perp/spot basis against Binance futures price.

    TODO: call get_best_bid_ask and return mid
    """
    data = await get_best_bid_ask(product_id)
    return data.get("mid", 0.0)


async def refresh_cache(symbols: list[str], cache) -> None:
    """Periodically refresh spot prices in cache for basis computation.

    TODO: map BTCUSDT -> BTC-USD etc.
    TODO: fetch spot prices and write to cache
    TODO: run every 30 seconds as background task
    """
    for symbol in symbols:
        # TODO: product_id = map_to_coinbase(symbol)
        # TODO: price = await get_spot_price(product_id)
        # TODO: await cache.set(f"spot:{symbol}", price)
        pass
