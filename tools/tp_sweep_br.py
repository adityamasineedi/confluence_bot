"""
tools/tp_sweep_br.py
Sweep rr_ratio from 2.0 → 3.0 in 0.1 steps for breakout_retest.

For each rr_ratio, runs the full backtest engine on BTC and ETH and reports:
  - n trades, WR%, PF, net R, total $ profit (assuming $50 fixed risk)
  - whether the bucket beats the current 2.2 baseline

Picks the rr_ratio that maximizes net dollar profit (NOT just PF — high PF with
low trade count loses to medium PF with high count).
"""
import argparse
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from backtest.engine import load, run_breakout_retest, FEE_RT, FUNDING_PER_BAR_5M


def _ms(date_str: str) -> int:
    return int(
        datetime.strptime(date_str, "%Y-%m-%d")
        .replace(tzinfo=timezone.utc)
        .timestamp() * 1000
    )


def _stats(trades, rr_ratio: float, fixed_risk_usdt: float = 50.0) -> dict:
    if not trades:
        return {"n": 0}
    n        = len(trades)
    wins     = sum(1 for t in trades if t.outcome == "TP")
    losses   = sum(1 for t in trades if t.outcome == "SL")
    tos      = sum(1 for t in trades if t.outcome == "TIMEOUT")
    gross_w  = sum(t.pnl_r for t in trades if t.pnl_r > 0)
    gross_l  = sum(-t.pnl_r for t in trades if t.pnl_r < 0)
    pf       = (gross_w / gross_l) if gross_l > 0 else float("inf")
    wr       = wins / n * 100
    net_r    = gross_w - gross_l
    net_usdt = net_r * fixed_risk_usdt
    return {
        "n":       n,
        "wins":    wins,
        "losses":  losses,
        "tos":     tos,
        "wr":      wr,
        "pf":      pf,
        "net_r":   net_r,
        "net_usdt": net_usdt,
    }


def _pf_str(pf: float) -> str:
    return "inf" if pf == float("inf") else f"{pf:.2f}"


def sweep(symbol: str, from_date: str, to_date: str) -> tuple[float, dict]:
    data = load(symbol)
    if data is None:
        print(f"  No data for {symbol}")
        return 0.0, {}
    btc_data = load("BTCUSDT") if symbol != "BTCUSDT" else data
    from_ts = _ms(from_date)
    to_ts   = _ms(to_date)

    print(f"\n{'='*72}")
    print(f"  TP SWEEP — {symbol}  ({from_date} → {to_date})")
    print(f"{'='*72}")
    print(f"  {'rr':>4}  {'n':>5} {'WR%':>6} {'PF':>6} {'netR':>8} {'$net':>10}  verdict")
    print(f"  {'-'*4}  {'-'*5} {'-'*6} {'-'*6} {'-'*8} {'-'*10}  -------")

    results: dict[float, dict] = {}
    baseline = None
    for rr_x10 in range(20, 31):  # 2.0 → 3.0 inclusive
        rr = rr_x10 / 10.0
        trades = run_breakout_retest(symbol, data, btc_data, from_ts, to_ts, rr_ratio=rr)
        st = _stats(trades, rr)
        results[rr] = st
        if abs(rr - 2.2) < 1e-6:
            baseline = st
        verdict = ""
        if baseline is not None and st["n"] > 0:
            d_usdt = st["net_usdt"] - baseline["net_usdt"]
            verdict = f"  {d_usdt:+8.2f}$ vs 2.2"
        print(f"  {rr:>4.1f}  {st['n']:>5} {st['wr']:>5.1f}% "
              f"{_pf_str(st['pf']):>6} {st['net_r']:>+8.1f} {st['net_usdt']:>+10.2f}{verdict}")

    # Pick the best by net_usdt
    best_rr = max(results, key=lambda r: results[r].get("net_usdt", -1e9))
    best = results[best_rr]
    print(f"\n  → BEST: rr={best_rr:.1f}  net=${best['net_usdt']:+.2f}  "
          f"PF={_pf_str(best['pf'])}  WR={best['wr']:.1f}%  n={best['n']}")
    if baseline is not None:
        delta = best["net_usdt"] - baseline["net_usdt"]
        delta_pf = best["pf"] - baseline["pf"]
        print(f"     vs current 2.2: net {delta:+.2f}$  PF {delta_pf:+.2f}")
    return best_rr, best


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--from-date", default="2023-01-01")
    ap.add_argument("--to-date",   default="2026-04-01")
    ap.add_argument("--symbol", default="BOTH",
                    help="BTCUSDT, ETHUSDT, or BOTH (default)")
    args = ap.parse_args()
    syms = ["BTCUSDT", "ETHUSDT"] if args.symbol.upper() == "BOTH" else [args.symbol.upper()]

    summary = {}
    for s in syms:
        best_rr, best_st = sweep(s, args.from_date, args.to_date)
        summary[s] = (best_rr, best_st)

    print(f"\n{'='*72}")
    print("  SWEEP SUMMARY")
    print(f"{'='*72}")
    for s, (rr, st) in summary.items():
        print(f"  {s:<10}  best rr={rr:.1f}  net=${st['net_usdt']:+.2f}  "
              f"PF={_pf_str(st['pf'])}  WR={st['wr']:.1f}%  n={st['n']}")


if __name__ == "__main__":
    main()
