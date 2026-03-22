"""Coinglass REST client — Open Interest, Funding Rate, Liquidation data."""
import os
import aiohttp

_BASE_URL = "https://open-api.coinglass.com/public/v2"
_API_KEY = os.environ.get("COINGLASS_API_KEY", "")


async def get_open_interest(symbol: str, interval: str = "h1", limit: int = 48) -> list[dict]:
    """Fetch open interest history from Coinglass.

    Endpoint: GET /indicator/open_interest_history
    Returns list of {t: timestamp, o: oi_value} dicts.

    TODO: implement authenticated GET request
    TODO: handle pagination and rate limits
    TODO: parse and return OI time series
    """
    # TODO: async with aiohttp.ClientSession(headers={"coinglassSecret": _API_KEY}) as s:
    # TODO:     resp = await s.get(f"{_BASE_URL}/indicator/open_interest_history", params={...})
    # TODO:     return await resp.json()
    return []


async def get_funding_rate(symbol: str, limit: int = 8) -> list[dict]:
    """Fetch funding rate history for a symbol.

    Endpoint: GET /indicator/funding_rates_chart
    Returns list of {t: timestamp, r: rate} dicts.

    TODO: implement authenticated GET request
    TODO: parse and return funding rate series
    """
    return []


async def get_liquidations(symbol: str, interval: str = "h1", limit: int = 24) -> list[dict]:
    """Fetch liquidation data (long and short liquidations).

    TODO: implement GET /indicator/liquidation_history
    TODO: return list of {t, long_liq, short_liq} dicts
    """
    return []


async def refresh_cache(symbols: list[str], cache) -> None:
    """Periodically fetch Coinglass data and push to cache.

    TODO: loop over symbols, fetch OI, funding, liquidations
    TODO: write to cache with appropriate keys
    TODO: run every N minutes as a background task
    """
    for symbol in symbols:
        oi = await get_open_interest(symbol)
        funding = await get_funding_rate(symbol)
        # TODO: await cache.set(f"oi:{symbol}", oi)
        # TODO: await cache.set(f"funding:{symbol}", funding)
