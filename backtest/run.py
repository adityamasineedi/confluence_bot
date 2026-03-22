"""Backtest runner — entry point.

Usage
-----
    python -m backtest.run
    python -m backtest.run --symbols BTCUSDT,ETHUSDT,SOLUSDT --capital 1000
    python -m backtest.run --refresh        # force re-download
    python -m backtest.run --risk-pct 0.02  # 2% risk per trade (default)
"""
import argparse
import asyncio
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("backtest")


def main() -> None:
    parser = argparse.ArgumentParser(description="confluence_bot backtester")
    parser.add_argument(
        "--symbols", default="BTCUSDT,ETHUSDT,SOLUSDT",
        help="Comma-separated symbols (default: BTCUSDT,ETHUSDT,SOLUSDT)",
    )
    parser.add_argument(
        "--refresh", action="store_true",
        help="Force re-download even if cached data exists",
    )
    parser.add_argument(
        "--warmup", type=int, default=210,
        help="1h bars to skip for indicator warmup (default: 210)",
    )
    parser.add_argument(
        "--capital", type=float, default=1_000.0,
        help="Starting capital in USD (default: 1000)",
    )
    parser.add_argument(
        "--risk-pct", type=float, default=0.02,
        help="Fraction of equity risked per trade (default: 0.02 = 2%%)",
    )
    args = parser.parse_args()

    symbols = [s.strip().upper() for s in args.symbols.split(",")]

    # ── Step 1: fetch historical data ─────────────────────────────────────────
    print(f"\nFetching historical data for {symbols} ...")
    print("(cached files in backtest/data/ reused if < 24 h old)\n")

    from backtest.fetcher import fetch_all_sync
    data = fetch_all_sync(symbols, force=args.refresh)

    ohlcv   = data["ohlcv"]
    oi      = data["oi"]
    funding = data["funding"]

    for sym in symbols:
        n = len(ohlcv.get(f"{sym}:1h", []))
        print(f"  {sym}  1h bars: {n:,}"
              f"  |  OI snapshots: {len(oi.get(sym, []))}"
              f"  |  funding records: {len(funding.get(sym, []))}")
    print()

    # ── Step 2: run the backtest ───────────────────────────────────────────────
    print(f"Starting capital : ${args.capital:,.2f}")
    print(f"Risk per trade   : {args.risk_pct*100:.1f}% of equity")
    print(f"RR ratio         : 2.5  (TP = risk x 2.5)")
    print(f"Warmup bars      : {args.warmup} x 1h\n")
    print("Running backtest pipeline...\n")

    from backtest.engine import run
    trades = asyncio.run(run(
        symbols          = symbols,
        ohlcv            = ohlcv,
        oi               = oi,
        funding          = funding,
        warmup_bars      = args.warmup,
        starting_capital = args.capital,
        risk_pct         = args.risk_pct,
    ))

    if not trades:
        print("No trades generated. Check data availability and warmup period.")
        return

    # ── Step 3: report ────────────────────────────────────────────────────────
    from backtest.reporter import compute_stats, print_report
    stats = compute_stats(trades, starting_capital=args.capital)
    print_report(stats, trades=trades, starting_capital=args.capital)


if __name__ == "__main__":
    main()
