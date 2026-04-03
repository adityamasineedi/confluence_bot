"""
backtest/fetch.py
Download Binance Futures OHLCV data for backtesting.
- Shows live progress bar for each timeframe
- Verifies existing files are complete before skipping
- Run once: python backtest/fetch.py
"""
import asyncio
import json
import os
import sys
from datetime import datetime, timezone

import aiohttp

SYMBOLS    = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT",
              "XRPUSDT", "LINKUSDT", "DOGEUSDT", "SUIUSDT"]
TIMEFRAMES = ["5m", "15m", "1h", "4h", "1d", "1w"]
FROM_DATE  = "2023-01-01"
TO_DATE    = "2026-04-01"
OUT_DIR    = os.path.join(os.path.dirname(__file__), "data")
BINANCE    = "https://fapi.binance.com"

# Minimum expected bars per timeframe for 3-year period
# If existing file has fewer bars than this, it is re-downloaded
MIN_BARS: dict[str, int] = {
    "5m":  180_000,   # ~3 years of 5m bars
    "15m":  60_000,   # ~3 years of 15m bars
    "1h":   26_000,   # ~3 years of 1h bars
    "4h":    6_500,   # ~3 years of 4h bars
    "1d":    1_000,   # ~3 years of daily bars
    "1w":      140,   # ~3 years of weekly bars
}


def _ms(date: str) -> int:
    return int(datetime.strptime(date, "%Y-%m-%d")
               .replace(tzinfo=timezone.utc).timestamp() * 1000)


def _bar_count(path: str, symbol: str, tf: str) -> int:
    """Return number of bars for symbol:tf in existing JSON file. 0 if missing."""
    try:
        with open(path) as f:
            data = json.load(f)
        return len(data.get(f"{symbol}:{tf}", []))
    except Exception:
        return 0


def _is_complete(path: str, symbol: str) -> bool:
    """Return True only if ALL timeframes have enough bars."""
    if not os.path.exists(path):
        return False
    for tf, min_count in MIN_BARS.items():
        count = _bar_count(path, symbol, tf)
        if count < min_count:
            return False
    return True


def _progress_bar(done: int, total: int, width: int = 30) -> str:
    filled = int(width * done / total) if total > 0 else 0
    bar    = "#" * filled + "." * (width - filled)
    pct    = done / total * 100 if total > 0 else 0
    return f"[{bar}] {pct:5.1f}%  {done:>7,}/{total:>7,} bars"


async def _fetch_tf(session: aiohttp.ClientSession,
                    symbol: str, interval: str,
                    start: int, end: int) -> list[dict]:
    """Fetch all bars with live progress bar."""
    all_rows: list = []
    cursor         = start

    # Estimate total bars for progress display
    ms_per_bar = {
        "5m": 300_000, "15m": 900_000, "1h": 3_600_000,
        "4h": 14_400_000, "1d": 86_400_000, "1w": 604_800_000,
    }
    est_total = max(1, (end - start) // ms_per_bar.get(interval, 3_600_000))

    while cursor < end:
        try:
            async with session.get(
                f"{BINANCE}/fapi/v1/klines",
                params=dict(symbol=symbol, interval=interval,
                            startTime=cursor, endTime=end, limit=1500),
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                resp.raise_for_status()
                rows = await resp.json()
        except Exception as exc:
            print(f"\n    [RETRY] {exc}")
            await asyncio.sleep(5)
            continue

        if not rows:
            break

        all_rows.extend(rows)
        cursor = int(rows[-1][0]) + 1

        # Live progress bar — overwrite the same line
        bar = _progress_bar(len(all_rows), est_total)
        print(f"\r    {interval:>4s}  {bar}", end="", flush=True)

        if len(rows) < 1500:
            break

        await asyncio.sleep(0.25)

    # Final line — show actual count
    actual = len(all_rows)
    print(f"\r    {interval:>4s}  [{'#'*30}] 100.0%  {actual:>7,}/{actual:>7,} bars  OK")

    return [
        {"ts": int(r[0]), "o": float(r[1]), "h": float(r[2]),
         "l":  float(r[3]), "c": float(r[4]), "v": float(r[5])}
        for r in all_rows
    ]


async def main() -> None:
    os.makedirs(OUT_DIR, exist_ok=True)
    start_ms = _ms(FROM_DATE)
    end_ms   = _ms(TO_DATE)

    print(f"\nDownloading {len(SYMBOLS)} coins x {len(TIMEFRAMES)} timeframes")
    print(f"Period : {FROM_DATE}  ->  {TO_DATE}")
    print(f"Output : {OUT_DIR}\n")
    print("-" * 60)

    downloaded, skipped, failed = [], [], []

    async with aiohttp.ClientSession() as session:
        for symbol in SYMBOLS:
            out_path = os.path.join(OUT_DIR, f"{symbol}.json")

            # ── Check if existing file is already complete ──────────────────
            if os.path.exists(out_path):
                if _is_complete(out_path, symbol):
                    # Show what's already there
                    print(f"\n[SKIP] {symbol}.json — already complete:")
                    for tf in TIMEFRAMES:
                        count = _bar_count(out_path, symbol, tf)
                        print(f"    {tf:>4s}  {count:>8,} bars  OK")
                    skipped.append(symbol)
                    continue
                else:
                    # File exists but incomplete — re-download
                    print(f"\n[INCOMPLETE] {symbol}.json — re-downloading:")
                    for tf in TIMEFRAMES:
                        count    = _bar_count(out_path, symbol, tf)
                        minimum  = MIN_BARS[tf]
                        status   = "OK" if count >= minimum else f"!! (got {count:,}, need {minimum:,})"
                        print(f"    {tf:>4s}  {count:>8,} bars  {status}")
                    print()

            # ── Download all timeframes ─────────────────────────────────────
            print(f"\n[DOWNLOAD] {symbol}")
            coin_data: dict[str, list] = {}
            ok = True

            for tf in TIMEFRAMES:
                try:
                    bars = await _fetch_tf(session, symbol, tf, start_ms, end_ms)
                    coin_data[f"{symbol}:{tf}"] = bars
                except Exception as exc:
                    print(f"\n    [ERROR] {symbol} {tf}: {exc}")
                    ok = False
                    break

            if not ok:
                failed.append(symbol)
                continue

            # Save to disk
            with open(out_path, "w") as f:
                json.dump(coin_data, f, separators=(",", ":"))

            size_mb = os.path.getsize(out_path) / 1_048_576
            print(f"    -> saved {symbol}.json  ({size_mb:.1f} MB)")
            downloaded.append(symbol)

    # ── Final summary ───────────────────────────────────────────────────────
    print(f"\n{'-'*60}")
    print(f"SUMMARY")
    print(f"  Downloaded : {len(downloaded)} coins  {downloaded}")
    print(f"  Skipped    : {len(skipped)} coins  {skipped}")
    if failed:
        print(f"  Failed     : {len(failed)} coins  {failed}")
        print(f"  Re-run this script to retry failed coins.")
    print(f"{'-'*60}")

    if downloaded or skipped:
        print(f"\nData ready. Run:")
        print(f"  python backtest/run.py --symbol BTCUSDT --strategy fvg")
        print(f"  python backtest/run.py --symbol BTCUSDT --strategy all")
        print(f"  python backtest/run.py --symbol ALL --strategy all\n")


if __name__ == "__main__":
    asyncio.run(main())
