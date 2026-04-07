"""One-time data download script.

Run this ONCE before using the backtest tab:
    python backtest/download_data.py

After this, all backtests run from local cache — no API calls needed.
Re-run to update with latest data:
    python backtest/download_data.py --from-date 2025-01-01
"""
import argparse
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def main():
    parser = argparse.ArgumentParser(
        description="Download historical OHLCV data for backtesting"
    )
    parser.add_argument(
        "--from-date", default="2022-01-01",
        help="Start date YYYY-MM-DD (default: 2022-01-01)"
    )
    parser.add_argument(
        "--to-date", default="",
        help="End date YYYY-MM-DD (default: today)"
    )
    parser.add_argument(
        "--symbols", default="",
        help="Comma-separated symbols (default: all 8 coins)"
    )
    args = parser.parse_args()

    symbols = [s.strip().upper() for s in args.symbols.split(",")
               if s.strip()] or None

    from backtest.fetcher import download_all_history
    info = download_all_history(
        symbols=symbols,
        from_date=args.from_date,
        to_date=args.to_date or None,
    )

    print(f"\n{'='*50}")
    print("Download complete!")
    print(f"Total bars:  {info['total_bars']:,}")
    print(f"Total size:  {info['total_size_mb']} MB")
    print(f"Total files: {info['total_files']}")
    print(f"\nBacktests will now run from local cache.")
    print(f"Expected speed: 5-10 seconds per run (was 30-90s)")
    print(f"{'='*50}\n")

    print("Symbols cached:")
    for sym, tfs in sorted(info["symbols"].items()):
        bars_str = ", ".join(f"{tf}:{n:,}" for tf, n in sorted(tfs.items()))
        print(f"  {sym}: {bars_str}")


if __name__ == "__main__":
    main()
