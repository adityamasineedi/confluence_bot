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
  python backtest/run.py --symbol BTCUSDT --strategy liq_sweep --show-balance
  python backtest/run.py --symbol BTCUSDT --strategy liq_sweep \
      --capital 5000 --risk-usdt 50 --show-balance
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


# -- Vol-ratio comparison -----------------------------------------------------

def print_comparison(raw: dict, mc: dict, symbol: str, strategy: str,
                     mc_thresh: float = 2.0) -> None:
    """Print side-by-side comparison of unfiltered vs vol-ratio filtered."""
    skipped  = raw["n"] - mc["n"]
    skip_pct = skipped / raw["n"] * 100 if raw["n"] > 0 else 0

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


# -- Dollar P&L ---------------------------------------------------------------

def compute_dollar_stats(trades: list,
                         capital: float,
                         risk_usdt: float) -> dict:
    """Convert R-based trade results to dollar P&L.

    Each trade has pnl_r (R multiple).
    Dollar P&L = pnl_r x risk_usdt
    """
    if not trades:
        return {
            "starting_capital": capital,
            "final_balance":    capital,
            "total_profit":     0.0,
            "total_return_pct": 0.0,
            "max_drawdown_usd": 0.0,
            "max_drawdown_pct": 0.0,
            "peak_balance":     capital,
            "lowest_balance":   capital,
            "best_trade_usd":   0.0,
            "worst_trade_usd":  0.0,
            "avg_win_usd":      0.0,
            "avg_loss_usd":     0.0,
            "max_consec_wins":  0,
            "max_consec_loss":  0,
            "balance_history":  [capital],
            "trade_pnls":       [],
        }

    balance = capital
    peak    = capital
    balance_history = [capital]
    trade_pnls      = []

    max_dd_usd  = 0.0
    max_dd_pct  = 0.0
    consec_wins = consec_loss = 0
    max_cw = max_cl = 0

    for trade in sorted(trades, key=lambda t: t.bar_idx):
        dollar_pnl = round(trade.pnl_r * risk_usdt, 2)
        balance    = round(balance + dollar_pnl, 2)
        trade_pnls.append(dollar_pnl)
        balance_history.append(balance)

        if balance > peak:
            peak = balance

        drawdown_usd = peak - balance
        drawdown_pct = drawdown_usd / peak * 100 if peak > 0 else 0

        if drawdown_usd > max_dd_usd:
            max_dd_usd = drawdown_usd
            max_dd_pct = drawdown_pct

        if dollar_pnl > 0:
            consec_wins += 1
            consec_loss  = 0
            max_cw = max(max_cw, consec_wins)
        else:
            consec_loss += 1
            consec_wins  = 0
            max_cl = max(max_cl, consec_loss)

    wins   = [p for p in trade_pnls if p > 0]
    losses = [p for p in trade_pnls if p < 0]

    return {
        "starting_capital": capital,
        "final_balance":    balance,
        "total_profit":     round(balance - capital, 2),
        "total_return_pct": round((balance - capital) / capital * 100, 2),
        "max_drawdown_usd": round(max_dd_usd, 2),
        "max_drawdown_pct": round(max_dd_pct, 2),
        "peak_balance":     round(peak, 2),
        "lowest_balance":   round(min(balance_history), 2),
        "best_trade_usd":   round(max(trade_pnls), 2) if trade_pnls else 0,
        "worst_trade_usd":  round(min(trade_pnls), 2) if trade_pnls else 0,
        "avg_win_usd":      round(sum(wins) / len(wins), 2) if wins else 0,
        "avg_loss_usd":     round(sum(losses) / len(losses), 2) if losses else 0,
        "max_consec_wins":  max_cw,
        "max_consec_loss":  max_cl,
        "balance_history":  balance_history,
        "trade_pnls":       trade_pnls,
    }


def print_dollar_report(stats: dict, symbol: str, strategy: str,
                        risk_usdt: float, trades: list,
                        data: dict | None = None,
                        show_each_trade: bool = False) -> None:
    """Print full dollar-based P&L report."""
    s = stats
    profit_sign = "+" if s["total_profit"] >= 0 else ""

    print(f"\n{'='*65}")
    print(f"  DOLLAR REPORT: {symbol} {strategy}")
    print(f"  Capital: ${s['starting_capital']:,.2f}  |  Risk/trade: ${risk_usdt:.2f}")
    print(f"{'='*65}")
    print(f"  Starting balance : ${s['starting_capital']:>10,.2f}")
    print(f"  Final balance    : ${s['final_balance']:>10,.2f}")
    print(f"  Total profit     : {profit_sign}${s['total_profit']:>9,.2f}"
          f"  ({profit_sign}{s['total_return_pct']:.1f}%)")
    print(f"  Peak balance     : ${s['peak_balance']:>10,.2f}")
    print(f"  Lowest balance   : ${s['lowest_balance']:>10,.2f}")
    print(f"  {'-'*61}")
    print(f"  Max drawdown     : -${s['max_drawdown_usd']:>9,.2f}"
          f"  (-{s['max_drawdown_pct']:.1f}%)")
    print(f"  Best trade       : +${s['best_trade_usd']:>9,.2f}")
    print(f"  Worst trade      : -${abs(s['worst_trade_usd']):>9,.2f}")
    print(f"  Avg win          : +${s['avg_win_usd']:>9,.2f}")
    print(f"  Avg loss         : -${abs(s['avg_loss_usd']):>9,.2f}")
    print(f"  Max consec wins  : {s['max_consec_wins']:>10}")
    print(f"  Max consec losses: {s['max_consec_loss']:>10}")

    n_trades = len(trades)
    if n_trades > 0:
        # Estimate annualised figures from the backtest period
        trades_per_year = n_trades / 3.0
        profit_per_year = s["total_profit"] / 3.0
        print(f"  {'-'*61}")
        print(f"  Trades/year (est): {trades_per_year:>9.1f}")
        print(f"  Profit/year (est): {profit_sign}${profit_per_year:>9,.2f}")
        print(f"  Profit/month(est): {profit_sign}${profit_per_year / 12:>9,.2f}")
        print(f"  Profit/day  (est): {profit_sign}${profit_per_year / 365:>9,.2f}")

    if show_each_trade and trades:
        print(f"\n  {'#':<4} {'Date':<12} {'Dir':<6} {'Outcome':<8}"
              f" {'P&L':>10} {'Balance':>12}")
        print(f"  {'-'*56}")

        sorted_trades = sorted(trades, key=lambda t: t.bar_idx)
        balance = s["starting_capital"]

        for i, (trade, pnl) in enumerate(
            zip(sorted_trades, s["trade_pnls"]), 1
        ):
            balance   = round(balance + pnl, 2)
            pnl_str   = f"+${pnl:,.2f}" if pnl >= 0 else f"-${abs(pnl):,.2f}"
            icon      = "TP" if pnl > 0 else "SL" if trade.outcome == "SL" else "TO"
            direction = getattr(trade, "direction", "?")
            date_str  = bar_date(data, symbol, trade.bar_idx) if data else str(trade.bar_idx)

            print(f"  {i:<4} {date_str:<12} {direction:<6} {icon:<8}"
                  f" {pnl_str:>10} ${balance:>11,.2f}")

    print(f"{'='*65}\n")


# -- Helpers ------------------------------------------------------------------

def bar_date(data: dict, symbol: str, bar_idx: int) -> str:
    """Convert bar index to readable UTC date string."""
    key  = f"{symbol}:1h"
    bars = data.get(key)
    if bars is None or bar_idx >= len(bars):
        return "???"
    ts = bars[bar_idx, 5]  # TS column
    return datetime.fromtimestamp(ts / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


# -- Main ---------------------------------------------------------------------

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
    ap.add_argument("--capital", type=float, default=5000.0,
                    help="Starting capital in USDT (default: 5000)")
    ap.add_argument("--risk-usdt", type=float, default=50.0,
                    help="Fixed risk per trade in USDT (default: 50)")
    ap.add_argument("--show-balance", action="store_true", default=False,
                    help="Show dollar P&L report with running balance")
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
    if args.show_balance:
        print(f"  Capital  : ${args.capital:,.2f}  |  Risk/trade: ${args.risk_usdt:.2f}")
    print(f"{'='*65}\n")

    mc_threshold  = args.mc_threshold
    mc_compare    = args.mc_compare
    mc_thresh_val = mc_threshold

    all_trades:  list = []
    result_rows: list = []

    for symbol in symbols:
        data = load(symbol)
        if data is None:
            print(f"  [MISSING] {symbol}.json - run: python backtest/fetch.py")
            continue

        for strategy in strategies:
            if mc_compare:
                # -- Side-by-side MC comparison --------------------------------
                t0 = time.time()
                trades_raw = run_strategy(symbol, strategy, data, btc_data,
                                          from_ts, to_ts, mc_threshold=-1.0)
                stats_raw  = compute_stats(trades_raw)

                trades_mc  = run_strategy(symbol, strategy, data, btc_data,
                                          from_ts, to_ts,
                                          mc_threshold=mc_thresh_val)
                stats_mc   = compute_stats(trades_mc)
                elapsed    = time.time() - t0

                eff_thresh = _resolve_mc_threshold(strategy, mc_thresh_val)
                print_comparison(stats_raw, stats_mc, symbol, strategy,
                                 mc_thresh=eff_thresh)

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
                                    "_trades": trades_raw, **stats_raw})
                print(f"  ({elapsed:.1f}s)")

            else:
                # -- Normal single run -----------------------------------------
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

                # Dollar report (always computed, printed when --show-balance)
                if args.show_balance and trades:
                    ds = compute_dollar_stats(trades, args.capital, args.risk_usdt)
                    print_dollar_report(ds, symbol, strategy, args.risk_usdt,
                                        trades, data=data,
                                        show_each_trade=True)

                all_trades.extend(trades)
                result_rows.append({"symbol": symbol, "strategy": strategy,
                                    "_trades": trades, **s})

    if not result_rows:
        print("  No results — check that backtest/data/ files exist.\n")
        return

    # -- Summary table ---------------------------------------------------------
    capital   = args.capital
    risk_usdt = args.risk_usdt

    print(f"\n{'='*80}")
    print(f"  {'SYMBOL':<10} {'STRATEGY':<20} {'N':>5} {'WR':>6}"
          f" {'PF':>6} {'PROFIT$':>10} {'RETURN':>8}  VERDICT")
    print(f"  {'-'*75}")
    for r in sorted(result_rows, key=lambda x: x["pf"], reverse=True):
        ds    = compute_dollar_stats(r.get("_trades", []), capital, risk_usdt)
        vrd   = verdict(r["pf"])
        psign = "+" if ds["total_profit"] >= 0 else ""
        print(
            f"  {r['symbol']:<10} {r['strategy']:<20} {r['n']:>5}"
            f" {r['wr']:>5.1f}% {r['pf']:>6.2f}"
            f" {psign}${ds['total_profit']:>8,.0f}"
            f" {psign}{ds['total_return_pct']:>6.1f}%  {vrd}"
        )

    if all_trades:
        t  = compute_stats(all_trades)
        ds = compute_dollar_stats(all_trades, capital, risk_usdt)
        psign = "+" if ds["total_profit"] >= 0 else ""
        print(f"  {'-'*75}")
        print(
            f"  {'OVERALL':<31} {t['n']:>5}"
            f" {t['wr']:>5.1f}% {t['pf']:>6.2f}"
            f" {psign}${ds['total_profit']:>8,.0f}"
            f" {psign}{ds['total_return_pct']:>6.1f}%  {verdict(t['pf'])}"
        )
    print(f"{'='*80}\n")


if __name__ == "__main__":
    main()
