"""CryptoQuant REST client — on-chain exchange flow and reserve data."""
import os
import aiohttp

_BASE_URL = "https://api.cryptoquant.com/v1"
_API_KEY = os.environ.get("CRYPTOQUANT_API_KEY", "")

_HEADERS = {"Authorization": f"Bearer {_API_KEY}"}


async def get_exchange_inflow(symbol: str, window: int = 24) -> list[dict]:
    """Fetch exchange inflow data for a coin.

    Endpoint: GET /btc/exchange-flows/inflow  (adjust path per coin)
    Returns list of {date, inflow_total, inflow_mean} dicts.

    TODO: map symbol to CryptoQuant coin path (e.g., BTC -> btc)
    TODO: implement authenticated GET request
    TODO: return last `window` data points
    """
    # TODO: coin = symbol.lower().replace("usdt", "")
    # TODO: async with aiohttp.ClientSession(headers=_HEADERS) as s:
    # TODO:     resp = await s.get(f"{_BASE_URL}/{coin}/exchange-flows/inflow", params={"window": "hour", "limit": window})
    # TODO:     return (await resp.json())["result"]["data"]
    return []


async def get_exchange_outflow(symbol: str, window: int = 24) -> list[dict]:
    """Fetch exchange outflow data for a coin.

    TODO: similar to get_exchange_inflow but for outflows endpoint
    """
    return []


async def get_exchange_reserve(symbol: str) -> float:
    """Fetch current exchange reserve (total coins on exchanges).

    TODO: GET /{coin}/exchange-flows/reserve
    TODO: return latest reserve value
    """
    return 0.0


async def refresh_cache(symbols: list[str], cache) -> None:
    """Periodically fetch CryptoQuant data and push to cache.

    TODO: loop over symbols, fetch inflow and outflow
    TODO: compute net flow and write to cache
    TODO: run every 15 minutes as a background task
    """
    for symbol in symbols:
        inflow = await get_exchange_inflow(symbol)
        outflow = await get_exchange_outflow(symbol)
        # TODO: await cache.set(f"exchange_inflow:{symbol}", inflow)
        # TODO: await cache.set(f"exchange_outflow:{symbol}", outflow)
