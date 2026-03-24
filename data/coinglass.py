"""Coinglass REST client — Open Interest, Funding Rate, Liquidation data.

Free-tier endpoints (no API key required for public endpoints;
authenticated endpoints require COINGLASS_API_KEY in environment).

Free v2 base: https://open-api.coinglass.com/public/v2
    - /indicator/open_interest_history   OI time-series (aggregated)
    - /indicator/funding_rates_chart     Funding rate history
    - /indicator/liquidation_history     Historical liquidation volumes (NOT price levels)

Note: the liquidation-level heatmap (price × OI) is a paid-tier feature.
      Synthetic clusters from OHLCV swing pivots are generated in binance_rest.py
      as a free fallback for check_liq_sweep() / check_liq_grab_short().
"""
import logging
import os

import aiohttp

log = logging.getLogger(__name__)

_BASE_URL   = "https://open-api.coinglass.com/public/v2"
_API_KEY    = os.environ.get("COINGLASS_API_KEY", "")
_TIMEOUT    = aiohttp.ClientTimeout(total=10)
_POLL_SECS  = 300   # refresh every 5 minutes (stay well within free-tier rate limits)

# Coinglass uses short ticker symbols, not pair notation
_SYMBOL_MAP: dict[str, str] = {
    "BTCUSDT": "BTC",
    "ETHUSDT": "ETH",
    "SOLUSDT": "SOL",
    "BNBUSDT": "BNB",
    "XRPUSDT": "XRP",
}


def _cg_symbol(symbol: str) -> str:
    return _SYMBOL_MAP.get(symbol.upper(), symbol.upper().replace("USDT", ""))


# ── Low-level GET helper ───────────────────────────────────────────────────────

async def _get(session: aiohttp.ClientSession, path: str, params: dict) -> dict | None:
    """GET a Coinglass endpoint. Returns the parsed JSON or None on failure."""
    url     = f"{_BASE_URL}{path}"
    headers = {"coinglassSecret": _API_KEY} if _API_KEY else {}
    try:
        async with session.get(url, params=params, headers=headers) as resp:
            if resp.status == 401:
                log.debug("Coinglass: no API key or invalid key — skipping authenticated endpoint")
                return None
            resp.raise_for_status()
            return await resp.json()
    except aiohttp.ClientError as exc:
        log.debug("Coinglass GET %s failed: %s", path, exc)
        return None


# ── Public fetch functions ────────────────────────────────────────────────────

async def get_open_interest(symbol: str, interval: str = "h1", limit: int = 48) -> list[dict]:
    """Fetch aggregated OI history.

    Returns list of {t: unix_ms, o: oi_float} dicts (oldest → newest).
    Requires COINGLASS_API_KEY; returns [] without it.
    """
    if not _API_KEY:
        return []
    async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
        data = await _get(session, "/indicator/open_interest_history", {
            "symbol":   _cg_symbol(symbol),
            "interval": interval,
            "limit":    limit,
        })
    if not data:
        return []
    try:
        agg = data["data"]["aggregated"]
        return [{"t": int(row["t"]), "o": float(row["o"])} for row in agg]
    except (KeyError, TypeError, ValueError):
        log.debug("Coinglass OI: unexpected response shape for %s", symbol)
        return []


async def get_funding_rate(symbol: str, limit: int = 8) -> list[dict]:
    """Fetch funding rate history.

    Returns list of {t: unix_ms, r: rate_float} dicts (oldest → newest).
    Requires COINGLASS_API_KEY; returns [] without it.
    """
    if not _API_KEY:
        return []
    async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
        data = await _get(session, "/indicator/funding_rates_chart", {
            "symbol": _cg_symbol(symbol),
            "limit":  limit,
        })
    if not data:
        return []
    try:
        rows = data["data"]
        return [{"t": int(r["t"]), "r": float(r["r"])} for r in rows]
    except (KeyError, TypeError, ValueError):
        log.debug("Coinglass funding: unexpected response shape for %s", symbol)
        return []


async def get_liquidations(symbol: str, interval: str = "h1", limit: int = 24) -> list[dict]:
    """Fetch liquidation volume history (NOT price-level heatmap).

    Returns list of {t: unix_ms, long_liq: float, short_liq: float} dicts.
    Requires COINGLASS_API_KEY; returns [] without it.
    """
    if not _API_KEY:
        return []
    async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
        data = await _get(session, "/indicator/liquidation_history", {
            "symbol":   _cg_symbol(symbol),
            "interval": interval,
            "limit":    limit,
        })
    if not data:
        return []
    try:
        agg = data["data"]["aggregated"]
        return [
            {
                "t":         int(row["t"]),
                "long_liq":  float(row.get("buyLiquidationUsd",  0)),
                "short_liq": float(row.get("sellLiquidationUsd", 0)),
            }
            for row in agg
        ]
    except (KeyError, TypeError, ValueError):
        log.debug("Coinglass liquidations: unexpected response shape for %s", symbol)
        return []


# ── Cache refresh loop ────────────────────────────────────────────────────────

async def refresh_cache(symbols: list[str], cache) -> None:
    """Fetch one round of Coinglass data and push to cache.

    Called periodically by main.py via _periodic(refresh_cache, ..., interval=300).
    Only runs if COINGLASS_API_KEY is set.

    Populates:
      - OI history → cache.push_oi() (supplements Binance + Bybit + OKX OI series)
      - Funding     → cache.set_funding_rate() (supplements Binance funding)

    Liquidation price-level heatmap is NOT available on the free tier.
    Synthetic liq clusters are generated from OHLCV in BinanceRestPoller instead.
    """
    if not _API_KEY:
        return

    for symbol in symbols:
        try:
            oi_history = await get_open_interest(symbol, interval="h1", limit=2)
            for entry in oi_history:
                cache.push_oi(symbol, entry["t"], entry["o"])

            funding = await get_funding_rate(symbol, limit=1)
            if funding:
                cache.set_funding_rate(symbol, funding[-1]["r"])

        except Exception as exc:
            log.warning("Coinglass refresh(%s) error: %s", symbol, exc)
