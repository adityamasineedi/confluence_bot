"""Coinglass REST client — Open Interest, Funding Rate, Liquidation data.

Uses Coinglass API v3 (requires COINGLASS_API_KEY).
When key is absent all functions return empty results — bot continues with
synthetic liq clusters from binance_rest.py as fallback.

Key endpoints used:
  /api/futures/liquidation/v1/chart   — real liquidation heatmap (price levels)
  /api/futures/openInterest/chart     — aggregated OI history
  /api/futures/fundingRate/chart      — funding rate history
"""
import logging
import os

import aiohttp

log = logging.getLogger(__name__)

_BASE_URL   = "https://open-api.coinglass.com"
_API_KEY    = os.environ.get("COINGLASS_API_KEY", "")
_TIMEOUT    = aiohttp.ClientTimeout(total=10)
_POLL_SECS  = 300   # refresh every 5 minutes (stay well within rate limits)

# Coinglass uses short ticker symbols, not pair notation
_SYMBOL_MAP: dict[str, str] = {
    "BTCUSDT":  "BTC",
    "ETHUSDT":  "ETH",
    "SOLUSDT":  "SOL",
    "BNBUSDT":  "BNB",
    "XRPUSDT":  "XRP",
    "AVAXUSDT": "AVAX",
    "ADAUSDT":  "ADA",
    "DOTUSDT":  "DOT",
    "DOGEUSDT": "DOGE",
    "SUIUSDT":  "SUI",
    "LINKUSDT": "LINK",
    "NEARUSDT": "NEAR",
    "INJUSDT":  "INJ",
    "ARBUSDT":  "ARB",
    "LTCUSDT":  "LTC",
}


def _cg_symbol(symbol: str) -> str:
    return _SYMBOL_MAP.get(symbol.upper(), symbol.upper().replace("USDT", ""))


# ── Low-level GET helper ───────────────────────────────────────────────────────

async def _get(session: aiohttp.ClientSession, path: str, params: dict) -> dict | None:
    """GET a Coinglass v3 endpoint. Returns the parsed JSON or None on failure."""
    url     = f"{_BASE_URL}{path}"
    headers = {"CG-API-KEY": _API_KEY} if _API_KEY else {}
    try:
        async with session.get(url, params=params, headers=headers) as resp:
            if resp.status in (401, 403):
                log.debug("Coinglass: invalid or missing API key — skipping")
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
        data = await _get(session, "/api/futures/openInterest/chart", {
            "symbol":   _cg_symbol(symbol),
            "interval": interval,
            "limit":    limit,
        })
    if not data:
        return []
    try:
        rows = data["data"]
        return [{"t": int(r["t"]), "o": float(r["o"])} for r in rows]
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
        data = await _get(session, "/api/futures/fundingRate/chart", {
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


async def get_long_short_ratio(symbol: str, interval: str = "h1", limit: int = 3) -> list[dict]:
    """Fetch global long/short account ratio.

    Returns list of {t: unix_ms, ls: float} dicts (oldest → newest).
    ls > 1.0 means more longs than shorts globally.
    Requires COINGLASS_API_KEY; returns [] without it.
    """
    if not _API_KEY:
        return []
    async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
        data = await _get(session, "/api/futures/globalLongShortAccountRatio/chart", {
            "symbol":   _cg_symbol(symbol),
            "interval": interval,
            "limit":    limit,
        })
    if not data:
        return []
    try:
        rows = data["data"]
        return [{"t": int(r["t"]), "ls": float(r["longShortRatio"])} for r in rows]
    except (KeyError, TypeError, ValueError):
        log.debug("Coinglass L/S ratio: unexpected response shape for %s", symbol)
        return []


async def get_liquidations(symbol: str, interval: str = "1h", limit: int = 24) -> list[dict]:
    """Fetch liquidation volume history.

    Returns list of {t: unix_ms, long_liq: float, short_liq: float} dicts.
    Requires COINGLASS_API_KEY; returns [] without it.
    """
    if not _API_KEY:
        return []
    async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
        data = await _get(session, "/api/futures/liquidation/v1/chart", {
            "symbol":   _cg_symbol(symbol),
            "interval": interval,
            "limit":    limit,
        })
    if not data:
        return []
    try:
        rows = data["data"]
        return [
            {
                "t":         int(row["t"]),
                "long_liq":  float(row.get("longLiquidationUsd",  0)),
                "short_liq": float(row.get("shortLiquidationUsd", 0)),
            }
            for row in rows
        ]
    except (KeyError, TypeError, ValueError):
        log.debug("Coinglass liquidations: unexpected response shape for %s", symbol)
        return []


async def get_liq_heatmap_clusters(symbol: str, range_type: str = "3") -> list[dict]:
    """Fetch real liquidation heatmap price clusters (requires API key).

    Returns list of {price: float, long_liq_usd: float, short_liq_usd: float} dicts
    sorted by notional size descending. Returns [] without an API key.

    range_type: '1'=1d, '3'=3d, '7'=7d, '30'=30d
    """
    if not _API_KEY:
        return []
    async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
        data = await _get(session, "/api/futures/liquidation/v2/detail", {
            "symbol":    _cg_symbol(symbol),
            "rangeType": range_type,
        })
    if not data:
        return []
    try:
        rows = data["data"]
        clusters = []
        for row in rows:
            price         = float(row["price"])
            long_liq_usd  = float(row.get("longLiquidationUsd",  0))
            short_liq_usd = float(row.get("shortLiquidationUsd", 0))
            if long_liq_usd + short_liq_usd > 0:
                clusters.append({
                    "price":         price,
                    "long_liq_usd":  long_liq_usd,
                    "short_liq_usd": short_liq_usd,
                })
        clusters.sort(key=lambda x: x["long_liq_usd"] + x["short_liq_usd"], reverse=True)
        return clusters
    except (KeyError, TypeError, ValueError):
        log.debug("Coinglass liq heatmap: unexpected response shape for %s", symbol)
        return []


# ── Cache refresh loop ────────────────────────────────────────────────────────

async def refresh_cache(symbols: list[str], cache) -> None:
    """Fetch one round of Coinglass data and push to cache.

    Called periodically by main.py via _periodic(refresh_cache, ..., interval=300).
    Only runs if COINGLASS_API_KEY is set.

    Populates:
      - OI history       → cache.push_oi()
      - Funding          → cache.set_funding_rate()
      - Liq heatmap      → cache.set_liq_clusters() (real price-level clusters)
    """
    if not _API_KEY:
        return

    for symbol in symbols:
        try:
            # OI: fetch 24 hourly readings for trend detection (was 2)
            oi_history = await get_open_interest(symbol, interval="h1", limit=24)
            for entry in oi_history:
                cache.push_oi(symbol, entry["t"], entry["o"])

            # Funding: fetch 3 readings to detect ramping pattern (was 1)
            funding = await get_funding_rate(symbol, limit=3)
            if funding:
                cache.set_funding_rate(symbol, funding[-1]["r"])

            # Long/Short ratio: push to cache for signal use
            ls_data = await get_long_short_ratio(symbol, interval="h1", limit=3)
            if ls_data:
                cache.set_long_short_ratio(symbol, ls_data[-1]["ls"])
                log.debug("Coinglass L/S %s: %.3f", symbol, ls_data[-1]["ls"])

            # Real liquidation heatmap — replaces synthetic OHLCV clusters when key is set
            heatmap = await get_liq_heatmap_clusters(symbol, range_type="3")
            if heatmap:
                # Compute per-symbol min cluster size dynamically:
                # use top-5 cluster median so small-caps don't need $5M clusters
                top5_sizes = sorted(
                    [h["long_liq_usd"] + h["short_liq_usd"] for h in heatmap[:5]],
                    reverse=True,
                )
                min_size = top5_sizes[2] * 0.1 if len(top5_sizes) >= 3 else 100_000

                clusters = []
                for h in heatmap[:50]:   # top 50 price levels by notional
                    total = h["long_liq_usd"] + h["short_liq_usd"]
                    if total < min_size:
                        continue
                    side = "buy" if h["long_liq_usd"] > h["short_liq_usd"] else "sell"
                    clusters.append({
                        "price":    h["price"],
                        "size_usd": total,
                        "side":     side,
                    })
                if clusters:
                    cache.set_liq_clusters(symbol, clusters)
                    log.debug("Coinglass liq heatmap %s: %d clusters  min_size=$%.0f",
                              symbol, len(clusters), min_size)

        except Exception as exc:
            log.warning("Coinglass refresh(%s) error: %s", symbol, exc)
