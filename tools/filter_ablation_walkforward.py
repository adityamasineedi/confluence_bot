"""
tools/filter_ablation_walkforward.py
Walk-forward validation of filter ablation findings.

For each filter, runs FOUR backtests:
  IS-baseline   = 2023-01-01 → 2023-12-31, baseline (no filters)
  IS-with       = 2023-01-01 → 2023-12-31, baseline + this ONE filter
  OOS-baseline  = 2024-01-01 → 2026-04-01, baseline (no filters)
  OOS-with      = 2024-01-01 → 2026-04-01, baseline + this ONE filter

Verdict logic:
  ROBUST_REMOVE  — removing helps in BOTH IS and OOS  → safe to remove
  FRAGILE_REMOVE — removing helps IS but hurts OOS    → overfit, KEEP
  KEEP           — filter helps in both IS and OOS    → essential, KEEP
  NEUTRAL        — < $1000 swing in either direction  → no impact

Designed to answer: "Is the in-sample ablation finding real or overfit?"
"""
import argparse
import copy
import os
import sys
import time as _time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backtest.engine import _CFG, run_breakout_retest, load


def _ms(d: str) -> int:
    return int(datetime.strptime(d, "%Y-%m-%d")
               .replace(tzinfo=timezone.utc).timestamp() * 1000)


# Snapshot original production config
_ORIG_BR = copy.deepcopy(_CFG.get("breakout_retest", {}))

# Baseline: all optional filters relaxed/disabled
BASELINE = {
    **_ORIG_BR,
    "require_breakout_confirm": False,
    "min_retest_body_ratio":    0.0,
    "vol_spike_mult":           1.0,
    "exhaustion_pct":           0.10,
    "exhaustion_bars":          6,
    "max_boundary_touches":     99,
    "atr_mult_max":             99.0,
    "choppy_atr_mult":          99.0,
    "crash_cooldown_pct":       99.0,
    "min_width_pct":            0.0001,
    "max_width_pct":            0.50,
    "btc_confirm_for_alts":     False,
    "max_entries_per_30min":    999,
    "max_trades_per_day":       999,
    "cooldown_mins":            0,
}

# Top 5 filters from ablation, ranked by in-sample $ impact (worst to best)
FILTERS_TO_TEST = [
    ("exhaustion_4h",     {"exhaustion_pct": 0.025}),
    ("retest_body_ratio", {"min_retest_body_ratio": 0.40}),
    ("breakout_confirm",  {"require_breakout_confirm": True}),
    ("boundary_touches",  {"max_boundary_touches": 4}),
    ("btc_confirm_alts",  {"btc_confirm_for_alts": True}),
]


def set_cfg(overrides: dict) -> None:
    _CFG["breakout_retest"].clear()
    _CFG["breakout_retest"].update(BASELINE)
    _CFG["breakout_retest"].update(overrides)


def restore_production() -> None:
    _CFG["breakout_retest"].clear()
    _CFG["breakout_retest"].update(_ORIG_BR)


def run_test(coins: list[str], from_d: str, to_d: str) -> dict:
    btc_data = load("BTCUSDT")
    from_ms = _ms(from_d)
    to_ms = _ms(to_d)
    all_trades = []
    for sym in coins:
        data = btc_data if sym == "BTCUSDT" else load(sym)
        if data is None:
            continue
        trades = run_breakout_retest(sym, data, btc_data, from_ms, to_ms)
        all_trades.extend(trades)
    if not all_trades:
        return {"n": 0, "wr": 0, "pf": 0, "net_r": 0, "net_usdt": 0}
    n = len(all_trades)
    wins = sum(1 for t in all_trades if t.outcome == "TP")
    gw = sum(t.pnl_r for t in all_trades if t.pnl_r > 0)
    gl = sum(-t.pnl_r for t in all_trades if t.pnl_r < 0)
    pf = gw / gl if gl > 0 else 999.0
    return {
        "n":        n,
        "wr":       round(wins / n * 100, 1),
        "pf":       round(pf, 2),
        "net_r":    round(gw - gl, 1),
        "net_usdt": round((gw - gl) * 50, 0),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default="BTCUSDT,ETHUSDT,SOLUSDT,BNBUSDT,XRPUSDT,LINKUSDT,DOGEUSDT,SUIUSDT")
    ap.add_argument("--is-from",  default="2023-01-01")
    ap.add_argument("--is-to",    default="2023-12-31")
    ap.add_argument("--oos-from", default="2024-01-01")
    ap.add_argument("--oos-to",   default="2026-04-01")
    args = ap.parse_args()

    coins = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]

    print(f"\n{'='*92}")
    print(f"  WALK-FORWARD FILTER ABLATION")
    print(f"  Coins: {', '.join(coins)}")
    print(f"  IS:  {args.is_from} → {args.is_to}")
    print(f"  OOS: {args.oos_from} → {args.oos_to}")
    print(f"{'='*92}")

    t0 = _time.time()

    # Step 1 — IS and OOS baselines (no filters)
    print(f"\n  [1/{len(FILTERS_TO_TEST)+1}] Computing IS and OOS baselines (no filters)...")
    set_cfg({})
    is_baseline  = run_test(coins, args.is_from,  args.is_to)
    oos_baseline = run_test(coins, args.oos_from, args.oos_to)
    print(f"    IS  baseline: n={is_baseline['n']:>5}  WR={is_baseline['wr']:.1f}%  "
          f"PF={is_baseline['pf']}  ${is_baseline['net_usdt']:+,.0f}")
    print(f"    OOS baseline: n={oos_baseline['n']:>5}  WR={oos_baseline['wr']:.1f}%  "
          f"PF={oos_baseline['pf']}  ${oos_baseline['net_usdt']:+,.0f}")

    # Step 2 — For each filter, IS and OOS with that filter ENABLED
    print(f"\n{'='*92}")
    print(f"  PER-FILTER WALK-FORWARD")
    print(f"{'='*92}")
    print(f"  {'filter':<20} | {'IS Δ$':>10} {'IS ΔPF':>8} | "
          f"{'OOS Δ$':>10} {'OOS ΔPF':>8}  verdict")
    print(f"  {'-'*20} | {'-'*10} {'-'*8} | "
          f"{'-'*10} {'-'*8}  -------")

    rows = []
    for i, (fname, overrides) in enumerate(FILTERS_TO_TEST, 1):
        print(f"  [{i+1}/{len(FILTERS_TO_TEST)+1}] testing {fname}...", end="\r", flush=True)
        set_cfg(overrides)
        is_with  = run_test(coins, args.is_from,  args.is_to)
        oos_with = run_test(coins, args.oos_from, args.oos_to)

        is_d_usd  = is_with["net_usdt"]  - is_baseline["net_usdt"]
        is_d_pf   = is_with["pf"]        - is_baseline["pf"]
        oos_d_usd = oos_with["net_usdt"] - oos_baseline["net_usdt"]
        oos_d_pf  = oos_with["pf"]       - oos_baseline["pf"]

        # Verdict — note we're testing if ENABLING the filter helps.
        # If enabling helps both → KEEP filter
        # If enabling hurts both → REMOVE filter (ROBUST removal)
        # If enabling helps IS but hurts OOS → KEEP (was overfit to remove)
        if is_d_usd > 1000 and oos_d_usd > 1000:
            verdict = "KEEP (robust)"
        elif is_d_usd < -1000 and oos_d_usd < -1000:
            verdict = "REMOVE ✓ robust"
        elif is_d_usd < -1000 and oos_d_usd > 1000:
            verdict = "KEEP (overfit removal)"
        elif is_d_usd > 1000 and oos_d_usd < -1000:
            verdict = "REMOVE (overfit keep)"
        else:
            verdict = "neutral"

        print(f"  {fname:<20} | {is_d_usd:>+10,.0f} {is_d_pf:>+8.2f} | "
              f"{oos_d_usd:>+10,.0f} {oos_d_pf:>+8.2f}  {verdict}")
        rows.append((fname, is_d_usd, oos_d_usd, verdict, overrides))

    elapsed = _time.time() - t0
    print(f"\n  Completed in {elapsed:.0f}s")

    # Recommendation summary
    print(f"\n{'='*92}")
    print(f"  RECOMMENDATIONS")
    print(f"{'='*92}")

    safe_removals  = [r for r in rows if "REMOVE ✓ robust" in r[3]]
    keep_filters   = [r for r in rows if "KEEP" in r[3]]
    neutral        = [r for r in rows if r[3] == "neutral"]

    if safe_removals:
        total_oos_gain = sum(-r[2] for r in safe_removals)
        print(f"\n  ✓ SAFE TO REMOVE (helps both IS and OOS):")
        for r in safe_removals:
            print(f"    - {r[0]:<20} OOS gain by removing: +${-r[2]:,.0f}")
        print(f"  → Total OOS profit gain if all removed: +${total_oos_gain:,.0f}")

    if keep_filters:
        print(f"\n  ✗ KEEP (essential or overfit-to-remove):")
        for r in keep_filters:
            print(f"    - {r[0]:<20} {r[3]}")

    if neutral:
        print(f"\n  · NEUTRAL (no significant impact):")
        for r in neutral:
            print(f"    - {r[0]}")

    print(f"\n{'='*92}\n")


if __name__ == "__main__":
    main()
