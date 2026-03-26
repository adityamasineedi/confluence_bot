"""Backtest runner — entry point.

Usage
-----
    python -m backtest.run
    python -m backtest.run --symbols BTCUSDT,ETHUSDT,SOLUSDT --capital 1000
    python -m backtest.run --capital 100 --days 180   # $100 starting capital, last 6 months
    python -m backtest.run --refresh        # force re-download
    python -m backtest.run --risk-pct 0.02  # 2% risk per trade (default)
"""
import argparse
import asyncio
import logging
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("backtest")


def main() -> None:
    parser = argparse.ArgumentParser(description="confluence_bot backtester")
    parser.add_argument(
        "--symbols", default="BTCUSDT,ETHUSDT,SOLUSDT,BNBUSDT,AVAXUSDT,ADAUSDT,DOTUSDT,DOGEUSDT,SUIUSDT",
        help="Comma-separated symbols (default: all 9 configured symbols)",
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
    parser.add_argument(
        "--days", type=int, default=0,
        help="Limit backtest to last N days of data (0 = all available)",
    )
    parser.add_argument(
        "--strategy",
        choices=["main", "leadlag", "both", "microrange", "session", "insidebar", "funding", "all"],
        default="main",
        help="Which strategy to backtest (default: main)",
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

    # ── Optional: trim to last N days ─────────────────────────────────────────
    if args.days > 0:
        cutoff_ms = int((time.time() - args.days * 86_400) * 1000)
        warmup_h  = args.warmup
        for key in list(ohlcv.keys()):
            bars = ohlcv[key]
            # Find first bar at or after the cutoff, then keep warmup bars before it
            idx = next((i for i, b in enumerate(bars) if b["ts"] >= cutoff_ms), len(bars))
            start = max(0, idx - warmup_h)
            ohlcv[key] = bars[start:]
        print(f"Date window      : last {args.days} days  "
              f"(cutoff {time.strftime('%Y-%m-%d', time.gmtime(cutoff_ms/1000))})\n")

    for sym in symbols:
        n = len(ohlcv.get(f"{sym}:1h", []))
        print(f"  {sym}  1h bars: {n:,}"
              f"  |  OI snapshots: {len(oi.get(sym, []))}"
              f"  |  funding records: {len(funding.get(sym, []))}")
    print()

    from backtest.reporter import compute_stats, print_report
    import json, os as _os

    # ── Step 2a: main strategy backtest ───────────────────────────────────────
    if args.strategy in ("main", "both"):
        print(f"Starting capital : ${args.capital:,.2f}")
        print(f"Risk per trade   : {args.risk_pct*100:.1f}% of equity")
        print(f"RR ratio         : 2.5  (TP = risk x 2.5)")
        print(f"Warmup bars      : {args.warmup} x 1h\n")
        print("Running MAIN backtest pipeline...\n")

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
        else:
            stats = compute_stats(trades, starting_capital=args.capital)
            print_report(stats, trades=trades, starting_capital=args.capital)

            result_path = _os.path.join(_os.path.dirname(__file__), "results.json")
            with open(result_path, "w") as f:
                json.dump({"stats": stats, "trades": trades, "symbols": symbols,
                           "capital": args.capital, "risk_pct": args.risk_pct}, f, default=str)
            print(f"Results saved to {result_path}")

    # ── Step 2b: lead-lag strategy backtest ───────────────────────────────────
    if args.strategy in ("leadlag", "both"):
        print(f"\n{'='*68}")
        print("Running LEAD-LAG backtest pipeline (5m bars)...\n")
        print(f"Starting capital : ${args.capital:,.2f}")
        print(f"Risk per trade   : {args.risk_pct*100:.1f}% of equity")
        print(f"SL / TP          : 0.20% / 0.50%  (2.5 RR fixed)")
        print(f"Max hold         : 30 min (6 × 5m bars)")
        print(f"Max alts/signal  : 3\n")

        from backtest.leadlag_engine import run as run_ll
        ll_trades = run_ll(
            symbols          = symbols,
            ohlcv            = ohlcv,
            starting_capital = args.capital,
            risk_pct         = args.risk_pct,
        )

        if not ll_trades:
            print("No lead-lag trades generated.")
        else:
            ll_stats = compute_stats(ll_trades, starting_capital=args.capital)
            print_report(ll_stats, trades=ll_trades, starting_capital=args.capital)

            ll_path = _os.path.join(_os.path.dirname(__file__), "results_leadlag.json")
            with open(ll_path, "w") as f:
                json.dump({"stats": ll_stats, "trades": ll_trades, "symbols": symbols,
                           "capital": args.capital, "risk_pct": args.risk_pct,
                           "strategy": "leadlag"}, f, default=str)
            print(f"Results saved to {ll_path}")


    # ── Step 2c: micro-range backtest ─────────────────────────────────────────
    if args.strategy in ("microrange", "all"):
        print(f"\n{'='*68}")
        print("Running MICRO-RANGE backtest (5m mean-reversion)...\n")
        print(f"Starting capital : ${args.capital:,.2f}")
        print(f"Risk per trade   : {args.risk_pct*100:.1f}% of equity")
        print(f"Box detection    : 10 × 5m bars  |  entry zone 0.2%  |  RSI filter")
        print(f"Max hold         : 30 min (6 × 5m)\n")

        from backtest.microrange_engine import run as run_mr
        mr_trades = run_mr(symbols=symbols, ohlcv=ohlcv,
                           starting_capital=args.capital, risk_pct=args.risk_pct)
        if not mr_trades:
            print("No micro-range trades generated.")
        else:
            mr_stats = compute_stats(mr_trades, starting_capital=args.capital)
            print_report(mr_stats, trades=mr_trades, starting_capital=args.capital)
            mr_path = _os.path.join(_os.path.dirname(__file__), "results_microrange.json")
            with open(mr_path, "w") as f:
                json.dump({"stats": mr_stats, "trades": mr_trades, "symbols": symbols,
                           "capital": args.capital, "risk_pct": args.risk_pct,
                           "strategy": "microrange"}, f, default=str)
            print(f"Results saved to {mr_path}")

    # ── Step 2d: session trap backtest ────────────────────────────────────────
    if args.strategy in ("session", "all"):
        print(f"\n{'='*68}")
        print("Running SESSION TRAP backtest (fade session open fake moves)...\n")
        print(f"Starting capital : ${args.capital:,.2f}")
        print(f"Risk per trade   : {args.risk_pct*100:.1f}% of equity")
        print(f"Sessions         : Asia 01:00 / London 08:00 / NY 13:00 UTC")
        print(f"RR               : 1.5×  |  max hold 1h\n")

        from backtest.session_engine import run as run_sess
        sess_trades = run_sess(symbols=symbols, ohlcv=ohlcv,
                               starting_capital=args.capital, risk_pct=args.risk_pct)
        if not sess_trades:
            print("No session trap trades generated.")
        else:
            sess_stats = compute_stats(sess_trades, starting_capital=args.capital)
            print_report(sess_stats, trades=sess_trades, starting_capital=args.capital)
            sess_path = _os.path.join(_os.path.dirname(__file__), "results_session.json")
            with open(sess_path, "w") as f:
                json.dump({"stats": sess_stats, "trades": sess_trades, "symbols": symbols,
                           "capital": args.capital, "risk_pct": args.risk_pct,
                           "strategy": "session"}, f, default=str)
            print(f"Results saved to {sess_path}")

    # ── Step 2e: inside bar flip backtest ─────────────────────────────────────
    if args.strategy in ("insidebar", "all"):
        print(f"\n{'='*68}")
        print("Running INSIDE BAR FLIP backtest (1H compression zones)...\n")
        print(f"Starting capital : ${args.capital:,.2f}")
        print(f"Risk per trade   : {args.risk_pct*100:.1f}% of equity")
        print(f"Min inside bars  : 2  |  RR 1.5×  |  max hold 6H\n")

        from backtest.insidebar_engine import run as run_ib
        ib_trades = run_ib(symbols=symbols, ohlcv=ohlcv,
                           starting_capital=args.capital, risk_pct=args.risk_pct)
        if not ib_trades:
            print("No inside bar trades generated.")
        else:
            ib_stats = compute_stats(ib_trades, starting_capital=args.capital)
            print_report(ib_stats, trades=ib_trades, starting_capital=args.capital)
            ib_path = _os.path.join(_os.path.dirname(__file__), "results_insidebar.json")
            with open(ib_path, "w") as f:
                json.dump({"stats": ib_stats, "trades": ib_trades, "symbols": symbols,
                           "capital": args.capital, "risk_pct": args.risk_pct,
                           "strategy": "insidebar"}, f, default=str)
            print(f"Results saved to {ib_path}")

    # ── Step 2f: funding harvest backtest ─────────────────────────────────────
    if args.strategy in ("funding", "all"):
        print(f"\n{'='*68}")
        print("Running FUNDING HARVEST backtest (collect 8h settlement payments)...\n")
        print(f"Starting capital : ${args.capital:,.2f}")
        print(f"Risk per trade   : {args.risk_pct*100:.1f}% of equity")
        print(f"Min rate         : 0.05%  |  SL 0.5%  |  TP 0.8%  |  RR 1.6×\n")

        from backtest.funding_harvest_engine import run as run_fh
        fh_trades = run_fh(symbols=symbols, ohlcv=ohlcv, funding=funding,
                           starting_capital=args.capital, risk_pct=args.risk_pct)
        if not fh_trades:
            print("No funding harvest trades generated.")
        else:
            fh_stats = compute_stats(fh_trades, starting_capital=args.capital)
            print_report(fh_stats, trades=fh_trades, starting_capital=args.capital)
            fh_path = _os.path.join(_os.path.dirname(__file__), "results_funding.json")
            with open(fh_path, "w") as f:
                json.dump({"stats": fh_stats, "trades": fh_trades, "symbols": symbols,
                           "capital": args.capital, "risk_pct": args.risk_pct,
                           "strategy": "funding"}, f, default=str)
            print(f"Results saved to {fh_path}")


if __name__ == "__main__":
    main()
