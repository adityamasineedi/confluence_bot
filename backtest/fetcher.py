"""Historical data fetcher — downloads OHLCV, OI, and funding from Binance.

Uses Binance Futures API for data from futures launch onwards, and falls back
to Binance Spot API for earlier history (BTC/ETH back to 2017, SOL to 2020).
Data is cached to backtest/data/ as JSON files; files < 24 h old are reused.
"""
import asyncio
import json
import logging
import os
import time

import aiohttp

log = logging.getLogger(__name__)

_FUTURES_BASE = "https://fapi.binance.com"
_SPOT_BASE    = "https://api.binance.com"
_TIMEOUT      = aiohttp.ClientTimeout(total=60)
_DATA_DIR     = os.path.join(os.path.dirname(__file__), "data")
_CACHE_TTL    = 172_800  # 48 h in seconds — reduces re-fetch frequency on daily runs

# Candles per timeframe — enough to cover ~9 years
_TF_LIMITS: dict[str, int] = {
    "1m":  500,        # recent only (entry price proxy)
    "5m":  52_000,     # ~180 days for lead-lag backtest (180 × 288 bars/day)
    "15m": 2_880,      # ~30 days for ATR
    "1h":  80_000,     # ~9 years
    "4h":  20_000,     # ~9 years
    "1d":  3_650,      # 10 years
    "1w":  520,        # 10 years
}

_MS = {
    "1m":  60_000,
    "5m":  300_000,
    "15m": 900_000,
    "1h":  3_600_000,
    "4h":  14_400_000,
    "1d":  86_400_000,
    "1w":  604_800_000,
}

# Approximate Binance Futures launch timestamps (ms) per symbol.
# Spot API is used for any history before this date.
_FUTURES_LAUNCH_MS: dict[str, int] = {
    "BTCUSDT":  1568592000000,   # 2019-09-16
    "ETHUSDT":  1574035200000,   # 2019-11-18
    "ADAUSDT":  1577836800000,   # 2020-01-01
    "BNBUSDT":  1581292800000,   # 2020-02-10
    "DOTUSDT":  1607212800000,   # 2020-12-06
    "SOLUSDT":  1607904000000,   # 2020-12-14
    "DOGEUSDT": 1614643200000,   # 2021-03-02
    "AVAXUSDT": 1630454400000,   # 2021-09-01
    "SUIUSDT":  1685059200000,   # 2023-05-26
}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _cache_path(name: str) -> str:
    return os.path.join(_DATA_DIR, f"{name}.json")


def _is_fresh(path: str) -> bool:
    if not os.path.exists(path):
        return False
    return (time.time() - os.path.getmtime(path)) < _CACHE_TTL


def _save(path: str, data) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f)


def _load(path: str):
    with open(path) as f:
        return json.load(f)


def _parse_kline(row: list) -> dict:
    return {
        "ts": int(row[0]),
        "o":  float(row[1]),
        "h":  float(row[2]),
        "l":  float(row[3]),
        "c":  float(row[4]),
        "v":  float(row[5]),
    }


# ── Low-level paginated kline fetcher (URL-agnostic) ─────────────────────────

async def _fetch_klines_from(
    session:  aiohttp.ClientSession,
    url:      str,
    symbol:   str,
    tf:       str,
    total:    int,
    end_ms:   int | None = None,
) -> list[dict]:
    """Fetch up to `total` candles walking backwards from end_ms."""
    candles: list[dict] = []

    while len(candles) < total:
        batch  = min(1500, total - len(candles))
        params: dict = {"symbol": symbol, "interval": tf, "limit": batch}
        if end_ms is not None:
            params["endTime"] = end_ms

        try:
            async with session.get(url, params=params) as resp:
                resp.raise_for_status()
                rows = await resp.json()
        except Exception as exc:
            log.warning("klines fetch failed %s %s: %s", symbol, tf, exc)
            break

        if not rows:
            break

        parsed  = [_parse_kline(r) for r in rows]
        candles = parsed + candles
        end_ms  = parsed[0]["ts"] - 1

        if len(rows) < batch:
            break   # no more history available

        await asyncio.sleep(0.05)

    candles.sort(key=lambda c: c["ts"])
    return candles


async def _fetch_klines(
    session: aiohttp.ClientSession,
    symbol:  str,
    tf:      str,
    total:   int,
) -> list[dict]:
    """Fetch `total` candles, using Futures first then Spot for older data."""
    futures_url = f"{_FUTURES_BASE}/fapi/v1/klines"
    spot_url    = f"{_SPOT_BASE}/api/v3/klines"

    # Step 1 — fetch from futures (returns only from futures launch onwards)
    futures = await _fetch_klines_from(session, futures_url, symbol, tf, total)

    if len(futures) >= total:
        return futures[-total:]

    # Step 2 — supplement with spot data for the pre-futures period
    need   = total - len(futures)
    end_ms = futures[0]["ts"] - 1 if futures else None
    spot   = await _fetch_klines_from(session, spot_url, symbol, tf, need, end_ms=end_ms)

    if not spot:
        return futures[-total:]

    # Merge and deduplicate (spot for old data, futures for newer)
    combined: dict[int, dict] = {}
    for c in spot + futures:
        combined[c["ts"]] = c

    merged = sorted(combined.values(), key=lambda c: c["ts"])
    return merged[-total:]


# ── OI and funding fetchers ───────────────────────────────────────────────────

async def _fetch_oi(
    session: aiohttp.ClientSession,
    symbol:  str,
    total:   int = 8_760,
) -> list[dict]:
    """Fetch hourly open interest history (Binance typically returns ~30 days)."""
    url    = f"{_FUTURES_BASE}/futures/data/openInterestHist"
    result: list[dict] = []
    end_ms: int | None = None

    while len(result) < total:
        batch  = min(500, total - len(result))
        params: dict = {"symbol": symbol, "period": "1h", "limit": batch}
        if end_ms is not None:
            params["endTime"] = end_ms
        try:
            async with session.get(url, params=params) as resp:
                resp.raise_for_status()
                rows = await resp.json()
        except Exception as exc:
            log.warning("OI fetch failed %s: %s", symbol, exc)
            break

        if not rows:
            break

        parsed = [{"ts": int(r["timestamp"]), "oi": float(r["sumOpenInterest"])}
                  for r in rows]
        result = parsed + result
        end_ms = parsed[0]["ts"] - 1

        if len(rows) < batch:
            break

        await asyncio.sleep(0.05)

    result.sort(key=lambda x: x["ts"])
    return result[-total:]


async def _fetch_funding(
    session: aiohttp.ClientSession,
    symbol:  str,
    total:   int = 3_000,
) -> list[dict]:
    """Fetch 8-h funding rate history (up to ~3 years)."""
    url    = f"{_FUTURES_BASE}/fapi/v1/fundingRate"
    result: list[dict] = []
    end_ms: int | None = None

    while len(result) < total:
        batch  = min(1000, total - len(result))
        params: dict = {"symbol": symbol, "limit": batch}
        if end_ms is not None:
            params["endTime"] = end_ms
        try:
            async with session.get(url, params=params) as resp:
                resp.raise_for_status()
                rows = await resp.json()
        except Exception as exc:
            log.warning("funding fetch failed %s: %s", symbol, exc)
            break

        if not rows:
            break

        parsed = [{"ts": int(r["fundingTime"]), "rate": float(r["fundingRate"])}
                  for r in rows]
        result = parsed + result
        end_ms = parsed[0]["ts"] - 1

        if len(rows) < batch:
            break

        await asyncio.sleep(0.05)

    result.sort(key=lambda x: x["ts"])
    return result[-total:]


# ── Per-symbol fetch ──────────────────────────────────────────────────────────

async def _fetch_symbol(
    session: aiohttp.ClientSession,
    symbol:  str,
    force:   bool = False,
) -> dict:
    result: dict = {"ohlcv": {}, "oi": [], "funding": []}

    for tf, limit in _TF_LIMITS.items():
        key  = f"{symbol}_{tf}"
        path = _cache_path(key)
        if not force and _is_fresh(path):
            log.info("  cache hit: %s", key)
            result["ohlcv"][f"{symbol}:{tf}"] = _load(path)
        else:
            log.info("  fetching: %s %s (%d candles)...", symbol, tf, limit)
            candles = await _fetch_klines(session, symbol, tf, limit)
            _save(path, candles)
            result["ohlcv"][f"{symbol}:{tf}"] = candles

    oi_path = _cache_path(f"{symbol}_oi")
    if not force and _is_fresh(oi_path):
        log.info("  cache hit: %s OI", symbol)
        result["oi"] = _load(oi_path)
    else:
        log.info("  fetching: %s OI...", symbol)
        result["oi"] = await _fetch_oi(session, symbol)
        _save(oi_path, result["oi"])

    fund_path = _cache_path(f"{symbol}_funding")
    if not force and _is_fresh(fund_path):
        log.info("  cache hit: %s funding", symbol)
        result["funding"] = _load(fund_path)
    else:
        log.info("  fetching: %s funding...", symbol)
        result["funding"] = await _fetch_funding(session, symbol)
        _save(fund_path, result["funding"])

    return result


# ── Range fetch (specific start→end window) ──────────────────────────────────

async def _fetch_klines_range(
    session:  aiohttp.ClientSession,
    symbol:   str,
    tf:       str,
    start_ms: int,
    end_ms:   int,
) -> list[dict]:
    """Fetch all candles between start_ms and end_ms for a given timeframe."""
    futures_url = f"{_FUTURES_BASE}/fapi/v1/klines"
    spot_url    = f"{_SPOT_BASE}/api/v3/klines"
    candles: dict[int, dict] = {}

    futures_launch = _FUTURES_LAUNCH_MS.get(symbol, 0)

    for url in [futures_url, spot_url]:
        cursor = start_ms
        while cursor < end_ms:
            params: dict = {
                "symbol":    symbol,
                "interval":  tf,
                "startTime": cursor,
                "endTime":   end_ms,
                "limit":     1500,
            }
            rows = None
            for attempt in range(3):
                try:
                    async with session.get(url, params=params) as resp:
                        resp.raise_for_status()
                        rows = await resp.json()
                    break
                except Exception as exc:
                    if attempt == 2:
                        log.warning("range klines failed %s %s after 3 attempts: %s",
                                    symbol, tf, exc)
                    else:
                        await asyncio.sleep(1.5 ** attempt)

            if not rows:
                if not candles:
                    log.warning("range klines: 0 bars returned for %s %s "
                                "(start=%d end=%d) — possible rate-limit or gap",
                                symbol, tf, cursor, end_ms)
                break

            for r in rows:
                bar = _parse_kline(r)
                if start_ms <= bar["ts"] <= end_ms:
                    candles[bar["ts"]] = bar

            last_ts = int(rows[-1][0])
            if last_ts >= end_ms or len(rows) < 1500:
                break
            cursor = last_ts + _MS.get(tf, 60_000)
            await asyncio.sleep(0.05)

        # only use spot for pre-futures data
        if url == futures_url and futures_launch <= start_ms:
            break   # futures covers the whole range — skip spot

    result = sorted(candles.values(), key=lambda c: c["ts"])
    if not result:
        log.warning("range klines: final result empty for %s %s", symbol, tf)
    return result


_OI_MAX_WINDOW_MS = 30 * 86_400_000   # Binance OI API: max 30 days per request
_OI_HISTORY_LIMIT_MS = 30 * 86_400_000  # OI history only available ~30 days back


async def _fetch_oi_range(
    session:  aiohttp.ClientSession,
    symbol:   str,
    start_ms: int,
    end_ms:   int,
) -> list[dict]:
    """Fetch OI history for a specific time window.

    Binance /futures/data/openInterestHist constraints:
      - max 30 days per request (endTime - startTime ≤ 30 days)
      - only ~30 days of history available (older requests return 400)
    Paginates forward in 30-day chunks; silently skips chunks older than
    the API's retention window.
    """
    url    = f"{_FUTURES_BASE}/futures/data/openInterestHist"
    result: dict[int, dict] = {}
    # Clamp start to avoid requesting data older than API retention
    now_ms    = int(time.time() * 1000)
    effective_start = max(start_ms, now_ms - _OI_HISTORY_LIMIT_MS)
    cursor    = effective_start

    if effective_start > end_ms:
        log.info("OI range %s: entire window older than API retention — skipping", symbol)
        return []

    while cursor < end_ms:
        window_end = min(cursor + _OI_MAX_WINDOW_MS, end_ms)
        params: dict = {
            "symbol":    symbol,
            "period":    "1h",
            "startTime": cursor,
            "endTime":   window_end,
            "limit":     500,
        }
        try:
            async with session.get(url, params=params) as resp:
                resp.raise_for_status()
                rows = await resp.json()
        except Exception as exc:
            log.warning("range OI fetch failed %s: %s", symbol, exc)
            break

        if not rows:
            cursor = window_end + 3_600_000
            continue

        for r in rows:
            ts = int(r["timestamp"])
            if start_ms <= ts <= end_ms:
                result[ts] = {"ts": ts, "oi": float(r["sumOpenInterest"])}

        last_ts = int(rows[-1]["timestamp"])
        if len(rows) < 500:
            cursor = window_end + 3_600_000   # advance to next window
        else:
            cursor = last_ts + 3_600_000
        await asyncio.sleep(0.05)

    return sorted(result.values(), key=lambda x: x["ts"])


async def _fetch_funding_range(
    session:  aiohttp.ClientSession,
    symbol:   str,
    start_ms: int,
    end_ms:   int,
) -> list[dict]:
    url    = f"{_FUTURES_BASE}/fapi/v1/fundingRate"
    result: dict[int, dict] = {}
    cursor = start_ms

    while cursor < end_ms:
        params: dict = {
            "symbol":    symbol,
            "startTime": cursor,
            "endTime":   end_ms,
            "limit":     1000,
        }
        try:
            async with session.get(url, params=params) as resp:
                resp.raise_for_status()
                rows = await resp.json()
        except Exception as exc:
            log.warning("range funding fetch failed %s: %s", symbol, exc)
            break

        if not rows:
            break

        for r in rows:
            ts = int(r["fundingTime"])
            if start_ms <= ts <= end_ms:
                result[ts] = {"ts": ts, "rate": float(r["fundingRate"])}

        last_ts = int(rows[-1]["fundingTime"])
        if last_ts >= end_ms or len(rows) < 1000:
            break
        cursor = last_ts + 28_800_000
        await asyncio.sleep(0.05)

    return sorted(result.values(), key=lambda x: x["ts"])


async def fetch_period_async(
    symbols:    list[str],
    from_ms:    int,
    to_ms:      int,
    warmup_days: int = 45,
) -> dict:
    """Fetch OHLCV + OI + funding for a specific historical period.

    Adds `warmup_days` days of context before `from_ms` so indicators
    (EMA200, ADX, ATR) are properly warmed up at the start of the window.
    Data is NOT cached to disk (always fresh for historical periods).
    """
    warmup_ms   = warmup_days * 86_400_000
    fetch_start = from_ms - warmup_ms
    merged: dict = {"ohlcv": {}, "oi": {}, "funding": {}}

    async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
        for symbol in symbols:
            log.info("Fetching range %s (%s → %s + %dd warmup)...",
                     symbol,
                     _ms_to_date(from_ms), _ms_to_date(to_ms), warmup_days)

            for tf in ["1m", "5m", "15m", "1h", "4h", "1d", "1w"]:
                # Skip 1m — not used by any strategy (too large and not useful for backtest)
                if tf == "1m":
                    merged["ohlcv"][f"{symbol}:{tf}"] = []
                    continue
                log.info("  %s %s...", symbol, tf)
                bars = await _fetch_klines_range(session, symbol, tf, fetch_start, to_ms)
                merged["ohlcv"][f"{symbol}:{tf}"] = bars
                await asyncio.sleep(0.1)

            log.info("  %s OI...", symbol)
            oi = await _fetch_oi_range(session, symbol, fetch_start, to_ms)
            merged["oi"][symbol] = oi

            log.info("  %s funding...", symbol)
            fund = await _fetch_funding_range(session, symbol, fetch_start, to_ms)
            merged["funding"][symbol] = fund

    return merged


def _ms_to_date(ms: int) -> str:
    import datetime
    return datetime.datetime.utcfromtimestamp(ms / 1000).strftime("%Y-%m-%d")


def fetch_period_sync(
    symbols:     list[str],
    from_ms:     int,
    to_ms:       int,
    warmup_days: int = 45,
) -> dict:
    return asyncio.run(fetch_period_async(symbols, from_ms, to_ms, warmup_days))


# ── Public API ────────────────────────────────────────────────────────────────

async def fetch_all_async(symbols: list[str], force: bool = False) -> dict:
    merged: dict = {"ohlcv": {}, "oi": {}, "funding": {}}
    async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
        for symbol in symbols:
            log.info("Fetching %s...", symbol)
            data = await _fetch_symbol(session, symbol, force=force)
            merged["ohlcv"].update(data["ohlcv"])
            merged["oi"][symbol]      = data["oi"]
            merged["funding"][symbol] = data["funding"]
    return merged


def fetch_all_sync(symbols: list[str], force: bool = False) -> dict:
    return asyncio.run(fetch_all_async(symbols, force=force))
