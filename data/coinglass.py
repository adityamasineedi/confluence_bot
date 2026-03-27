"""Coinglass REST client — V4 API — Open Interest, Funding Rate, Liquidation data.

HOBBYIST plan ($30/mo) endpoints used:
  /api/futures/coins-markets                          — OI snapshot + current funding (all coins)
  /api/futures/open-interest/history                  — OI history (min 4h interval on HOBBYIST)
  /api/futures/global-long-short-account-ratio/history — global L/S ratio history
  /api/futures/liquidation/map                        — price-level liquidation clusters

V4 base URL: https://open-api-v4.coinglass.com
Auth header: CG-API-KEY

OI history at 1h is STANDARD plan only — use interval=4h (6 readings = 24h trend).
"""
import logging
import os

import aiohttp

log = logging.getLogger(__name__)

_BASE_URL  = "https://open-api-v4.coinglass.com"
_API_KEY   = os.environ.get("COINGLASS_API_KEY", "")
_TIMEOUT   = aiohttp.ClientTimeout(total=10)
_POLL_SECS = 300  # refresh every 5 minutes (stay well within rate limits)

# Short symbol map used for coins-markets response matching
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


def _cg_short(symbol: str) -> str:
    """Return short ticker (BTC) from pair notation (BTCUSDT)."""
    return _SYMBOL_MAP.get(symbol.upper(), symbol.upper().replace("USDT", ""))


# ── Low-level GET helper ───────────────────────────────────────────────────────

async def _get(session: aiohttp.ClientSession, path: str, params: dict) -> dict | None:
    """GET a Coinglass V4 endpoint. Returns parsed JSON or None on failure."""
    url     = f"{_BASE_URL}{path}"
    headers = {"CG-API-KEY": _API_KEY} if _API_KEY else {}
    try:
        async with session.get(url, params=params, headers=headers) as resp:
            if resp.status in (401, 403):
                log.debug("Coinglass: invalid or missing API key — skipping")
                return None
            resp.raise_for_status()
            data = await resp.json()
            # V4 uses code "0" for success
            if isinstance(data, dict) and str(data.get("code", "0")) != "0":
                log.debug("Coinglass V4 error: %s  path=%s", data.get("msg"), path)
                return None
            return data
    except aiohttp.ClientError as exc:
        log.debug("Coinglass GET %s failed: %s", path, exc)
        return None


# ── Public fetch functions ────────────────────────────────────────────────────

async def get_coins_markets() -> list[dict]:
    """Fetch current OI + funding snapshot for all coins (one call).

    Returns list of raw dicts with keys including:
      symbol              — short ticker e.g. "BTC"
      open_interest_usd   — current OI in USD
      avg_funding_rate_by_oi — current funding rate (float)
    Returns [] without API key or on failure.
    """
    if not _API_KEY:
        return []
    async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
        data = await _get(session, "/api/futures/coins-markets", {"per_page": 50})
    if not data:
        return []
    try:
        return data["data"] or []
    except (KeyError, TypeError):
        log.debug("Coinglass coins-markets: unexpected response shape")
        return []


async def get_open_interest(symbol: str, interval: str = "4h", limit: int = 6) -> list[dict]:
    """Fetch aggregated OI history.

    HOBBYIST plan supports interval=4h, 8h, 12h, 1d only.
    Default 6 readings × 4h = 24h trend window.

    Returns list of {t: unix_ms, o: oi_float} dicts (oldest → newest).
    Returns [] without API key or on failure.
    """
    if not _API_KEY:
        return []
    async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
        data = await _get(session, "/api/futures/open-interest/history", {
            "exchange": "Binance",
            "symbols":  symbol.upper(),
            "interval": interval,
            "limit":    limit,
        })
    if not data:
        return []
    try:
        rows = data["data"]
        if not rows:
            return []
        # V4 shape: [{t/time: unix_ms, o/openInterest: float}, ...]
        result = []
        for r in rows:
            t = r.get("t") or r.get("time") or r.get("createTime")
            o = r.get("o") or r.get("openInterest") or r.get("openInterestUsd")
            if t is not None and o is not None:
                result.append({"t": int(t), "o": float(o)})
        return result
    except (KeyError, TypeError, ValueError):
        log.debug("Coinglass OI history: unexpected response shape for %s", symbol)
        return []


async def get_funding_rate(symbol: str, limit: int = 3) -> list[dict]:
    """Fetch funding rate from coins-markets snapshot (most recent only).

    Returns list of {t: unix_ms, r: rate_float} dicts. For HOBBYIST plan,
    only the current rate is available via coins-markets.
    Returns [] without API key or on failure.
    """
    if not _API_KEY:
        return []
    markets = await get_coins_markets()
    short = _cg_short(symbol)
    for item in markets:
        if str(item.get("symbol", "")).upper() == short.upper():
            try:
                rate = float(item.get("avg_funding_rate_by_oi", 0) or 0)
                import time as _time
                return [{"t": int(_time.time() * 1000), "r": rate}]
            except (TypeError, ValueError):
                return []
    return []


async def get_long_short_ratio(symbol: str, interval: str = "4h", limit: int = 3) -> list[dict]:
    """Fetch global long/short account ratio history.

    HOBBYIST plan: use interval=4h or larger.
    Returns list of {t: unix_ms, ls: float} dicts (oldest → newest).
    ls > 1.0 means more longs than shorts globally.
    Returns [] without API key or on failure.
    """
    if not _API_KEY:
        return []
    async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
        data = await _get(session, "/api/futures/global-long-short-account-ratio/history", {
            "exchange": "Binance",
            "symbol":   symbol.upper(),
            "interval": interval,
            "limit":    limit,
        })
    if not data:
        return []
    try:
        rows = data["data"]
        if not rows:
            return []
        result = []
        for r in rows:
            t  = r.get("time") or r.get("t") or r.get("createTime")
            ls = r.get("global_account_long_short_ratio") or r.get("longShortRatio")
            if t is not None and ls is not None:
                result.append({"t": int(t), "ls": float(ls)})
        return result
    except (KeyError, TypeError, ValueError):
        log.debug("Coinglass L/S ratio: unexpected response shape for %s", symbol)
        return []


async def get_liquidations(symbol: str, interval: str = "4h", limit: int = 6) -> list[dict]:
    """Fetch liquidation volume history (aggregated, not price-level).

    Returns list of {t: unix_ms, long_liq: float, short_liq: float} dicts.
    Returns [] without API key or on failure.
    """
    if not _API_KEY:
        return []
    async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
        data = await _get(session, "/api/futures/liquidation/history", {
            "exchange": "Binance",
            "symbol":   symbol.upper(),
            "interval": interval,
            "limit":    limit,
        })
    if not data:
        return []
    try:
        rows = data["data"]
        if not rows:
            return []
        result = []
        for row in rows:
            t          = row.get("t") or row.get("time") or row.get("createTime")
            long_liq   = float(row.get("longLiquidationUsd",  row.get("buy_usd",  0)) or 0)
            short_liq  = float(row.get("shortLiquidationUsd", row.get("sell_usd", 0)) or 0)
            if t is not None:
                result.append({"t": int(t), "long_liq": long_liq, "short_liq": short_liq})
        return result
    except (KeyError, TypeError, ValueError):
        log.debug("Coinglass liquidations: unexpected response shape for %s", symbol)
        return []


async def get_liq_heatmap_clusters(symbol: str, range_type: str = "1d") -> list[dict]:
    """Fetch real liquidation heatmap price clusters.

    V4 endpoint: /api/futures/liquidation/map
    range_type: '12h', '1d', '3d', '7d', '30d'

    Returns list of {price: float, long_liq_usd: float, short_liq_usd: float} dicts
    sorted by total notional descending. Returns [] without API key.

    V4 response structure:
      data.data = {"<price_level>": [[exact_price, usd_amount, leverage_pct, timeframe], ...], ...}

    Price levels above current price → short liquidation clusters (shorts get squeezed up).
    Price levels below current price → long liquidation clusters (longs get stopped down).
    """
    if not _API_KEY:
        return []
    async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
        data = await _get(session, "/api/futures/liquidation/map", {
            "exchange": "Binance",
            "symbol":   symbol.upper(),
            "range":    range_type,
        })
    if not data:
        return []
    try:
        # Navigate: data → data → {price_level_str: [[price, usd, pct, tf], ...]}
        inner = data.get("data", {})
        if isinstance(inner, dict) and "data" in inner:
            inner = inner["data"]
        if not isinstance(inner, dict):
            return []

        # Compute median price to split long/short sides
        price_levels = [float(k) for k in inner.keys()]
        if not price_levels:
            return []
        price_levels.sort()
        mid = price_levels[len(price_levels) // 2]

        clusters = []
        for price_str, entries in inner.items():
            price_level = float(price_str)
            total_usd   = sum(float(e[1]) for e in entries if len(e) >= 2)
            if total_usd <= 0:
                continue
            # Levels below midpoint: long liquidations (longs stopped out if price drops here)
            # Levels above midpoint: short liquidations (shorts squeezed if price rises here)
            if price_level < mid:
                clusters.append({
                    "price":         price_level,
                    "long_liq_usd":  total_usd,
                    "short_liq_usd": 0.0,
                })
            else:
                clusters.append({
                    "price":         price_level,
                    "long_liq_usd":  0.0,
                    "short_liq_usd": total_usd,
                })

        clusters.sort(key=lambda x: x["long_liq_usd"] + x["short_liq_usd"], reverse=True)
        return clusters

    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        log.debug("Coinglass liq heatmap: unexpected response shape for %s: %s", symbol, exc)
        return []


# ── Cache refresh loop ────────────────────────────────────────────────────────

async def refresh_cache(symbols: list[str], cache) -> None:
    """Fetch one round of Coinglass data and push to cache.

    Called periodically by main.py via _periodic(refresh_cache, ..., interval=300).
    Only runs if COINGLASS_API_KEY is set.

    Populates:
      - OI history       → cache.push_oi()         (4h readings, 24h window)
      - Funding          → cache.set_funding_rate() (from coins-markets snapshot)
      - L/S ratio        → cache.set_long_short_ratio()
      - Liq clusters     → cache.set_liq_clusters() (real price-level heatmap)
    """
    if not _API_KEY:
        return

    # Fetch OI+funding snapshot for all coins in one API call
    markets_snapshot: dict[str, dict] = {}
    try:
        markets = await get_coins_markets()
        for item in markets:
            short = str(item.get("symbol", "")).upper()
            markets_snapshot[short] = item
    except Exception as exc:
        log.warning("Coinglass coins-markets refresh failed: %s", exc)

    for symbol in symbols:
        try:
            short = _cg_short(symbol)

            # Funding rate from snapshot
            if short in markets_snapshot:
                item = markets_snapshot[short]
                try:
                    rate = float(item.get("avg_funding_rate_by_oi", 0) or 0)
                    cache.set_funding_rate(symbol, rate)
                    log.debug("Coinglass funding %s: %.6f", symbol, rate)
                except (TypeError, ValueError):
                    pass

            # OI history: 6 × 4h = 24h trend window (HOBBYIST plan max resolution)
            oi_history = await get_open_interest(symbol, interval="4h", limit=6)
            for entry in oi_history:
                cache.push_oi(symbol, entry["t"], entry["o"])

            # Long/Short ratio: 3 readings at 4h
            ls_data = await get_long_short_ratio(symbol, interval="4h", limit=3)
            if ls_data:
                cache.set_long_short_ratio(symbol, ls_data[-1]["ls"])
                log.debug("Coinglass L/S %s: %.3f", symbol, ls_data[-1]["ls"])

            # Real liquidation heatmap — price-level clusters (1d range)
            heatmap = await get_liq_heatmap_clusters(symbol, range_type="1d")
            if heatmap:
                # Dynamic min cluster size: 10% of 3rd-largest cluster
                top5_sizes = sorted(
                    [h["long_liq_usd"] + h["short_liq_usd"] for h in heatmap[:5]],
                    reverse=True,
                )
                min_size = top5_sizes[2] * 0.1 if len(top5_sizes) >= 3 else 100_000

                clusters = []
                for h in heatmap[:50]:
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
                    log.debug("Coinglass liq map %s: %d clusters  min_size=$%.0f",
                              symbol, len(clusters), min_size)

        except Exception as exc:
            log.warning("Coinglass refresh(%s) error: %s", symbol, exc)
