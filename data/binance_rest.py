"""Binance Futures REST poller + order execution client."""
import asyncio
import hashlib
import hmac
import logging
import os
import time
import urllib.parse

import aiohttp

from .cache import DataCache

log = logging.getLogger(__name__)

_BINANCE_BASE      = os.environ.get("BINANCE_BASE_URL",  "https://fapi.binance.com")
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
    "AVAXUSDT": 3,
    "ADAUSDT":  4,
    "DOTUSDT":  3,
    "DOGEUSDT": 5,
    "SUIUSDT":  4,
}

def _round_price(symbol: str, price: float) -> float:
    dp = _PRICE_DECIMALS.get(symbol.upper(), 2)
    return round(price, dp)

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

# Read API credentials from environment — never hardcode
_API_KEY = os.environ.get("BINANCE_API_KEY", "")
_SECRET  = os.environ.get("BINANCE_SECRET", "")


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
            self._fetch_klines(session, symbol, "1w", 5),
            self._fetch_klines(session, symbol, "1d", 60 if load_history else 2),
            # 4H: need 210 bars for EMA200 + ADX warmup on startup; 2 bars to stay current
            self._fetch_klines(session, symbol, "4h", 210 if load_history else 2),
            self._fetch_klines(session, symbol, "1h", 100 if load_history else 2),
            self._fetch_klines(session, symbol, "15m", 30 if load_history else 2),
            self._fetch_bybit_oi(session, symbol),
            self._fetch_okx_oi(session, symbol),
            return_exceptions=True,
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

    async def _fetch_klines(
        self,
        session: aiohttp.ClientSession,
        symbol: str,
        interval: str,
        limit: int,
    ) -> None:
        """GET /fapi/v1/klines → cache.push_candle() for each row."""
        url = f"{_BINANCE_DATA_BASE}/fapi/v1/klines"
        try:
            async with session.get(
                url,
                params={"symbol": symbol, "interval": interval, "limit": limit},
            ) as resp:
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

    # Ensure quantity has no spurious .0 suffix (Binance rejects "18295.0" for step=1 symbols)
    quantity = int(quantity) if quantity == int(quantity) else quantity

    entry_params: dict = {
        "symbol":   symbol,
        "side":     side,
        "type":     "MARKET" if entry == 0.0 else "LIMIT",
        "quantity": quantity,
    }
    if entry > 0.0:
        entry_params["price"]    = entry
        entry_params["timeInForce"] = "GTC"

    sl_params = {
        "symbol":        symbol,
        "side":          close_side,
        "type":          "STOP_MARKET",
        "quantity":      quantity,
        "stopPrice":     _round_price(symbol, stop),
        "reduceOnly":    "true",
    }
    tp_params = {
        "symbol":        symbol,
        "side":          close_side,
        "type":          "TAKE_PROFIT_MARKET",
        "quantity":      quantity,
        "stopPrice":     _round_price(symbol, take_profit),
        "reduceOnly":    "true",
    }

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

            # 2. Stop loss
            async with session.post(
                url, params=_sign(sl_params), headers=headers
            ) as resp:
                sl_resp = await resp.json()
            if isinstance(sl_resp.get("code"), int) and sl_resp["code"] < 0:
                log.warning("SL order rejected %s: code=%s msg=%s — software SL/TP will protect position",
                            symbol, sl_resp["code"], sl_resp.get("msg", "?"))
            else:
                log.debug("SL placed %s: orderId=%s", symbol, sl_resp.get("orderId"))

            # 3. Take profit
            async with session.post(
                url, params=_sign(tp_params), headers=headers
            ) as resp:
                tp_resp = await resp.json()
            if isinstance(tp_resp.get("code"), int) and tp_resp["code"] < 0:
                log.warning("TP order rejected %s: code=%s msg=%s — software SL/TP will protect position",
                            symbol, tp_resp["code"], tp_resp.get("msg", "?"))

        log.info("Order placed: %s %s qty=%.4f entry=%s orderId=%s",
                 side, symbol, quantity, entry or "MARKET", result.get("orderId", "?"))
    except Exception as exc:
        log.error("place_order(%s %s) failed: %s", side, symbol, exc)

    return result


async def get_order_status(symbol: str, order_id: int) -> str:
    """Return order status string ('FILLED', 'NEW', 'PARTIALLY_FILLED', etc.) or '' on error."""
    url     = f"{_BINANCE_BASE}/fapi/v1/order"
    headers = {"X-MBX-APIKEY": _API_KEY}
    params  = _sign({"symbol": symbol, "orderId": order_id})
    try:
        async with aiohttp.ClientSession(timeout=_REQUEST_TIMEOUT) as session:
            async with session.get(url, params=params, headers=headers) as resp:
                resp.raise_for_status()
                data = await resp.json()
        return data.get("status", "")
    except Exception as exc:
        log.debug("get_order_status(%s, %s): %s", symbol, order_id, exc)
        return ""


async def place_limit_then_market(
    symbol: str,
    side: str,
    quantity: float,
    limit_price: float,
    stop: float,
    take_profit: float,
    timeout_s: float = 30.0,
) -> dict:
    """Place a LIMIT entry; fall back to MARKET after timeout_s seconds if unfilled.

    Strategy:
      1. Submit GTC LIMIT at limit_price.
      2. Wait timeout_s seconds.
      3. Check fill status via REST.
      4. If unfilled, cancel the LIMIT and place a MARKET order.
      5. In all paths, place SL + TP conditional orders.

    Returns the entry order confirmation dict, or {} on failure.
    """
    headers   = {"X-MBX-APIKEY": _API_KEY}
    close_side = "SELL" if side == "BUY" else "BUY"
    quantity   = int(quantity) if quantity == int(quantity) else quantity

    entry_params: dict = {
        "symbol":      symbol,
        "side":        side,
        "type":        "LIMIT",
        "quantity":    quantity,
        "price":       _round_price(symbol, limit_price),
        "timeInForce": "GTC",
    }

    url    = f"{_BINANCE_BASE}/fapi/v1/order"
    result: dict = {}

    async with aiohttp.ClientSession(timeout=_REQUEST_TIMEOUT) as session:
        # ── Step 1: place LIMIT entry ──────────────────────────────────────────
        try:
            async with session.post(url, params=_sign(entry_params), headers=headers) as resp:
                resp.raise_for_status()
                result = await resp.json()
            if isinstance(result.get("code"), int) and result["code"] < 0:
                log.error("place_limit_then_market entry rejected %s %s: %s", side, symbol, result)
                return {}
            order_id = result.get("orderId")
            log.info("LIMIT entry placed %s %s @ %.4f orderId=%s", side, symbol, limit_price, order_id)
        except Exception as exc:
            log.error("place_limit_then_market LIMIT failed (%s %s): %s", side, symbol, exc)
            return {}

        # ── Step 2: wait for fill ──────────────────────────────────────────────
        await asyncio.sleep(timeout_s)

        # ── Step 3: check fill status ──────────────────────────────────────────
        status = await get_order_status(symbol, order_id)

        if status not in ("FILLED", "PARTIALLY_FILLED"):
            # ── Step 4: cancel LIMIT and submit MARKET ─────────────────────────
            await cancel_order(symbol, order_id)
            log.info(
                "LIMIT unfilled after %.0fs (%s) — cancelling and switching to MARKET (%s %s)",
                timeout_s, status, side, symbol,
            )
            mkt_params: dict = {
                "symbol":   symbol,
                "side":     side,
                "type":     "MARKET",
                "quantity": quantity,
            }
            try:
                async with session.post(url, params=_sign(mkt_params), headers=headers) as resp:
                    resp.raise_for_status()
                    result = await resp.json()
                if isinstance(result.get("code"), int) and result["code"] < 0:
                    log.error("MARKET fallback rejected %s %s: %s", side, symbol, result)
                    return {}
                log.info("MARKET fallback filled %s %s orderId=%s", side, symbol, result.get("orderId"))
            except Exception as exc:
                log.error("MARKET fallback failed (%s %s): %s", side, symbol, exc)
                return {}

        # ── Step 5: SL + TP ───────────────────────────────────────────────────
        sl_params = {
            "symbol":    symbol,
            "side":      close_side,
            "type":      "STOP_MARKET",
            "quantity":  quantity,
            "stopPrice": _round_price(symbol, stop),
            "reduceOnly": "true",
        }
        tp_params = {
            "symbol":    symbol,
            "side":      close_side,
            "type":      "TAKE_PROFIT_MARKET",
            "quantity":  quantity,
            "stopPrice": _round_price(symbol, take_profit),
            "reduceOnly": "true",
        }
        for label, params in (("SL", sl_params), ("TP", tp_params)):
            try:
                async with session.post(url, params=_sign(params), headers=headers) as resp:
                    r = await resp.json()
                if isinstance(r.get("code"), int) and r["code"] < 0:
                    log.warning("%s rejected %s: code=%s msg=%s", label, symbol, r["code"], r.get("msg"))
                else:
                    log.debug("%s placed %s orderId=%s", label, symbol, r.get("orderId"))
            except Exception as exc:
                log.warning("%s placement failed %s: %s", label, symbol, exc)

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
