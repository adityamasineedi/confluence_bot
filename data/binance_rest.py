"""Binance Futures REST poller + order execution client."""
import asyncio
import hashlib
import hmac
import logging
import os
import time
import urllib.parse
from datetime import datetime

import aiohttp

from .cache import DataCache

log = logging.getLogger(__name__)

_BINANCE_BASE      = os.environ.get("BINANCE_BASE_URL",  "https://fapi.binance.com")
_IS_DEMO           = "demo-fapi" in _BINANCE_BASE
# Separate base for public data endpoints (klines, OI, funding).
# Defaults to live fapi so kline history always resolves even when
# BINANCE_BASE_URL points to the demo host for order operations.
_BINANCE_DATA_BASE = os.environ.get("BINANCE_DATA_URL", "https://fapi.binance.com")
_BYBIT_BASE   = "https://api.bybit.com"
_OKX_BASE     = "https://www.okx.com"

# Binance Futures price tick decimals (from PRICE_FILTER tickSize)
_PRICE_DECIMALS: dict[str, int] = {
    "BTCUSDT":  1,
    "ETHUSDT":  2,
    "SOLUSDT":  2,
    "BNBUSDT":  2,
    "XRPUSDT":  4,
    "LINKUSDT": 3,
    "DOGEUSDT": 5,
    "SUIUSDT":  4,
    "ADAUSDT":  4,
    "AVAXUSDT": 2,
    "TAOUSDT":  2,
}

# Binance Futures quantity step decimals (from LOT_SIZE stepSize)
_QTY_DECIMALS: dict[str, int] = {
    "BTCUSDT":  3,
    "ETHUSDT":  3,
    "SOLUSDT":  1,
    "BNBUSDT":  2,
    "XRPUSDT":  0,
    "LINKUSDT": 1,
    "DOGEUSDT": 0,
    "SUIUSDT":  0,
    "ADAUSDT":  0,
    "AVAXUSDT": 1,
    "TAOUSDT":  2,
}


def _round_price(symbol: str, price: float) -> float:
    dp = _PRICE_DECIMALS.get(symbol.upper(), 2)
    return round(price, dp)


def _round_qty(symbol: str, qty: float) -> float:
    dp = _QTY_DECIMALS.get(symbol.upper(), 3)
    rounded = round(qty, dp)
    return int(rounded) if dp == 0 else rounded

_POLL_INTERVAL_S = 60
_REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=10)

# OKX instrument ID: BTCUSDT → BTC-USDT-SWAP
_OKX_SYMBOL_MAP: dict[str, str] = {
    "BTCUSDT":  "BTC-USDT-SWAP",
    "ETHUSDT":  "ETH-USDT-SWAP",
    "SOLUSDT":  "SOL-USDT-SWAP",
    "BNBUSDT":  "BNB-USDT-SWAP",
    "XRPUSDT":  "XRP-USDT-SWAP",
}

# Read API credentials from environment — never hardcode.
# Can be overridden at runtime via configure_credentials() when using
# the exchange manager UI.
_API_KEY = os.environ.get("BINANCE_API_KEY", "")
_SECRET  = os.environ.get("BINANCE_SECRET", "")


def configure_credentials(api_key: str, api_secret: str,
                          base_url: str | None = None) -> None:
    """Override module-level API credentials (called from exchange manager)."""
    global _API_KEY, _SECRET, _BINANCE_BASE, _IS_DEMO
    _API_KEY = api_key
    _SECRET = api_secret
    if base_url:
        _BINANCE_BASE = base_url
        _IS_DEMO = "demo-fapi" in _BINANCE_BASE
    log.info("Binance credentials configured via exchange manager (key=%s...)",
             api_key[:6] if len(api_key) > 6 else "***")


# ── Signed request helper ─────────────────────────────────────────────────────

def _sign(params: dict) -> dict:
    """Add timestamp + HMAC-SHA256 signature to a Binance request param dict."""
    params["timestamp"] = int(time.time() * 1000)
    query = urllib.parse.urlencode(params)
    sig = hmac.new(_SECRET.encode(), query.encode(), hashlib.sha256).hexdigest()
    params["signature"] = sig
    return params


# ── Candle parser (shared by REST historical loader) ─────────────────────────

def _parse_kline_row(row: list) -> dict:
    """Convert a raw Binance kline array to a cache-compatible dict."""
    return {
        "ts": int(row[0]),       # open time, Unix ms
        "o":  float(row[1]),
        "h":  float(row[2]),
        "l":  float(row[3]),
        "c":  float(row[4]),
        "v":  float(row[5]),
    }


# ── BinanceRestPoller ─────────────────────────────────────────────────────────

class BinanceRestPoller:
    """Polls Binance Futures (+ Bybit and OKX for cross-exchange OI) every 60 s.

    Per poll cycle, for each symbol:
      - Binance OI          → cache.push_oi()
      - Binance funding     → cache.set_funding_rate()
      - Binance 1w klines   → cache.push_candle() ×5
      - Binance 1d klines   → cache.push_candle() ×60
      - Bybit OI            → cache.push_oi()   (stored alongside Binance OI)
      - OKX OI              → cache.push_oi()   (stored alongside Binance/Bybit)

    Errors on individual endpoints are logged and swallowed; they never crash
    the loop or affect other symbols / endpoints.

    Usage::

        poller = BinanceRestPoller(["BTCUSDT", "ETHUSDT"], cache)
        await poller.run()        # infinite loop; launch as asyncio.create_task()
    """

    def __init__(self, symbols: list[str], cache: DataCache) -> None:
        self._symbols = [s.upper() for s in symbols]
        self._cache   = cache

    # ── Main loop ─────────────────────────────────────────────────────────────

    async def run(self) -> None:
        """Poll all endpoints every _POLL_INTERVAL_S seconds, forever."""
        async with aiohttp.ClientSession(timeout=_REQUEST_TIMEOUT) as session:
            # Bootstrap: load daily + weekly history before the first sleep
            await self._poll_all(session, load_history=True)
            while True:
                await asyncio.sleep(_POLL_INTERVAL_S)
                await self._poll_all(session, load_history=False)

    async def _poll_all(self, session: aiohttp.ClientSession, *, load_history: bool) -> None:
        """Fire all per-symbol polls concurrently."""
        await self._fetch_account_balance(session)
        tasks = [
            self._poll_symbol(session, sym, load_history=load_history)
            for sym in self._symbols
        ]
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _fetch_account_balance(self, session: aiohttp.ClientSession) -> None:
        """GET /fapi/v2/account → cache.set_account_balance() with USDT wallet balance."""
        url = f"{_BINANCE_BASE}/fapi/v2/account"
        try:
            params = _sign({"timestamp": int(time.time() * 1000)})
            async with session.get(url, params=params, headers={"X-MBX-APIKEY": _API_KEY}) as resp:
                resp.raise_for_status()
                data = await resp.json()
            balance = float(data.get("totalWalletBalance", 0))
            if balance > 0:
                self._cache.set_account_balance(balance)
                log.info("Account balance: %.2f USDT", balance)
                # Persist for circuit breaker (reads from DB, not cache)
                try:
                    import sqlite3 as _sq, os as _os
                    from datetime import datetime, timezone
                    db = _os.environ.get("DB_PATH", "confluence_bot.db")
                    with _sq.connect(db) as _c:
                        _c.execute(
                            "INSERT OR REPLACE INTO bot_state(key,value,updated) VALUES(?,?,?)",
                            ("account_balance", str(balance),
                             datetime.now(timezone.utc).isoformat())
                        )
                except Exception:
                    pass
        except Exception as exc:
            log.warning("_fetch_account_balance failed: %s", exc)

    async def _poll_symbol(
        self,
        session: aiohttp.ClientSession,
        symbol: str,
        *,
        load_history: bool,
    ) -> None:
        """Poll all endpoints for a single symbol. Errors are contained here."""
        await asyncio.gather(
            self._fetch_oi(session, symbol),
            self._fetch_funding(session, symbol),
            self._fetch_klines(session, symbol, "1w", 15),
            self._fetch_klines(session, symbol, "1d", 90 if load_history else 2),
            self._fetch_klines(session, symbol, "4h", 210 if load_history else 2),
            self._fetch_klines(session, symbol, "1h", 200 if load_history else 2),
            self._fetch_klines(session, symbol, "15m", 200 if load_history else 2),
            self._fetch_klines(session, symbol, "5m", 200 if load_history else 2),
            self._fetch_klines(session, symbol, "1m", 200 if load_history else 2),
            self._fetch_bybit_oi(session, symbol),
            self._fetch_okx_oi(session, symbol),
            return_exceptions=True,
        )

        if load_history:
            # Skip full reload if cache already has data (e.g. after internet blip)
            existing = self._cache.get_ohlcv(symbol, window=5, tf="5m")
            if existing and len(existing) >= 10:
                log.info("Cache intact for %s (%d bars) — skipping history reload",
                         symbol, len(existing))
                return

            # Gap fill — catches any bars missed since last startup
            for tf in ("5m", "15m", "1h", "4h"):
                await self._fill_gap(session, symbol, tf)

            log.info(
                "History loaded %s: 5m=%d  15m=%d  1h=%d  4h=%d",
                symbol,
                len(self._cache.get_ohlcv(symbol, 200, "5m") or []),
                len(self._cache.get_ohlcv(symbol, 200, "15m") or []),
                len(self._cache.get_ohlcv(symbol, 200, "1h") or []),
                len(self._cache.get_ohlcv(symbol, 100, "4h") or []),
            )

        # Compute synthetic liq clusters from OHLCV swing pivots (free alternative
        # to CoinGlass paid-tier liquidation heatmap). This enables check_liq_sweep()
        # and provides approximate stop-cluster levels for signal scoring.
        self._update_synthetic_liq_clusters(symbol)

    # ── Binance endpoints ─────────────────────────────────────────────────────

    async def _fetch_oi(self, session: aiohttp.ClientSession, symbol: str) -> None:
        """GET /fapi/v1/openInterest → cache.push_oi()"""
        url = f"{_BINANCE_DATA_BASE}/fapi/v1/openInterest"
        try:
            async with session.get(url, params={"symbol": symbol}) as resp:
                resp.raise_for_status()
                data = await resp.json()
            ts = int(data["time"])
            oi = float(data["openInterest"])
            self._cache.push_oi(symbol, ts, oi, exchange="binance")
            log.debug("OI %s: %.2f", symbol, oi)
        except Exception as exc:
            log.warning("_fetch_oi(%s) failed: %s", symbol, exc)

    async def _fetch_funding(self, session: aiohttp.ClientSession, symbol: str) -> None:
        """GET /fapi/v1/fundingRate → cache.set_funding_rate()"""
        url = f"{_BINANCE_DATA_BASE}/fapi/v1/fundingRate"
        try:
            async with session.get(url, params={"symbol": symbol, "limit": 1}) as resp:
                resp.raise_for_status()
                data = await resp.json()
            rate = float(data[0]["fundingRate"])
            self._cache.set_funding_rate(symbol, rate)
            log.debug("Funding %s: %.6f", symbol, rate)
        except Exception as exc:
            log.warning("_fetch_funding(%s) failed: %s", symbol, exc)

    async def _fill_gap(
        self,
        session: aiohttp.ClientSession,
        symbol: str,
        tf: str,
    ) -> None:
        """Fetch bars from the last cached bar up to now.

        Ensures no gap exists after a restart regardless of how long
        the bot was offline.
        """
        bars = self._cache.get_ohlcv(symbol, window=5, tf=tf)
        if not bars:
            return  # nothing loaded yet — initial load covers this

        last_ts_ms = bars[-1]["ts"]
        now_ms = int(time.time() * 1000)

        _TF_MS = {
            "1m":  60_000,
            "5m":  300_000,
            "15m": 900_000,
            "1h":  3_600_000,
            "4h":  14_400_000,
            "1d":  86_400_000,
            "1w":  604_800_000,
        }
        ms_per_bar = _TF_MS.get(tf, 60_000)

        bars_missing = int((now_ms - last_ts_ms) / ms_per_bar)
        if bars_missing <= 1:
            return  # already up to date

        bars_missing = min(bars_missing + 5, 500)  # cap at 500, add 5 buffer
        log.info("Gap fill %s %s: fetching %d bars since %s",
                 symbol, tf, bars_missing,
                 datetime.fromtimestamp(last_ts_ms / 1000).strftime("%H:%M"))

        await self._fetch_klines(
            session, symbol, tf,
            limit=bars_missing,
            start_ms=last_ts_ms,
        )

    async def _fetch_klines(
        self,
        session: aiohttp.ClientSession,
        symbol: str,
        interval: str,
        limit: int,
        start_ms: int | None = None,
    ) -> None:
        """GET /fapi/v1/klines → cache.push_candle() for each row."""
        url = f"{_BINANCE_DATA_BASE}/fapi/v1/klines"
        params: dict = {"symbol": symbol, "interval": interval, "limit": limit}
        if start_ms is not None:
            params["startTime"] = start_ms
        try:
            async with session.get(url, params=params) as resp:
                resp.raise_for_status()
                rows = await resp.json()
            for row in rows:
                self._cache.push_candle(symbol, interval, _parse_kline_row(row))
            log.debug("Klines %s %s: loaded %d candles", symbol, interval, len(rows))
        except Exception as exc:
            log.warning("_fetch_klines(%s, %s) failed: %s", symbol, interval, exc)

    # ── Synthetic liquidation clusters ───────────────────────────────────────

    def _update_synthetic_liq_clusters(self, symbol: str) -> None:
        """Derive liq clusters from 4H OHLCV swing pivots.

        Real liq clusters (CoinGlass paid) show where leveraged stops concentrate.
        This approximation uses the classic observation that stops accumulate at
        recent swing highs (shorts stop out above) and swing lows (longs stop out
        below).  A 5-bar pivot rule identifies pivots; volume × price gives a
        rough cluster size in USDT.

        Clusters are refreshed every poll cycle (~60 s) using the current 4H data.
        """
        try:
            candles = self._cache.get_ohlcv(symbol, window=60, tf="4h")
            if len(candles) < 10:
                return

            clusters: list[dict] = []
            # 5-bar pivot: centre bar must be the local extremum over ±2 neighbours
            for i in range(2, len(candles) - 2):
                bar   = candles[i]
                hi    = bar["h"]
                lo    = bar["l"]
                price = bar["c"]
                size  = bar["v"] * price  # approximate USDT notional

                is_swing_high = (
                    hi >= candles[i - 1]["h"] and hi >= candles[i - 2]["h"] and
                    hi >= candles[i + 1]["h"] and hi >= candles[i + 2]["h"]
                )
                is_swing_low = (
                    lo <= candles[i - 1]["l"] and lo <= candles[i - 2]["l"] and
                    lo <= candles[i + 1]["l"] and lo <= candles[i + 2]["l"]
                )

                if is_swing_high:
                    clusters.append({"price": hi,  "size_usd": size, "side": "sell"})
                if is_swing_low:
                    clusters.append({"price": lo, "size_usd": size, "side": "buy"})

            if clusters:
                self._cache.set_liq_clusters(symbol, clusters)
                log.debug("Synthetic liq clusters %s: %d pivots", symbol, len(clusters))
        except Exception as exc:
            log.debug("_update_synthetic_liq_clusters(%s) failed: %s", symbol, exc)

    # ── Cross-exchange OI ─────────────────────────────────────────────────────

    async def _fetch_bybit_oi(self, session: aiohttp.ClientSession, symbol: str) -> None:
        """GET Bybit linear OI → cache.push_oi() alongside Binance readings."""
        url = f"{_BYBIT_BASE}/v5/market/open-interest"
        params = {
            "symbol":       symbol,       # BTCUSDT maps directly to Bybit linear
            "intervalTime": "1h",
            "category":     "linear",
            "limit":        1,
        }
        try:
            async with session.get(url, params=params) as resp:
                resp.raise_for_status()
                body = await resp.json()
            entry = body["result"]["list"][0]
            oi  = float(entry["openInterest"])
            ts  = int(entry["timestamp"])
            # Store with the same symbol key; OI series mixes exchange snapshots.
            # Signal functions use the series for trend/direction, not absolute level.
            self._cache.push_oi(symbol, ts, oi, exchange="bybit")
            log.debug("Bybit OI %s: %.2f", symbol, oi)
        except Exception as exc:
            log.debug("_fetch_bybit_oi(%s) failed: %s", symbol, exc)

    async def _fetch_okx_oi(self, session: aiohttp.ClientSession, symbol: str) -> None:
        """GET OKX SWAP OI → cache.push_oi() (oi field is in contracts × coin)."""
        okx_id = _OKX_SYMBOL_MAP.get(symbol)
        if not okx_id:
            return
        url = f"{_OKX_BASE}/api/v5/public/open-interest"
        try:
            async with session.get(
                url, params={"instType": "SWAP", "instId": okx_id}
            ) as resp:
                resp.raise_for_status()
                body = await resp.json()
            entry = body["data"][0]
            oi = float(entry["oiCcy"])   # OI in base coin units (e.g. BTC)
            ts = int(entry["ts"])
            self._cache.push_oi(symbol, ts, oi, exchange="okx")
            log.debug("OKX OI %s: %.4f", symbol, oi)
        except Exception as exc:
            log.debug("_fetch_okx_oi(%s) failed: %s", symbol, exc)


# ── Order retry helper ────────────────────────────────────────────────────────

async def _place_with_retry(
    session:      aiohttp.ClientSession,
    url:          str,
    params:       dict,
    headers:      dict,
    label:        str,
    max_attempts: int = 3,
) -> dict:
    """Place an order with retry on timeout or rate limit.

    `params` must be the UNSIGNED base params dict — this function calls _sign()
    on each attempt so the timestamp stays fresh across retries.
    """
    for attempt in range(1, max_attempts + 1):
        try:
            async with session.post(
                url, params=_sign(dict(params)), headers=headers
            ) as resp:
                result = await resp.json()
            if isinstance(result.get("code"), int) and result["code"] < 0:
                if result["code"] in (-1003, -1015):   # rate-limit codes
                    await asyncio.sleep(5 * attempt)
                    continue
                log.warning("%s rejected (attempt %d): %s", label, attempt, result)
                if attempt < max_attempts:
                    await asyncio.sleep(2)
                    continue
            return result
        except asyncio.TimeoutError:
            log.warning("%s timeout (attempt %d/%d)", label, attempt, max_attempts)
            if attempt < max_attempts:
                await asyncio.sleep(2 * attempt)
        except Exception as exc:
            log.warning("%s error (attempt %d): %s", label, attempt, exc)
            if attempt < max_attempts:
                await asyncio.sleep(2)
    return {}


# ── Order execution (signed — used by executor.py) ───────────────────────────

async def setup_symbols(symbols: list[str], leverage: int, margin_type: str = "ISOLATED") -> None:
    """Set leverage and margin type for all symbols on startup."""
    url_margin   = f"{_BINANCE_BASE}/fapi/v1/marginType"
    url_leverage = f"{_BINANCE_BASE}/fapi/v1/leverage"
    headers = {"X-MBX-APIKEY": _API_KEY}
    async with aiohttp.ClientSession(timeout=_REQUEST_TIMEOUT) as session:
        for sym in symbols:
            try:
                async with session.post(url_margin, params=_sign({"symbol": sym, "marginType": margin_type}), headers=headers) as r:
                    data = await r.json()
                    code = data.get("code", 0)
                    if code not in (200, -4046):   # -4046 = already set
                        log.warning("marginType %s %s: %s", sym, margin_type, data)
            except Exception as exc:
                log.debug("setup_symbols marginType %s: %s", sym, exc)
            try:
                async with session.post(url_leverage, params=_sign({"symbol": sym, "leverage": leverage}), headers=headers) as r:
                    data = await r.json()
                    if data.get("leverage") != leverage:
                        log.warning("leverage %s → %s: %s", sym, leverage, data)
            except Exception as exc:
                log.debug("setup_symbols leverage %s: %s", sym, exc)
    log.info("Symbol setup complete: leverage=%dx  margin=%s  symbols=%s", leverage, margin_type, symbols)


async def get_position_amt(symbol: str) -> float:
    """Return current position size for symbol (positive=LONG, negative=SHORT, 0=flat)."""
    url = f"{_BINANCE_BASE}/fapi/v2/positionRisk"
    params = _sign({"symbol": symbol})
    headers = {"X-MBX-APIKEY": _API_KEY}
    try:
        async with aiohttp.ClientSession(timeout=_REQUEST_TIMEOUT) as session:
            async with session.get(url, params=params, headers=headers) as resp:
                resp.raise_for_status()
                positions = await resp.json()
        for p in positions:
            if p.get("symbol") == symbol:
                return float(p.get("positionAmt", 0))
    except Exception as exc:
        log.debug("get_position_amt(%s): %s", symbol, exc)
    return 0.0


async def fetch_all_positions() -> list[dict]:
    """Fetch all open positions from Binance Futures.

    Returns list of dicts with: symbol, direction, size, entry, mark_price,
    unrealized_pnl, leverage, margin_type.
    Only returns positions with non-zero size.
    """
    url = f"{_BINANCE_BASE}/fapi/v2/positionRisk"
    params = _sign({})
    headers = {"X-MBX-APIKEY": _API_KEY}
    try:
        async with aiohttp.ClientSession(timeout=_REQUEST_TIMEOUT) as session:
            async with session.get(url, params=params, headers=headers) as resp:
                resp.raise_for_status()
                positions = await resp.json()
        result = []
        for p in positions:
            amt = float(p.get("positionAmt", 0))
            if abs(amt) < 1e-8:
                continue
            result.append({
                "symbol": p.get("symbol", ""),
                "direction": "LONG" if amt > 0 else "SHORT",
                "size": abs(amt),
                "entry": float(p.get("entryPrice", 0)),
                "mark_price": float(p.get("markPrice", 0)),
                "unrealized_pnl": float(p.get("unRealizedProfit", 0)),
                "leverage": int(p.get("leverage", 1)),
                "margin_type": p.get("marginType", "").upper(),
            })
        return result
    except Exception as exc:
        log.warning("fetch_all_positions failed: %s", exc)
        return []


async def refresh_account_balance() -> float:
    """Fetch balance from Binance and update cache + DB immediately.

    Call after a trade closes to ensure position_size() compounds correctly.
    Returns the new balance, or 0.0 on failure (cache keeps last known value).
    """
    url = f"{_BINANCE_BASE}/fapi/v2/account"
    try:
        params = _sign({"timestamp": int(time.time() * 1000)})
        async with aiohttp.ClientSession(timeout=_REQUEST_TIMEOUT) as session:
            async with session.get(url, params=params,
                                   headers={"X-MBX-APIKEY": _API_KEY}) as resp:
                resp.raise_for_status()
                data = await resp.json()
        balance = float(data.get("totalWalletBalance", 0))
        if balance > 0:
            from data.cache import _global_cache
            if _global_cache is not None:
                _global_cache.set_account_balance(balance)
            # Persist for circuit breaker
            try:
                import sqlite3 as _sq
                from datetime import datetime, timezone
                db = os.environ.get("DB_PATH", "confluence_bot.db")
                with _sq.connect(db) as _c:
                    _c.execute(
                        "INSERT OR REPLACE INTO bot_state(key,value,updated) VALUES(?,?,?)",
                        ("account_balance", str(balance),
                         datetime.now(timezone.utc).isoformat())
                    )
            except Exception:
                pass
            log.info("Balance refreshed after trade: %.2f USDT", balance)
        return balance
    except Exception as exc:
        log.warning("refresh_account_balance failed: %s", exc)
        return 0.0


async def get_account_balance() -> float:
    """Fetch available USDT balance from Binance Futures account."""
    url = f"{_BINANCE_BASE}/fapi/v2/balance"
    params = _sign({})
    headers = {"X-MBX-APIKEY": _API_KEY}
    try:
        async with aiohttp.ClientSession(timeout=_REQUEST_TIMEOUT) as session:
            async with session.get(url, params=params, headers=headers) as resp:
                resp.raise_for_status()
                data = await resp.json()
        for asset in data:
            if asset.get("asset") == "USDT":
                return float(asset["availableBalance"])
    except Exception as exc:
        log.error("get_account_balance failed: %s", exc)
    return 0.0


# ── Demo/live order type helpers ──────────────────────────────────────────────
log.info("Order mode: %s — SL/TP via /fapi/v1/algoOrder (BINANCE_BASE_URL=%s)",
         "DEMO" if _IS_DEMO else "LIVE",
         os.environ.get("BINANCE_BASE_URL", ""))


def _make_sl_params(symbol: str, close_side: str, quantity: float, stop: float) -> dict:
    """Build SL params for the Algo Order API (POST /fapi/v1/algoOrder).

    Since 2025-12-09 Binance migrated all conditional order types
    (STOP, STOP_MARKET, TAKE_PROFIT, TAKE_PROFIT_MARKET, TRAILING_STOP_MARKET)
    to the Algo Service.  The old /fapi/v1/order endpoint returns -4120.
    """
    return {
        "algoType":     "CONDITIONAL",
        "symbol":       symbol,
        "side":         close_side,
        "type":         "STOP_MARKET",
        "quantity":     quantity,
        "triggerPrice": _round_price(symbol, stop),
        "reduceOnly":   "true",
        "workingType":  "MARK_PRICE",
    }


def _make_tp_params(symbol: str, close_side: str, quantity: float, take_profit: float) -> dict:
    """Build TP params for the Algo Order API (POST /fapi/v1/algoOrder)."""
    return {
        "algoType":     "CONDITIONAL",
        "symbol":       symbol,
        "side":         close_side,
        "type":         "TAKE_PROFIT_MARKET",
        "quantity":     quantity,
        "triggerPrice": _round_price(symbol, take_profit),
        "reduceOnly":   "true",
        "workingType":  "MARK_PRICE",
    }


async def place_order(
    symbol: str,
    side: str,
    quantity: float,
    entry: float,
    stop: float,
    take_profit: float,
) -> dict:
    """Place entry + SL + TP bracket on Binance Futures.

    Args:
        symbol:       e.g. "BTCUSDT"
        side:         "BUY" or "SELL"
        quantity:     position size in base currency
        entry:        0.0 → MARKET; > 0 → LIMIT at this price
        stop:         stop-loss trigger price
        take_profit:  take-profit trigger price

    Returns order confirmation dict from the entry leg, or {} on failure.
    """
    headers = {"X-MBX-APIKEY": _API_KEY}
    close_side = "SELL" if side == "BUY" else "BUY"

    # Round quantity and prices to Binance-required precision
    quantity = _round_qty(symbol, quantity)
    if entry > 0.0:
        entry = _round_price(symbol, entry)

    entry_params: dict = {
        "symbol":   symbol,
        "side":     side,
        "type":     "MARKET" if entry == 0.0 else "LIMIT",
        "quantity": quantity,
    }
    if entry > 0.0:
        entry_params["price"]    = entry
        entry_params["timeInForce"] = "GTC"

    url = f"{_BINANCE_BASE}/fapi/v1/order"
    result: dict = {}
    try:
        async with aiohttp.ClientSession(timeout=_REQUEST_TIMEOUT) as session:
            # 1. Entry order
            async with session.post(
                url, params=_sign(entry_params), headers=headers
            ) as resp:
                resp.raise_for_status()
                result = await resp.json()
            # Binance returns HTTP 200 with code<0 on API-level errors
            if isinstance(result.get("code"), int) and result["code"] < 0:
                log.error("place_order entry rejected %s %s: %s", side, symbol, result)
                return {}

            # Determine actual filled quantity for bracket orders
            orig_qty   = float(result.get("origQty",     quantity))
            filled_qty = float(result.get("executedQty", 0))

            if filled_qty <= 0:
                log.warning("Entry unfilled for %s %s (executedQty=0) — skipping bracket",
                            side, symbol)
                return result

            if filled_qty < orig_qty * 0.95:
                log.warning("Partial fill %s %s: filled %.4f of %.4f requested",
                            side, symbol, filled_qty, orig_qty)

            bracket_qty = _round_qty(symbol, filled_qty)
            result["executedQty"] = filled_qty

            sl_params = _make_sl_params(symbol, close_side, bracket_qty, stop)
            tp_params = _make_tp_params(symbol, close_side, bracket_qty, take_profit)
            algo_url = f"{_BINANCE_BASE}/fapi/v1/algoOrder"
            sl_placed = False
            tp_placed = False

            # 2. Stop loss — TIER 1: algo order (preferred)
            sl_resp = await _place_with_retry(
                session, algo_url, sl_params, headers, f"SL {symbol}"
            )
            if sl_resp.get("algoId"):
                sl_placed = True
                log.info("SL placed (algo) %s: algoId=%s", symbol, sl_resp["algoId"])
            else:
                sl_code = sl_resp.get("code", "?")
                sl_msg = sl_resp.get("msg", "?")
                log.warning("SL algo rejected %s: code=%s msg=%s — trying STOP_MARKET fallback",
                            symbol, sl_code, sl_msg)

                # TIER 2: standard STOP_MARKET fallback
                try:
                    sm_params = _sign({
                        "symbol":      symbol,
                        "side":        close_side,
                        "type":        "STOP_MARKET",
                        "quantity":    bracket_qty,
                        "stopPrice":   _round_price(symbol, stop),
                        "reduceOnly":  "true",
                        "workingType": "MARK_PRICE",
                    })
                    async with session.post(
                        f"{_BINANCE_BASE}/fapi/v1/order",
                        params=sm_params, headers=headers,
                    ) as sm_resp:
                        sm_data = await sm_resp.json()
                    if sm_data.get("orderId") and not (
                        isinstance(sm_data.get("code"), int) and sm_data["code"] < 0
                    ):
                        sl_placed = True
                        log.info("SL placed (STOP_MARKET fallback) %s: orderId=%s",
                                 symbol, sm_data["orderId"])
                    else:
                        log.warning("SL STOP_MARKET also rejected %s: code=%s msg=%s",
                                    symbol, sm_data.get("code", "?"), sm_data.get("msg", "?"))
                except Exception as sm_exc:
                    log.warning("SL STOP_MARKET fallback error %s: %s", symbol, sm_exc)

            # TIER 3: emergency abort if SL could not be placed by any method
            if not sl_placed:
                log.error(
                    "ABORT %s %s: could not place SL by any method — "
                    "cancelling entry to avoid naked position", side, symbol,
                )
                await cancel_all_orders(symbol)
                # Flatten any open position from the entry fill
                try:
                    flatten_params = _sign({
                        "symbol":     symbol,
                        "side":       close_side,
                        "type":       "MARKET",
                        "quantity":   bracket_qty,
                        "reduceOnly": "true",
                    })
                    async with session.post(
                        f"{_BINANCE_BASE}/fapi/v1/order",
                        params=flatten_params, headers=headers,
                    ) as flat_resp:
                        flat_data = await flat_resp.json()
                    log.info("Emergency flatten %s %s: %s", close_side, symbol,
                             flat_data.get("orderId", flat_data))
                except Exception as flat_exc:
                    log.critical(
                        "NAKED POSITION: %s %s qty=%.4f — flatten failed: %s. "
                        "MANUAL CLOSE REQUIRED on exchange immediately.",
                        side, symbol, bracket_qty, flat_exc,
                    )
                    try:
                        from notifications.telegram import send_text
                        import asyncio as _aio
                        _aio.create_task(send_text(
                            f"🚨 NAKED POSITION: {side} {symbol} qty={bracket_qty} — "
                            f"no SL, flatten failed. Close manually NOW."
                        ))
                    except Exception:
                        pass
                return {}

            # 3. Take profit — via Algo Order API (soft failure OK, software TP covers it)
            tp_resp = await _place_with_retry(
                session, algo_url, tp_params, headers, f"TP {symbol}"
            )
            if tp_resp.get("algoId"):
                tp_placed = True
                log.info("TP placed (algo) %s: algoId=%s", symbol, tp_resp["algoId"])
            elif isinstance(tp_resp.get("code"), int) and int(tp_resp["code"]) < 0:
                log.warning("TP order rejected %s: code=%s msg=%s — software TP will protect position",
                            symbol, tp_resp["code"], tp_resp.get("msg", "?"))

            result["sl_placed_on_exchange"] = sl_placed
            result["tp_placed_on_exchange"] = tp_placed

        if bracket_qty < orig_qty:
            log.warning("Bracket placed for %.4f (filled) not %.4f (requested) — %s %s",
                        bracket_qty, orig_qty, side, symbol)
        log.info("Order placed: %s %s qty=%.4f entry=%s orderId=%s",
                 side, symbol, bracket_qty, entry or "MARKET", result.get("orderId", "?"))
    except Exception as exc:
        log.error("place_order(%s %s) failed: %s", side, symbol, exc)

    return result


async def get_order_status(symbol: str, order_id: int) -> str:
    """Return order status string ('FILLED', 'NEW', 'PARTIALLY_FILLED', etc.) or '' on error."""
    detail = await _get_order_detail(symbol, order_id)
    return detail.get("status", "")


async def _get_order_detail(symbol: str, order_id: int) -> dict:
    """Return full order detail dict from Binance, or {} on error."""
    url     = f"{_BINANCE_BASE}/fapi/v1/order"
    headers = {"X-MBX-APIKEY": _API_KEY}
    params  = _sign({"symbol": symbol, "orderId": order_id})
    try:
        async with aiohttp.ClientSession(timeout=_REQUEST_TIMEOUT) as session:
            async with session.get(url, params=params, headers=headers) as resp:
                resp.raise_for_status()
                return await resp.json()
    except Exception as exc:
        log.debug("_get_order_detail(%s, %s): %s", symbol, order_id, exc)
        return {}


async def place_limit_then_market(
    symbol: str,
    side: str,
    quantity: float,
    limit_price: float,
    stop: float,
    take_profit: float | None,
    timeout_s: float = 30.0,
) -> dict:
    """Place entry order with full protection against ghost trades.

    Flow:
      1. Place LIMIT at limit_price
      2. Wait timeout_s (default 30s)
      3. Check fill → if unfilled, cancel LIMIT and try MARKET
      4. Verify position actually exists on exchange
      5. Only then place SL + TP bracket orders
      6. If anything fails → cancel ALL orders for this symbol (cleanup)

    Returns order dict with executedQty > 0 on success, or {} on failure.
    """
    headers    = {"X-MBX-APIKEY": _API_KEY}
    close_side = "SELL" if side == "BUY" else "BUY"
    quantity   = _round_qty(symbol, quantity)
    url        = f"{_BINANCE_BASE}/fapi/v1/order"
    result: dict = {}

    async with aiohttp.ClientSession(timeout=_REQUEST_TIMEOUT) as session:
        # ── Step 1: place LIMIT entry ─────────────────────────────────────
        try:
            entry_params: dict = {
                "symbol": symbol, "side": side, "type": "MARKET",
                "quantity": quantity,
            }
            async with session.post(url, params=_sign(entry_params), headers=headers) as resp:
                resp.raise_for_status()
                result = await resp.json()
            if isinstance(result.get("code"), int) and result["code"] < 0:
                log.error("LIMIT entry rejected %s %s: %s", side, symbol, result)
                return {}
            order_id = result.get("orderId")
            log.info("LIMIT entry placed %s %s @ %.4f orderId=%s",
                     side, symbol, limit_price, order_id)
        except Exception as exc:
            log.error("LIMIT entry failed (%s %s): %s", side, symbol, exc)
            return {}

        # ── Step 2: wait for fill (market = instant)
        await asyncio.sleep(2)

        # ── Step 3: check fill status ─────────────────────────────────────
        order_detail = await _get_order_detail(symbol, order_id)
        status     = order_detail.get("status", "")
        filled_qty = float(order_detail.get("executedQty", 0))
        result["executedQty"] = filled_qty

        if status not in ("FILLED", "PARTIALLY_FILLED"):
            # Cancel unfilled LIMIT and try MARKET
            await cancel_order(symbol, order_id)
            log.info("LIMIT unfilled after %.0fs (%s) — MARKET fallback (%s %s)",
                     timeout_s, status, side, symbol)
            try:
                mkt_params: dict = {
                    "symbol": symbol, "side": side, "type": "MARKET",
                    "quantity": quantity,
                }
                async with session.post(url, params=_sign(mkt_params), headers=headers) as resp:
                    resp.raise_for_status()
                    result = await resp.json()
                if isinstance(result.get("code"), int) and result["code"] < 0:
                    log.error("MARKET rejected %s %s: %s", side, symbol, result)
                    return {}
                filled_qty = float(result.get("executedQty", 0))
                result["executedQty"] = filled_qty
                log.info("MARKET filled %s %s qty=%.4f", side, symbol, filled_qty)
            except Exception as exc:
                log.error("MARKET failed (%s %s): %s", side, symbol, exc)
                return {}

        # ── Step 4: verify fill is real ───────────────────────────────────
        if filled_qty <= 0:
            log.warning("Entry qty=0 for %s %s — no trade", side, symbol)
            return {}

        # Double-check: does the position actually exist on the exchange?
        await asyncio.sleep(1)  # small delay for exchange to settle
        try:
            pos_amt = await get_position_amt(symbol)
            if abs(pos_amt) < 0.0001:
                log.warning("PHANTOM FILL: %s %s reports qty=%.4f but position=0 — "
                            "cancelling all orders", side, symbol, filled_qty)
                await cancel_all_orders(symbol)
                return {}
            log.info("Position verified: %s %s qty=%.4f", side, symbol, pos_amt)
        except Exception as exc:
            log.debug("Position verify failed %s: %s — proceeding", symbol, exc)

        bracket_qty = _round_qty(symbol, filled_qty)
        result["executedQty"] = filled_qty

        if filled_qty < quantity * 0.95:
            log.warning("Partial fill %s %s: %.4f of %.4f",
                        side, symbol, filled_qty, quantity)

        # ── Step 5: place SL — three-tier with emergency abort ────────
        algo_url = f"{_BINANCE_BASE}/fapi/v1/algoOrder"
        sl_placed = False
        tp_placed = False

        # TIER 1: algo order (preferred)
        sl_params = _make_sl_params(symbol, close_side, bracket_qty, stop)
        try:
            async with session.post(algo_url, params=_sign(sl_params), headers=headers) as resp:
                sl_resp = await resp.json()
            if sl_resp.get("algoId"):
                sl_placed = True
                log.info("SL placed (algo) %s: algoId=%s", symbol, sl_resp["algoId"])
            else:
                log.warning("SL algo rejected %s: code=%s msg=%s — trying STOP_MARKET fallback",
                            symbol, sl_resp.get("code", "?"), sl_resp.get("msg", "?"))
        except Exception as exc:
            log.warning("SL algo error %s: %s — trying STOP_MARKET fallback", symbol, exc)

        # TIER 2: standard STOP_MARKET fallback
        if not sl_placed:
            try:
                sm_params = _sign({
                    "symbol":      symbol,
                    "side":        close_side,
                    "type":        "STOP_MARKET",
                    "quantity":    bracket_qty,
                    "stopPrice":   _round_price(symbol, stop),
                    "reduceOnly":  "true",
                    "workingType": "MARK_PRICE",
                })
                async with session.post(
                    f"{_BINANCE_BASE}/fapi/v1/order",
                    params=sm_params, headers=headers,
                ) as sm_resp:
                    sm_data = await sm_resp.json()
                if sm_data.get("orderId") and not (
                    isinstance(sm_data.get("code"), int) and sm_data["code"] < 0
                ):
                    sl_placed = True
                    log.info("SL placed (STOP_MARKET fallback) %s: orderId=%s",
                             symbol, sm_data["orderId"])
                else:
                    log.warning("SL STOP_MARKET also rejected %s: code=%s msg=%s",
                                symbol, sm_data.get("code", "?"), sm_data.get("msg", "?"))
            except Exception as sm_exc:
                log.warning("SL STOP_MARKET fallback error %s: %s", symbol, sm_exc)

        # TIER 3: emergency abort — no SL means naked position
        if not sl_placed:
            log.error(
                "ABORT %s %s: could not place SL by any method — "
                "cancelling entry to avoid naked position", side, symbol,
            )
            await cancel_all_orders(symbol)
            try:
                flatten_params = _sign({
                    "symbol":     symbol,
                    "side":       close_side,
                    "type":       "MARKET",
                    "quantity":   bracket_qty,
                    "reduceOnly": "true",
                })
                async with session.post(
                    f"{_BINANCE_BASE}/fapi/v1/order",
                    params=flatten_params, headers=headers,
                ) as flat_resp:
                    flat_data = await flat_resp.json()
                log.info("Emergency flatten %s %s: %s", close_side, symbol,
                         flat_data.get("orderId", flat_data))
            except Exception as flat_exc:
                log.critical(
                    "NAKED POSITION: %s %s qty=%.4f — flatten failed: %s. "
                    "MANUAL CLOSE REQUIRED on exchange immediately.",
                    side, symbol, bracket_qty, flat_exc,
                )
                try:
                    from notifications.telegram import send_text
                    import asyncio as _aio
                    _aio.create_task(send_text(
                        f"🚨 NAKED POSITION: {side} {symbol} qty={bracket_qty} — "
                        f"no SL, flatten failed. Close manually NOW."
                    ))
                except Exception:
                    pass
            return {}

        # ── Step 6: place TP — algo order only (soft failure OK) ─────
        if take_profit is not None:
            tp_params = _make_tp_params(symbol, close_side, bracket_qty, take_profit)
            try:
                async with session.post(algo_url, params=_sign(tp_params), headers=headers) as resp:
                    tp_resp = await resp.json()
                if tp_resp.get("algoId"):
                    tp_placed = True
                    log.info("TP placed (algo) %s: algoId=%s", symbol, tp_resp["algoId"])
                elif isinstance(tp_resp.get("code"), int) and int(tp_resp["code"]) < 0:
                    log.warning("TP order rejected %s: code=%s msg=%s — software TP will protect position",
                                symbol, tp_resp["code"], tp_resp.get("msg", "?"))
            except Exception as exc:
                log.warning("TP failed %s: %s — software TP will protect position", symbol, exc)

        result["sl_placed_on_exchange"] = sl_placed
        result["tp_placed_on_exchange"] = tp_placed

    return result


async def cancel_order(symbol: str, order_id: int) -> dict:
    """Cancel an open order by ID."""
    url     = f"{_BINANCE_BASE}/fapi/v1/order"
    headers = {"X-MBX-APIKEY": _API_KEY}
    params  = _sign({"symbol": symbol, "orderId": order_id})
    try:
        async with aiohttp.ClientSession(timeout=_REQUEST_TIMEOUT) as session:
            async with session.delete(url, params=params, headers=headers) as resp:
                resp.raise_for_status()
                return await resp.json()
    except Exception as exc:
        log.error("cancel_order(%s, %s) failed: %s", symbol, order_id, exc)
        return {}


async def cancel_all_orders(symbol: str) -> int:
    """Cancel ALL open orders (regular + algo) for a symbol. Returns count cancelled."""
    headers = {"X-MBX-APIKEY": _API_KEY}
    cancelled = 0
    async with aiohttp.ClientSession(timeout=_REQUEST_TIMEOUT) as session:
        # 1. Cancel regular orders (batch)
        try:
            url = f"{_BINANCE_BASE}/fapi/v1/allOpenOrders"
            params = _sign({"symbol": symbol})
            async with session.delete(url, params=params, headers=headers) as resp:
                if resp.status == 200:
                    cancelled += 1
                    log.info("Cancelled all regular orders for %s", symbol)
        except Exception as exc:
            log.debug("cancel_all_orders regular %s: %s", symbol, exc)

        # 2. Cancel algo orders — try batch first, fall back to individual cancel
        try:
            url = f"{_BINANCE_BASE}/fapi/v1/allOpenAlgoOrders"
            params = _sign({"symbol": symbol})
            async with session.delete(url, params=params, headers=headers) as resp:
                result = await resp.json()
                if resp.status == 200 and result.get("code", 0) != -5000:
                    cancelled += 1
                    log.info("Cancelled all algo orders (batch) for %s", symbol)
                else:
                    # Batch cancel not supported (demo API) — cancel individually
                    list_params = _sign({"symbol": symbol})
                    async with session.get(
                        f"{_BINANCE_BASE}/fapi/v1/openAlgoOrders",
                        params=list_params, headers=headers,
                    ) as lr:
                        algo_data = await lr.json()
                    algo_orders = algo_data.get("orders", []) if isinstance(algo_data, dict) else []
                    for ao in algo_orders:
                        if ao.get("symbol") != symbol:
                            continue
                        aid = ao.get("algoId")
                        if not aid:
                            continue
                        try:
                            cp = _sign({"symbol": symbol, "algoId": aid})
                            async with session.delete(
                                f"{_BINANCE_BASE}/fapi/v1/algoOrder",
                                params=cp, headers=headers,
                            ) as cr:
                                await cr.json()
                            cancelled += 1
                        except Exception:
                            pass
                    if algo_orders:
                        log.info("Cancelled %d algo orders (individual) for %s",
                                 len(algo_orders), symbol)
        except Exception as exc:
            log.debug("cancel_all_orders algo %s: %s", symbol, exc)
    return cancelled


async def place_trailing_stop(
    symbol:         str,
    side:           str,      # "SELL" for LONG position, "BUY" for SHORT
    quantity:       float,
    activation_pct: float,    # unused by Binance directly, kept for API clarity
    callback_pct:   float,    # trailing distance % behind high watermark (0.1–5.0)
) -> dict:
    """Place a Binance TRAILING_STOP_MARKET order on Futures.

    Parameters
    ----------
    callback_pct : trailing callback rate as a percentage (e.g. 1.0 = 1%).
                   Clamped to Binance's accepted range [0.1, 5.0].
    activation_pct : informational only — Binance activates the trail from
                   the moment the order is placed when no activationPrice is set.

    Returns the Binance order dict, or {} on rejection/failure.
    """
    headers = {"X-MBX-APIKEY": _API_KEY}
    qty = _round_qty(symbol, quantity)
    params = _sign({
        "algoType":     "CONDITIONAL",
        "symbol":       symbol,
        "side":         side,
        "type":         "TRAILING_STOP_MARKET",
        "quantity":     qty,
        "callbackRate": round(max(0.1, min(5.0, callback_pct)), 1),
        "reduceOnly":   "true",
        "workingType":  "MARK_PRICE",
    })
    url = f"{_BINANCE_BASE}/fapi/v1/algoOrder"
    try:
        async with aiohttp.ClientSession(timeout=_REQUEST_TIMEOUT) as session:
            async with session.post(url, params=params, headers=headers) as resp:
                result = await resp.json()
        if isinstance(result.get("code"), int) and int(result["code"]) < 0:
            log.warning("place_trailing_stop rejected %s: %s", symbol, result)
            return {}
        log.info("Trailing stop placed %s side=%s callback=%.1f%% algoId=%s",
                 symbol, side, callback_pct, result.get("algoId"))
        return result
    except Exception as exc:
        log.error("place_trailing_stop %s failed: %s", symbol, exc)
        return {}


async def get_btc_dominance() -> float:
    """Fetch BTC market cap dominance % from CoinGecko global endpoint.

    Returns dominance as a float 0.0–1.0 (e.g. 0.56 = 56%).
    Returns 0.0 on failure so callers can skip the dominance gate.
    """
    url = "https://api.coingecko.com/api/v3/global"
    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=5)
        ) as session:
            async with session.get(url) as resp:
                resp.raise_for_status()
                data = await resp.json()
        return float(data["data"]["market_cap_percentage"].get("btc", 0)) / 100.0
    except Exception as exc:
        log.debug("get_btc_dominance failed: %s", exc)
        return 0.0
