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


def _date_to_ms(date_str: str) -> int:
    """Convert 'YYYY-MM-DD' to milliseconds UTC."""
    from datetime import datetime, timezone
    return int(datetime.strptime(date_str, "%Y-%m-%d")
               .replace(tzinfo=timezone.utc).timestamp() * 1000)


def _print_cost_analysis(trades: list[dict], capital: float) -> None:
    """Print gross/net return and fee/slippage/funding breakdown for cost-aware trades."""
    cost_trades = [t for t in trades if "gross_pnl" in t]
    if not cost_trades:
        return   # engine predates cost simulation — skip

    gross = sum(t["gross_pnl"] for t in cost_trades)
    fees  = sum(t.get("cost_fee",  0.0) for t in cost_trades)
    slip  = sum(t.get("cost_slip", 0.0) for t in cost_trades)
    fund  = sum(t.get("cost_fund", 0.0) for t in cost_trades)
    cost  = fees + slip + fund
    net   = gross - cost
    drag  = (cost / abs(gross) * 100) if gross != 0 else 0.0

    print(f"\n  {'─'*52}")
    print(f"  COST ANALYSIS  ({len(cost_trades)} trades with cost data)")
    print(f"  {'─'*52}")
    print(f"  Gross return   : ${gross:+.2f}  ({gross / capital * 100:+.2f}%)")
    print(f"  ├─ Taker fees  : -${fees:.2f}")
    print(f"  ├─ Slippage    : -${slip:.2f}")
    print(f"  └─ Funding     : -${fund:.2f}")
    print(f"  Net return     : ${net:+.2f}  ({net / capital * 100:+.2f}%)")
    print(f"  Cost drag      : {drag:.1f}% of gross return")
    print(f"  {'─'*52}")


# Symbols per crash period — kept to 3 for speed (BTC + ETH + one alt)
_PERIOD_SYMBOLS: dict[str, list[str]] = {
    "covid_2020":  ["BTCUSDT", "ETHUSDT", "BNBUSDT"],
    "china_2021":  ["BTCUSDT", "ETHUSDT", "SOLUSDT"],
    "ath_2021":    ["BTCUSDT", "ETHUSDT", "SOLUSDT"],
    "luna_2022":   ["BTCUSDT", "ETHUSDT", "SOLUSDT"],
    "celsius_2022":["BTCUSDT", "ETHUSDT", "SOLUSDT"],
    "ftx_2022":    ["BTCUSDT", "ETHUSDT", "SOLUSDT"],
}

_CRASH_PERIODS = [
    ("covid_2020",   "2020-02-12", "2020-03-13", "COVID crash — BTC -60% in weeks"),
    ("china_2021",   "2021-05-12", "2021-07-20", "China mining ban — -55% from ATH, dead cat bounces"),
    ("ath_2021",     "2021-11-10", "2021-12-31", "ATH -> bear start — PUMP->CRASH regime flip"),
    ("luna_2022",    "2022-05-05", "2022-05-18", "LUNA/UST collapse — violent crash, liq cascades"),
    ("celsius_2022", "2022-06-12", "2022-06-19", "Celsius/3AC collapse — second leg crash, -40%"),
    ("ftx_2022",     "2022-11-06", "2022-11-14", "FTX collapse — sudden crash, OI flush"),
]


def _run_one_period(
    period_id:  str,
    from_date:  str,
    to_date:    str,
    label:      str,
    strategies: list[str],
    capital:    float,
    risk_pct:   float,
) -> dict:
    """Fetch + run all strategies for one historical crash period. Returns summary dict."""
    from backtest.fetcher import fetch_period_sync
    from backtest.reporter import compute_stats

    symbols = _PERIOD_SYMBOLS[period_id]
    from_ms = _date_to_ms(from_date)
    to_ms   = _date_to_ms(to_date) + 86_400_000   # include the end date

    print(f"\n{'='*68}")
    print(f"  {label}")
    print(f"  Period  : {from_date} -> {to_date}  ({(to_ms - from_ms) // 86_400_000} days)")
    print(f"  Symbols : {', '.join(symbols)}")
    print(f"{'='*68}")

    data    = fetch_period_sync(symbols, from_ms, to_ms, warmup_days=45)
    ohlcv   = data["ohlcv"]
    oi      = data["oi"]
    funding = data["funding"]

    # Trim to actual period (warmup already handled by fetcher — engines use full data)
    results = {}

    for strat in strategies:
        try:
            trades = _run_strategy(strat, symbols, ohlcv, oi, funding, capital, risk_pct)
        except Exception as exc:
            log.warning("Strategy %s failed for %s: %s", strat, period_id, exc)
            trades = []

        if not trades:
            results[strat] = {"trades": 0, "return_pct": 0.0, "win_rate": 0.0, "max_dd": 0.0}
            print(f"  {strat:15s}: no trades")
            continue

        stats = compute_stats(trades, starting_capital=capital)
        ret   = stats.get("total_return_pct", 0.0)
        wr    = stats.get("win_rate", 0.0) * 100
        dd    = stats.get("max_drawdown_pct", 0.0) * 100
        n     = stats.get("total_trades", 0)
        results[strat] = {"trades": n, "return_pct": ret, "win_rate": wr, "max_dd": dd}
        pnl_str = f"+{ret:.1f}%" if ret >= 0 else f"{ret:.1f}%"
        print(f"  {strat:15s}: {n:4d} trades  WR={wr:.0f}%  return={pnl_str:8s}  maxDD={dd:.1f}%")

    return results


def _run_strategy(
    strat:    str,
    symbols:  list[str],
    ohlcv:    dict,
    oi:       dict,
    funding:  dict,
    capital:  float,
    risk_pct: float,
) -> list[dict]:
    import asyncio as _asyncio
    if strat == "main":
        from backtest.engine import run
        return _asyncio.run(run(
            symbols=symbols, ohlcv=ohlcv, oi=oi, funding=funding,
            warmup_bars=210, starting_capital=capital, risk_pct=risk_pct,
        ))
    elif strat == "microrange":
        from backtest.microrange_engine import run
        return run(symbols=symbols, ohlcv=ohlcv,
                   starting_capital=capital, risk_pct=risk_pct)
    elif strat == "ema_pullback":
        from backtest.ema_pullback_engine import run
        return run(symbols=symbols, ohlcv=ohlcv,
                   starting_capital=capital, risk_pct=risk_pct)
    elif strat == "sweep":
        from backtest.sweep_engine import run
        return run(symbols=symbols, ohlcv=ohlcv,
                   starting_capital=capital, risk_pct=risk_pct)
    elif strat == "zone":
        from backtest.zone_engine import run
        return run(symbols=symbols, ohlcv=ohlcv,
                   starting_capital=capital, risk_pct=risk_pct)
    elif strat == "fvg":
        from backtest.fvg_engine import run
        return run(symbols=symbols, ohlcv=ohlcv,
                   starting_capital=capital, risk_pct=risk_pct)
    elif strat == "bos":
        from backtest.bos_engine import run
        return run(symbols=symbols, ohlcv=ohlcv,
                   starting_capital=capital, risk_pct=risk_pct)
    elif strat == "vwap_band":
        from backtest.vwap_band_engine import run
        return run(symbols=symbols, ohlcv=ohlcv,
                   starting_capital=capital, risk_pct=risk_pct)
    elif strat == "oi_spike":
        from backtest.oi_spike_engine import run
        return run(symbols=symbols, ohlcv=ohlcv, oi=oi,
                   starting_capital=capital, risk_pct=risk_pct)
    return []


def _run_crash_periods(args) -> None:
    """Run all 6 crash periods and print a summary table."""
    strategies = ["main", "microrange", "ema_pullback", "fvg", "bos", "vwap_band", "oi_spike"]
    capital    = args.capital
    risk_pct   = args.risk_pct

    print("\n" + "="*68)
    print("  CONFLUENCE BOT — HISTORICAL CRASH PERIOD BACKTESTS")
    print("  Strategies tested: MAIN, MICRORANGE, EMA PULLBACK, FVG, BOS, VWAP BAND, OI SPIKE")
    print("  Capital: ${:.0f}  |  Risk: {:.1f}% per trade".format(capital, risk_pct * 100))
    print("="*68)

    all_results = {}
    for period_id, from_date, to_date, label in _CRASH_PERIODS:
        all_results[period_id] = _run_one_period(
            period_id, from_date, to_date, label,
            strategies, capital, risk_pct,
        )

    # ── Summary table ─────────────────────────────────────────────────────────
    print("\n\n" + "="*68)
    print("  CRASH PERIOD SUMMARY TABLE")
    print("="*68)
    print(f"  {'Period':<20} {'MAIN':>12} {'MICRORANGE':>12} {'EMA_PULL':>12}")
    print("  " + "-"*56)
    for period_id, from_date, to_date, label in _CRASH_PERIODS:
        r    = all_results.get(period_id, {})
        name = label[:18]
        main_r = r.get("main",        {}).get("return_pct", 0.0)
        mr_r   = r.get("microrange",  {}).get("return_pct", 0.0)
        ep_r   = r.get("ema_pullback",{}).get("return_pct", 0.0)
        print(f"  {name:<20} {main_r:+11.1f}% {mr_r:+11.1f}% {ep_r:+11.1f}%")
    print("="*68)


def _run_walk_forward(args, symbols: list[str]) -> None:
    """Walk-forward validation: train 2023-2024, validate 2025, report overfitting score."""
    from backtest.fetcher import fetch_period_sync
    from backtest.reporter import compute_stats

    strategy = args.strategy if args.strategy not in ("all", "both") else "main"

    TRAIN_FROM = "2023-01-01"
    TRAIN_TO   = "2024-12-31"
    VALID_FROM = "2025-01-01"
    VALID_TO   = "2025-12-31"

    print(f"\n{'='*68}")
    print("  WALK-FORWARD VALIDATION")
    print(f"  Strategy : {strategy}")
    print(f"  Train    : {TRAIN_FROM} → {TRAIN_TO}")
    print(f"  Validate : {VALID_FROM} → {VALID_TO}")
    print(f"  Symbols  : {', '.join(symbols)}")
    print(f"  Capital  : ${args.capital:.0f}  |  Risk: {args.risk_pct*100:.1f}%")
    print(f"{'='*68}")

    def _run_period(from_date: str, to_date: str) -> tuple[list[dict], dict]:
        from_ms = _date_to_ms(from_date)
        to_ms   = _date_to_ms(to_date) + 86_400_000
        data    = fetch_period_sync(symbols, from_ms, to_ms, warmup_days=45)
        trades  = _run_strategy(
            strategy, symbols, data["ohlcv"], data["oi"], data["funding"],
            args.capital, args.risk_pct,
        )
        stats   = compute_stats(trades, starting_capital=args.capital) if trades else {}
        return trades, stats

    print(f"\nFetching TRAIN period ({TRAIN_FROM} → {TRAIN_TO})...")
    train_trades, train_stats = _run_period(TRAIN_FROM, TRAIN_TO)

    print(f"\nFetching VALIDATE period ({VALID_FROM} → {VALID_TO})...")
    valid_trades, valid_stats = _run_period(VALID_FROM, VALID_TO)

    def _g(s: dict, key: str, default=0.0):
        return s.get(key, default)

    t_pf  = _g(train_stats, "profit_factor",   0.0)
    t_wr  = _g(train_stats, "win_rate",         0.0) * 100
    t_ret = _g(train_stats, "total_return_pct", 0.0)
    t_n   = int(_g(train_stats, "total_trades", 0))

    v_pf  = _g(valid_stats, "profit_factor",   0.0)
    v_wr  = _g(valid_stats, "win_rate",         0.0) * 100
    v_ret = _g(valid_stats, "total_return_pct", 0.0)
    v_n   = int(_g(valid_stats, "total_trades", 0))

    # Print cost-aware gross return when available
    def _cost_line(trades: list[dict], capital: float) -> str:
        ct = [t for t in trades if "gross_pnl" in t]
        if not ct:
            return ""
        gross = sum(t["gross_pnl"] for t in ct)
        cost  = sum(t.get("cost_fee", 0) + t.get("cost_slip", 0) + t.get("cost_fund", 0) for t in ct)
        return f"  gross {gross/capital*100:+.1f}%  net after costs {(gross-cost)/capital*100:+.1f}%"

    print(f"\n{'─'*68}")
    print(f"  {'':22s}  {'TRAIN 2023-2024':>20}  {'VALID 2025':>18}")
    print(f"  {'─'*62}")
    print(f"  {'Trades':22s}  {t_n:>20d}  {v_n:>18d}")
    print(f"  {'Win Rate':22s}  {t_wr:>19.1f}%  {v_wr:>17.1f}%")
    print(f"  {'Profit Factor':22s}  {t_pf:>20.2f}  {v_pf:>18.2f}")
    print(f"  {'Return (% capital)':22s}  {t_ret:>+19.2f}%  {v_ret:>+17.2f}%")

    t_cost = _cost_line(train_trades, args.capital)
    v_cost = _cost_line(valid_trades, args.capital)
    if t_cost or v_cost:
        print(f"\n  Cost-adjusted (gross → net):")
        if t_cost:
            print(f"    TRAIN   : {t_cost.strip()}")
        if v_cost:
            print(f"    VALIDATE: {v_cost.strip()}")

    print(f"  {'─'*62}")

    # Overfitting score
    if v_pf > 0 and t_pf > 0:
        ratio   = t_pf / v_pf
        verdict = "⚠  OVERFIT — params may be tuned to train data" if ratio > 1.5 else "✓  OK"
        print(f"\n  Overfitting score : {ratio:.2f}  (train PF / valid PF)  {verdict}")
        print(f"  Threshold         : > 1.5 = likely overfit")
    elif t_n == 0 or v_n == 0:
        print(f"\n  Overfitting score : N/A — {'train' if t_n == 0 else 'validation'} period produced no trades")
    else:
        print(f"\n  Overfitting score : N/A — PF undefined (no losing trades in one period)")
    print(f"{'='*68}\n")


def _run_date_range(args, symbols: list[str]) -> None:
    """Run backtest for a specific --from-date / --to-date window."""
    from backtest.fetcher import fetch_period_sync
    from backtest.reporter import compute_stats, print_report
    import json, os as _os

    from_ms = _date_to_ms(args.from_date)
    to_ms   = _date_to_ms(args.to_date) + 86_400_000

    print(f"\nDate range : {args.from_date} -> {args.to_date}")
    print(f"Symbols    : {symbols}")
    print("Fetching historical data (with 45-day warmup)...\n")

    data    = fetch_period_sync(symbols, from_ms, to_ms, warmup_days=45)
    ohlcv   = data["ohlcv"]
    oi      = data["oi"]
    funding = data["funding"]

    for sym in symbols:
        n = len(ohlcv.get(f"{sym}:1h", []))
        print(f"  {sym}  1h bars: {n}  |  OI: {len(oi.get(sym, []))}  |  funding: {len(funding.get(sym, []))}")
    print()

    strat = args.strategy if args.strategy != "all" else "main"
    trades = _run_strategy(strat, symbols, ohlcv, oi, funding, args.capital, args.risk_pct)
    if not trades:
        print("No trades generated.")
        return

    from backtest.reporter import compute_stats, print_report
    stats = compute_stats(trades, starting_capital=args.capital)
    print_report(stats, trades=trades, starting_capital=args.capital)
    _print_cost_analysis(trades, args.capital)


def main() -> None:
    parser = argparse.ArgumentParser(description="confluence_bot backtester")
    parser.add_argument(
        "--symbols", default="BTCUSDT,ETHUSDT,SOLUSDT,BNBUSDT,AVAXUSDT,ADAUSDT,DOTUSDT,DOGEUSDT,SUIUSDT",
        help="Comma-separated symbols (default: all 9 configured symbols)",
    )
    parser.add_argument(
        "--symbol", dest="symbols",
        default=argparse.SUPPRESS,
        help="Alias for --symbols (single symbol shorthand)",
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
        "--from-date", dest="from_date", default=None,
        help="Backtest start date YYYY-MM-DD (use with --to-date for crash period tests)",
    )
    parser.add_argument(
        "--to-date", dest="to_date", default=None,
        help="Backtest end date YYYY-MM-DD (use with --from-date)",
    )
    parser.add_argument(
        "--crash-periods", action="store_true",
        help="Run all 6 historical crash periods automatically",
    )
    parser.add_argument(
        "--walk-forward", action="store_true", dest="walk_forward",
        help="Walk-forward validation: train 2023-2024, validate 2025, print overfitting score",
    )
    parser.add_argument(
        "--strategy",
        choices=["main", "leadlag", "both", "microrange", "session", "insidebar", "funding",
                 "sweep", "ema_pullback", "zone", "fvg", "bos", "vwap_band", "oi_spike",
                 "new", "all"],
        default="main",
        help="Which strategy to backtest (default: main). 'new' = sweep+ema_pullback+zone+fvg+bos+vwap_band+oi_spike",
    )
    args = parser.parse_args()

    _ALL_SYMS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "AVAXUSDT",
                 "ADAUSDT", "DOTUSDT", "DOGEUSDT", "SUIUSDT"]
    raw = getattr(args, "symbols", "BTCUSDT")
    symbols = _ALL_SYMS if raw.upper() == "ALL" else [s.strip().upper() for s in raw.split(",")]

    # ── Walk-forward validation mode ──────────────────────────────────────────
    if args.walk_forward:
        _run_walk_forward(args, symbols)
        return

    # ── Crash periods mode ─────────────────────────────────────────────────────
    if args.crash_periods:
        _run_crash_periods(args)
        return

    # ── Date range mode ────────────────────────────────────────────────────────
    if args.from_date and args.to_date:
        _run_date_range(args, symbols)
        return

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
            _print_cost_analysis(mr_trades, args.capital)
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


    # ── Step 2g: sweep reversal backtest ──────────────────────────────────────
    if args.strategy in ("sweep", "new", "all"):
        print(f"\n{'='*68}")
        print("Running SWEEP REVERSAL backtest (15m stop-hunt reversals)...\n")
        print(f"Starting capital : ${args.capital:,.2f}")
        print(f"Risk per trade   : {args.risk_pct*100:.1f}% of equity")
        print(f"Detection        : 15m swing pivot sweep + volume + RSI")
        print(f"RR               : 2.5×  |  SL = wick extreme  |  max hold 2H\n")

        from backtest.sweep_engine import run as run_sw
        sw_trades = run_sw(symbols=symbols, ohlcv=ohlcv,
                           starting_capital=args.capital, risk_pct=args.risk_pct)
        if not sw_trades:
            print("No sweep reversal trades generated.")
        else:
            sw_stats = compute_stats(sw_trades, starting_capital=args.capital)
            print_report(sw_stats, trades=sw_trades, starting_capital=args.capital)
            _print_cost_analysis(sw_trades, args.capital)
            sw_path = _os.path.join(_os.path.dirname(__file__), "results_sweep.json")
            with open(sw_path, "w") as f:
                json.dump({"stats": sw_stats, "trades": sw_trades, "symbols": symbols,
                           "capital": args.capital, "risk_pct": args.risk_pct,
                           "strategy": "sweep"}, f, default=str)
            print(f"Results saved to {sw_path}")

    # ── Step 2h: 15m EMA pullback backtest ────────────────────────────────────
    if args.strategy in ("ema_pullback", "new", "all"):
        print(f"\n{'='*68}")
        print("Running EMA PULLBACK backtest (15m EMA21 trend-continuation)...\n")
        print(f"Starting capital : ${args.capital:,.2f}")
        print(f"Risk per trade   : {args.risk_pct*100:.1f}% of equity")
        print(f"Detection        : 4H macro bias + 15m pullback to EMA21")
        print(f"RR               : 2.5×  |  SL below EMA21  |  max hold 2H\n")

        from backtest.ema_pullback_engine import run as run_ep
        ep_trades = run_ep(symbols=symbols, ohlcv=ohlcv,
                           starting_capital=args.capital, risk_pct=args.risk_pct)
        if not ep_trades:
            print("No EMA pullback trades generated.")
        else:
            ep_stats = compute_stats(ep_trades, starting_capital=args.capital)
            print_report(ep_stats, trades=ep_trades, starting_capital=args.capital)
            _print_cost_analysis(ep_trades, args.capital)
            ep_path = _os.path.join(_os.path.dirname(__file__), "results_ema_pullback.json")
            with open(ep_path, "w") as f:
                json.dump({"stats": ep_stats, "trades": ep_trades, "symbols": symbols,
                           "capital": args.capital, "risk_pct": args.risk_pct,
                           "strategy": "ema_pullback"}, f, default=str)
            print(f"Results saved to {ep_path}")

    # ── Step 2i: HTF demand/supply zone backtest ───────────────────────────────
    if args.strategy in ("zone", "new", "all"):
        print(f"\n{'='*68}")
        print("Running ZONE RETEST backtest (4H demand/supply zone reactions)...\n")
        print(f"Starting capital : ${args.capital:,.2f}")
        print(f"Risk per trade   : {args.risk_pct*100:.1f}% of equity")
        print(f"Detection        : 4H consolidation-before-impulse zones")
        print(f"RR               : 2.5×  |  SL below/above zone  |  max hold 12H\n")

        from backtest.zone_engine import run as run_zn
        zn_trades = run_zn(symbols=symbols, ohlcv=ohlcv,
                           starting_capital=args.capital, risk_pct=args.risk_pct)
        if not zn_trades:
            print("No zone retest trades generated.")
        else:
            zn_stats = compute_stats(zn_trades, starting_capital=args.capital)
            print_report(zn_stats, trades=zn_trades, starting_capital=args.capital)
            zn_path = _os.path.join(_os.path.dirname(__file__), "results_zone.json")
            with open(zn_path, "w") as f:
                json.dump({"stats": zn_stats, "trades": zn_trades, "symbols": symbols,
                           "capital": args.capital, "risk_pct": args.risk_pct,
                           "strategy": "zone"}, f, default=str)
            print(f"Results saved to {zn_path}")


    # ── Step 2j: FVG Fill backtest ────────────────────────────────────────────
    if args.strategy in ("fvg", "new", "all"):
        print(f"\n{'='*68}")
        print("Running FVG FILL backtest (1H fair value gap retests)...\n")
        print(f"Starting capital : ${args.capital:,.2f}")
        print(f"Risk per trade   : {args.risk_pct*100:.1f}% of equity")
        print(f"Detection        : 3-bar imbalance gap + 4H EMA21 + RSI ≤45/≥55")
        print(f"RR               : 2.0×  |  SL = gap edge  |  max hold 24H\n")

        from backtest.fvg_engine import run as run_fvg
        fvg_trades = run_fvg(symbols=symbols, ohlcv=ohlcv,
                             starting_capital=args.capital, risk_pct=args.risk_pct)
        if not fvg_trades:
            print("No FVG Fill trades generated.")
        else:
            fvg_stats = compute_stats(fvg_trades, starting_capital=args.capital)
            print_report(fvg_stats, trades=fvg_trades, starting_capital=args.capital)
            _print_cost_analysis(fvg_trades, args.capital)
            fvg_path = _os.path.join(_os.path.dirname(__file__), "results_fvg.json")
            with open(fvg_path, "w") as f:
                json.dump({"stats": fvg_stats, "trades": fvg_trades, "symbols": symbols,
                           "capital": args.capital, "risk_pct": args.risk_pct,
                           "strategy": "fvg"}, f, default=str)
            print(f"Results saved to {fvg_path}")

    # ── Step 2k: BOS/CHoCH backtest ───────────────────────────────────────────
    if args.strategy in ("bos", "new", "all"):
        print(f"\n{'='*68}")
        print("Running BOS/CHoCH backtest (1H structure break entries)...\n")
        print(f"Starting capital : ${args.capital:,.2f}")
        print(f"Risk per trade   : {args.risk_pct*100:.1f}% of equity")
        print(f"Detection        : pivot_n=3 swing break + volume spike + 4H HTF")
        print(f"RR               : 2.5×  |  SL = prior swing  |  max hold 48H\n")

        from backtest.bos_engine import run as run_bos
        bos_trades = run_bos(symbols=symbols, ohlcv=ohlcv,
                             starting_capital=args.capital, risk_pct=args.risk_pct)
        if not bos_trades:
            print("No BOS/CHoCH trades generated.")
        else:
            bos_stats = compute_stats(bos_trades, starting_capital=args.capital)
            print_report(bos_stats, trades=bos_trades, starting_capital=args.capital)
            bos_path = _os.path.join(_os.path.dirname(__file__), "results_bos.json")
            with open(bos_path, "w") as f:
                json.dump({"stats": bos_stats, "trades": bos_trades, "symbols": symbols,
                           "capital": args.capital, "risk_pct": args.risk_pct,
                           "strategy": "bos"}, f, default=str)
            print(f"Results saved to {bos_path}")

    # ── Step 2l: VWAP Band Reversion backtest ─────────────────────────────────
    if args.strategy in ("vwap_band", "new", "all"):
        print(f"\n{'='*68}")
        print("Running VWAP BAND REVERSION backtest (15m ±2σ band touch rejections)...\n")
        print(f"Starting capital : ${args.capital:,.2f}")
        print(f"Risk per trade   : {args.risk_pct*100:.1f}% of equity")
        print(f"Detection        : rolling VWAP ±2σ + ADX<30 + RSI ≤35/≥65")
        print(f"TP               : VWAP midline (dynamic)  |  max hold 2H\n")

        from backtest.vwap_band_engine import run as run_vb
        vb_trades = run_vb(symbols=symbols, ohlcv=ohlcv,
                           starting_capital=args.capital, risk_pct=args.risk_pct)
        if not vb_trades:
            print("No VWAP Band trades generated.")
        else:
            vb_stats = compute_stats(vb_trades, starting_capital=args.capital)
            print_report(vb_stats, trades=vb_trades, starting_capital=args.capital)
            _print_cost_analysis(vb_trades, args.capital)
            vb_path = _os.path.join(_os.path.dirname(__file__), "results_vwap_band.json")
            with open(vb_path, "w") as f:
                json.dump({"stats": vb_stats, "trades": vb_trades, "symbols": symbols,
                           "capital": args.capital, "risk_pct": args.risk_pct,
                           "strategy": "vwap_band"}, f, default=str)
            print(f"Results saved to {vb_path}")

    # ── Step 2m: OI Spike Fade backtest ───────────────────────────────────────
    if args.strategy in ("oi_spike", "new", "all"):
        print(f"\n{'='*68}")
        print("Running OI SPIKE FADE backtest (liquidation cascade reversals)...\n")
        print(f"Starting capital : ${args.capital:,.2f}")
        print(f"Risk per trade   : {args.risk_pct*100:.1f}% of equity")
        print(f"Detection        : OI spike ≥15% (or vol proxy) + wick + EMA + RSI")
        print(f"RR               : 2.0×  |  SL = wick extreme  |  max hold 2H\n")

        from backtest.oi_spike_engine import run as run_os
        os_trades = run_os(symbols=symbols, ohlcv=ohlcv, oi=oi,
                           starting_capital=args.capital, risk_pct=args.risk_pct)
        if not os_trades:
            print("No OI Spike trades generated.")
        else:
            os_stats = compute_stats(os_trades, starting_capital=args.capital)
            print_report(os_stats, trades=os_trades, starting_capital=args.capital)
            os_path = _os.path.join(_os.path.dirname(__file__), "results_oi_spike.json")
            with open(os_path, "w") as f:
                json.dump({"stats": os_stats, "trades": os_trades, "symbols": symbols,
                           "capital": args.capital, "risk_pct": args.risk_pct,
                           "strategy": "oi_spike"}, f, default=str)
            print(f"Results saved to {os_path}")


if __name__ == "__main__":
    main()
