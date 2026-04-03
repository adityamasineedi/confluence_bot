"""Download historical OHLCV data from Binance for backtesting."""
import asyncio
import json
import os
import time
from datetime import datetime, timezone

import aiohttp

# ── Config ────────────────────────────────────────────────────────────────────
SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT",
           "XRPUSDT", "LINKUSDT", "DOGEUSDT", "SUIUSDT"]

TIMEFRAMES = ["1h", "4h", "15m", "1d", "1w"]

# Download 2 years: covers bull (2023), crash (late 2024), bear (2025-2026)
FROM_DATE = "2023-01-01"
TO_DATE   = "2026-04-01"

OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "data")
BINANCE_BASE = "https://fapi.binance.com"

# ── Helpers ───────────────────────────────────────────────────────────────────

def date_to_ms(date_str: str) -> int:
    dt = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def parse_kline(row: list) -> dict:
    return {
        "ts": int(row[0]),
        "o":  float(row[1]),
        "h":  float(row[2]),
        "l":  float(row[3]),
        "c":  float(row[4]),
        "v":  float(row[5]),
    }


async def fetch_klines(
    session: aiohttp.ClientSession,
    symbol: str,
    interval: str,
    start_ms: int,
    end_ms: int,
) -> list[dict]:
    """Fetch all klines for symbol/interval between start and end timestamps."""
    all_candles = []
    cursor = start_ms
    limit = 1000   # Binance max per request

    while cursor < end_ms:
        url = f"{BINANCE_BASE}/fapi/v1/klines"
        params = {
            "symbol":    symbol,
            "interval":  interval,
            "startTime": cursor,
            "endTime":   end_ms,
            "limit":     limit,
        }
        try:
            async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                resp.raise_for_status()
                rows = await resp.json()
        except Exception as e:
            print(f"  [ERROR] {symbol} {interval}: {e} — retrying in 5s")
            await asyncio.sleep(5)
            continue

        if not rows:
            break

        candles = [parse_kline(r) for r in rows]
        all_candles.extend(candles)

        # Move cursor forward — next batch starts after last candle
        cursor = candles[-1]["ts"] + 1

        # Binance rate limit: max 2400 weight/min, klines = 5 weight
        await asyncio.sleep(0.3)

        if len(rows) < limit:
            break  # reached the end

    return all_candles


async def download_all():
    """Download all symbols and timeframes, save as JSON files."""
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    start_ms = date_to_ms(FROM_DATE)
    end_ms   = date_to_ms(TO_DATE)

    print(f"Downloading {len(SYMBOLS)} coins × {len(TIMEFRAMES)} timeframes")
    print(f"Period: {FROM_DATE} -> {TO_DATE}")
    print(f"Output: {OUTPUT_DIR}/")
    print()

    async with aiohttp.ClientSession() as session:
        for symbol in SYMBOLS:
            coin_data = {}
            for tf in TIMEFRAMES:
                key = f"{symbol}:{tf}"
                filepath = os.path.join(OUTPUT_DIR, f"{symbol}_{tf}.json")

                # Skip if already downloaded
                if os.path.exists(filepath):
                    with open(filepath) as f:
                        candles = json.load(f)
                    print(f"  [SKIP] {key} — already downloaded ({len(candles)} bars)")
                    coin_data[key] = candles
                    continue

                print(f"  [FETCH] {key} ...", end="", flush=True)
                candles = await fetch_klines(session, symbol, tf, start_ms, end_ms)
                print(f" {len(candles)} bars")

                # Save individual file
                with open(filepath, "w") as f:
                    json.dump(candles, f, separators=(",", ":"))

                coin_data[key] = candles
                await asyncio.sleep(0.5)

            # Save combined file per symbol
            combined_path = os.path.join(OUTPUT_DIR, f"{symbol}_all.json")
            with open(combined_path, "w") as f:
                json.dump(coin_data, f, separators=(",", ":"))
            print(f"  [SAVED] {symbol}_all.json")

    print("\n[DONE] All data downloaded.")
    print(f"Run backtest: python -m backtest.run --strategy fvg --symbol BNBUSDT")


if __name__ == "__main__":
    asyncio.run(download_all())
