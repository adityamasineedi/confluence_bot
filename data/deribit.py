"""Deribit REST client — options data: IV, skew, P/C ratio, OI by strike."""
import os
import aiohttp

_BASE_URL = "https://www.deribit.com/api/v2"
# Deribit public endpoints don't require auth; private endpoints do
_CLIENT_ID = os.environ.get("DERIBIT_CLIENT_ID", "")
_CLIENT_SECRET = os.environ.get("DERIBIT_CLIENT_SECRET", "")


async def get_options_summary(currency: str = "BTC") -> list[dict]:
    """Fetch all active options instruments and their market data.

    Endpoint: GET /public/get_book_summary_by_currency?currency=BTC&kind=option

    TODO: implement GET request to Deribit public API
    TODO: parse and return list of option summaries
    """
    # TODO: async with aiohttp.ClientSession() as s:
    # TODO:     resp = await s.get(f"{_BASE_URL}/public/get_book_summary_by_currency",
    # TODO:                        params={"currency": currency, "kind": "option"})
    # TODO:     return (await resp.json())["result"]
    return []


async def get_iv_surface(currency: str = "BTC") -> dict:
    """Compute IV surface from options data.

    TODO: call get_options_summary and compute IV by strike/expiry matrix
    TODO: return dict keyed by (expiry, delta) with IV values
    """
    return {}


async def get_skew(currency: str = "BTC") -> float:
    """Return 25-delta risk reversal skew (call IV - put IV) for nearest expiry.

    TODO: pull IV for 25-delta call and 25-delta put
    TODO: return difference
    """
    return 0.0


async def get_put_call_ratio(currency: str = "BTC") -> float:
    """Return Put/Call OI ratio for the given currency.

    TODO: sum put OI and call OI from options summary
    TODO: return put_oi / call_oi
    """
    return 1.0


async def refresh_cache(currencies: list[str], cache) -> None:
    """Periodically fetch Deribit options data and push to cache.

    TODO: fetch skew and P/C ratio for each currency
    TODO: write to cache with appropriate keys
    TODO: run every 5 minutes as background task
    """
    for currency in currencies:
        skew = await get_skew(currency)
        pc_ratio = await get_put_call_ratio(currency)
        # TODO: await cache.set(f"skew:{currency}", skew)
        # TODO: await cache.set(f"pc_ratio:{currency}", pc_ratio)
