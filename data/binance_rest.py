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

_BINANCE_BASE = "https://fapi.binance.com"
_BYBIT_BASE   = "https://api.bybit.com"
_OKX_BASE     = "https://www.okx.com"

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
        tasks = [
            self._poll_symbol(session, sym, load_history=load_history)
            for sym in self._symbols
        ]
        await asyncio.gather(*tasks, return_exceptions=True)

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
        url = f"{_BINANCE_BASE}/fapi/v1/openInterest"
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
        url = f"{_BINANCE_BASE}/fapi/v1/fundingRate"
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
        url = f"{_BINANCE_BASE}/fapi/v1/klines"
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
        "stopPrice":     stop,
        "reduceOnly":    "true",
    }
    tp_params = {
        "symbol":        symbol,
        "side":          close_side,
        "type":          "TAKE_PROFIT_MARKET",
        "quantity":      quantity,
        "stopPrice":     take_profit,
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

            # 2. Stop loss
            async with session.post(
                url, params=_sign(sl_params), headers=headers
            ) as resp:
                resp.raise_for_status()

            # 3. Take profit
            async with session.post(
                url, params=_sign(tp_params), headers=headers
            ) as resp:
                resp.raise_for_status()

        log.info("Order placed: %s %s qty=%.4f entry=%s", side, symbol, quantity, entry or "MARKET")
    except Exception as exc:
        log.error("place_order(%s %s) failed: %s", side, symbol, exc)

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
