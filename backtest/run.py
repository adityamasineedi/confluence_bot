"""
backtest/run.py
CLI runner for the vectorized backtest engine.

Usage examples:
  python backtest/run.py --symbol BTCUSDT --strategy fvg
  python backtest/run.py --symbol BTCUSDT --strategy all
  python backtest/run.py --symbol ALL --strategy all
  python backtest/run.py --symbol BTCUSDT --strategy fvg --show-trades
  python backtest/run.py --symbol BTCUSDT --strategy fvg \
      --from-date 2025-01-01 --to-date 2026-04-01
  python backtest/run.py --symbol BTCUSDT --strategy fvg --mc-compare
  python backtest/run.py --symbol BTCUSDT --strategy fvg --mc-threshold 0.40
"""
import argparse
import os
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from backtest.engine import (load, compute_stats, RUNNERS, run_strategy,
                             _resolve_mc_threshold)

PASS_MARK = "PASS"
WARN_MARK = "WARN"
FAIL_MARK = "FAIL"
MIN_PF    = 1.50
WARN_PF   = 1.20


def _ms(date_str: str) -> int:
    return int(
        datetime.strptime(date_str, "%Y-%m-%d")
        .replace(tzinfo=timezone.utc)
        .timestamp() * 1000
    )


def verdict(pf: float) -> str:
    if pf >= MIN_PF:
        return PASS_MARK
    if pf >= WARN_PF:
        return WARN_MARK
    return FAIL_MARK


def print_comparison(raw: dict, mc: dict, symbol: str, strategy: str,
                     mc_thresh: float = 2.0) -> None:
    """Print side-by-side comparison of unfiltered vs vol-ratio filtered."""
    skipped  = raw["n"] - mc["n"]
    skip_pct = skipped / raw["n"] * 100 if raw["n"] > 0 else 0

    # Compute total R (sum of pnl_r) from avg_r × n
    total_r_raw = raw["avg_r"] * raw["n"]
    total_r_mc  = mc["avg_r"]  * mc["n"]

    thresh_label = f"vol<={mc_thresh:.1f}x"

    print(f"\n{'='*70}")
    print(f"  VOL-RATIO COMPARISON: {symbol} {strategy}")
    print(f"{'='*70}")
    print(f"  {'Metric':<20} {'Unfiltered':>15} {thresh_label:>15} {'Change':>12}")
    print(f"  {'-'*62}")
    print(f"  {'Trades':<20} {raw['n']:>15} {mc['n']:>15}"
          f"   {skipped:>+5} skip ({skip_pct:.0f}%)")
    print(f"  {'Win Rate':<20} {raw['wr']:>14.1f}% {mc['wr']:>14.1f}%"
          f"   {mc['wr'] - raw['wr']:>+9.1f}%")
    print(f"  {'Profit Factor':<20} {raw['pf']:>15.2f} {mc['pf']:>15.2f}"
          f"   {mc['pf'] - raw['pf']:>+9.2f}")
    print(f"  {'Avg R':<20} {raw['avg_r']:>+15.3f} {mc['avg_r']:>+15.3f}"
          f"   {mc['avg_r'] - raw['avg_r']:>+9.3f}")
    print(f"  {'Total R':<20} {total_r_raw:>+15.2f} {total_r_mc:>+15.2f}"
          f"   {total_r_mc - total_r_raw:>+9.2f}")

    if raw["pf"] > 0 and mc["pf"] > 0:
        improved = mc["pf"] > raw["pf"]
        tag = "IMPROVES" if improved else "HURTS"
        print(f"\n  Verdict: vol gate {tag} (PF {raw['pf']:.2f} -> {mc['pf']:.2f})")
    print(f"{'='*70}\n")


def bar_date(data: dict, symbol: str, bar_idx: int) -> str:
    """Convert bar index to readable UTC date string."""
    key  = f"{symbol}:1h"
    bars = data.get(key)
    if bars is None or bar_idx >= len(bars):
        return "???"
    ts = bars[bar_idx, 5]  # TS column
    return datetime.fromtimestamp(ts / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Vectorized backtest — confluence_bot"
    )
    ap.add_argument("--symbol",      default="BTCUSDT",
                    help="Symbol to test (e.g. BTCUSDT) or ALL")
    ap.add_argument("--strategy",    default="fvg",
                    choices=list(RUNNERS) + ["all"],
                    help="Strategy to test or 'all'")
    ap.add_argument("--from-date",   default="2023-01-01",
                    help="Start date YYYY-MM-DD")
    ap.add_argument("--to-date",     default="2026-04-01",
                    help="End date YYYY-MM-DD")
    ap.add_argument("--show-trades", action="store_true",
                    help="Print every individual trade entry/exit")
    ap.add_argument("--mc-threshold", type=float, default=0.0,
                    help="Max vol ratio threshold "
                         "(0.0 = disabled, 2.0 = block when 6H vol > 2x baseline)")
    ap.add_argument("--mc-compare", action="store_true", default=False,
                    help="Run both unfiltered and vol-ratio filtered, "
                         "show side-by-side comparison")
    args = ap.parse_args()

    from_ts = _ms(args.from_date)
    to_ts   = _ms(args.to_date)

    # resolve symbols
    if args.symbol.upper() == "ALL":
        try:
            import yaml
            cfg_path = os.path.join(os.path.dirname(__file__), "..", "config.yaml")
            with open(cfg_path) as f:
                symbols = yaml.safe_load(f).get("symbols", [])
        except Exception:
            symbols = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT",
                       "XRPUSDT", "LINKUSDT", "DOGEUSDT", "SUIUSDT"]
    else:
        symbols = [args.symbol.upper()]

    strategies = list(RUNNERS) if args.strategy == "all" else [args.strategy]

    # load BTC data once — needed for weekly macro gate on all symbols
    btc_data = load("BTCUSDT")

    print(f"\n{'='*65}")
    print(f"  BACKTEST")
    print(f"  Coins    : {', '.join(symbols)}")
    print(f"  Strategy : {', '.join(strategies)}")
    print(f"  Period   : {args.from_date}  ->  {args.to_date}")
    print(f"{'='*65}\n")

    mc_threshold = args.mc_threshold
    mc_compare   = args.mc_compare
    # For compare mode: CLI override or 0.0 (= use per-strategy config defaults)
    mc_thresh_val = mc_threshold

    all_trades: list  = []
    result_rows: list = []

    for symbol in symbols:
        data = load(symbol)
        if data is None:
            print(f"  [MISSING] {symbol}.json - run: python backtest/fetch.py")
            continue

        for strategy in strategies:
            if mc_compare:
                # ── Side-by-side MC comparison ────────────────────────────────
                t0 = time.time()
                trades_raw = run_strategy(symbol, strategy, data, btc_data,
                                          from_ts, to_ts, mc_threshold=-1.0)
                stats_raw  = compute_stats(trades_raw)

                trades_mc  = run_strategy(symbol, strategy, data, btc_data,
                                          from_ts, to_ts,
                                          mc_threshold=mc_thresh_val)
                stats_mc   = compute_stats(trades_mc)
                elapsed    = time.time() - t0

                # Show the effective threshold used
                eff_thresh = _resolve_mc_threshold(strategy, mc_thresh_val)
                print_comparison(stats_raw, stats_mc, symbol, strategy,
                                 mc_thresh=eff_thresh)

                # Show individual MC-filtered trades if requested
                if args.show_trades and trades_mc:
                    for t in trades_mc:
                        date_str = bar_date(data, symbol, t.bar_idx)
                        icon = ("TP" if t.outcome == "TP"
                                else "SL" if t.outcome == "SL"
                                else "TO")
                        print(
                            f"       [{icon}]  {date_str}  "
                            f"{t.direction:<5}  "
                            f"entry:{t.entry:>10.4f}  "
                            f"sl:{t.stop:>10.4f}  "
                            f"tp:{t.tp:>10.4f}  "
                            f"{t.outcome:<7}  "
                            f"R:{t.pnl_r:>+.2f}  "
                            f"vr:{t.vol_ratio:.2f}"
                        )
                    print()

                all_trades.extend(trades_raw)
                result_rows.append({"symbol": symbol, "strategy": strategy,
                                    **stats_raw})
                print(f"  ({elapsed:.1f}s)")

            else:
                # ── Normal single run ─────────────────────────────────────────
                t0      = time.time()
                trades  = run_strategy(symbol, strategy, data, btc_data,
                                       from_ts, to_ts,
                                       mc_threshold=mc_threshold)
                s       = compute_stats(trades)
                elapsed = time.time() - t0

                mc_label = f"  [vol<={mc_threshold:.1f}x]" if mc_threshold > 0 else ""
                vrd = verdict(s["pf"])
                print(
                    f"  [{vrd}]  {symbol:<10}  {strategy:<15}  "
                    f"n:{s['n']:>4}  "
                    f"W:{s['wins']:>3}  L:{s['losses']:>3}  "
                    f"TO:{s['timeouts']:>3}  "
                    f"WR:{s['wr']:>5.1f}%  "
                    f"PF:{s['pf']:>5.2f}  "
                    f"avgR:{s['avg_r']:>+.3f}  "
                    f"({elapsed:.1f}s){mc_label}"
                )

                if args.show_trades and trades:
                    for t in trades:
                        date_str = bar_date(data, symbol, t.bar_idx)
                        icon = ("TP" if t.outcome == "TP"
                                else "SL" if t.outcome == "SL"
                                else "TO")
                        mc_str = (f"  vr:{t.vol_ratio:.2f}"
                                  if t.vol_ratio > 0 else "")
                        print(
                            f"       [{icon}]  {date_str}  "
                            f"{t.direction:<5}  "
                            f"entry:{t.entry:>10.4f}  "
                            f"sl:{t.stop:>10.4f}  "
                            f"tp:{t.tp:>10.4f}  "
                            f"{t.outcome:<7}  "
                            f"R:{t.pnl_r:>+.2f}{mc_str}"
                        )
                    print()

                all_trades.extend(trades)
                result_rows.append({"symbol": symbol, "strategy": strategy,
                                    **s})

    if not result_rows:
        print("  No results — check that backtest/data/ files exist.\n")
        return

    # Summary table sorted by PF descending
    print(f"\n{'='*65}")
    print(f"  {'SYMBOL':<10}  {'STRATEGY':<15}  {'N':>4}  "
          f"{'WR':>6}  {'PF':>5}  VERDICT")
    print(f"  {'-'*60}")
    for r in sorted(result_rows, key=lambda x: x["pf"], reverse=True):
        print(
            f"  {r['symbol']:<10}  {r['strategy']:<15}  "
            f"{r['n']:>4}  "
            f"{r['wr']:>5.1f}%  "
            f"{r['pf']:>5.2f}  "
            f"{verdict(r['pf'])}"
        )

    if all_trades:
        t = compute_stats(all_trades)
        print(f"  {'-'*60}")
        print(
            f"  {'OVERALL':<27}  "
            f"{t['n']:>4}  "
            f"{t['wr']:>5.1f}%  "
            f"{t['pf']:>5.2f}  "
            f"{verdict(t['pf'])}"
        )
    print(f"{'='*65}\n")


if __name__ == "__main__":
    main()
