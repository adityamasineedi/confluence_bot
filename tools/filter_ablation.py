"""
tools/filter_ablation.py
13-run filter ablation study for breakout_retest.

Run A:      Baseline — all optional filters disabled (only core retest logic)
Runs B1-B11: Baseline + exactly ONE filter enabled at a time
Run C:      Current production config (all filters enabled)

For each run, report WR / PF / trades / net $ / delta vs baseline.
Isolates each filter's contribution so we can see which ones actually help.
"""
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

# Baseline: all optional filters RELAXED (effectively off)
# The core strategy (range detection + breakout + retest) still runs,
# but every optional filter is turned off or set to a non-binding value.
BASELINE = {
    **_ORIG_BR,
    "require_breakout_confirm": False,   # off
    "min_retest_body_ratio":    0.0,     # off
    "vol_spike_mult":           1.0,     # any volume ok
    "exhaustion_pct":           0.10,    # 10% — effectively never fires
    "exhaustion_bars":          6,
    "max_boundary_touches":     99,      # huge — effectively never fires
    "atr_mult_max":             99.0,    # huge — effectively never fires
    "choppy_atr_mult":          99.0,    # huge — effectively never fires
    "crash_cooldown_pct":       99.0,    # huge — effectively never fires
    "min_width_pct":            0.0001,  # tiny — accept any range
    "max_width_pct":            0.50,    # huge — accept any range
    "btc_confirm_for_alts":     False,   # off
    "max_entries_per_30min":    999,     # effectively no cap
    "max_trades_per_day":       999,     # effectively no cap
    "cooldown_mins":            0,       # no per-symbol cooldown
}

# Individual filters — baseline + THIS ONE setting enabled at production value
FILTERS = [
    ("breakout_confirm",  {"require_breakout_confirm": True}),
    ("retest_body_ratio", {"min_retest_body_ratio": 0.40}),
    ("vol_spike_1.25x",   {"vol_spike_mult": 1.25}),
    ("exhaustion_4h",     {"exhaustion_pct": 0.025}),
    ("boundary_touches",  {"max_boundary_touches": 4}),
    ("atr_regime_3x",     {"atr_mult_max": 3.0}),
    ("choppy_2x",         {"choppy_atr_mult": 2.0}),
    ("crash_cooldown",    {"crash_cooldown_pct": 1.5}),
    ("range_width_gate",  {"min_width_pct": 0.001, "max_width_pct": 0.02}),
    ("btc_confirm_alts",  {"btc_confirm_for_alts": True}),
    ("anti_correlation",  {"max_entries_per_30min": 2}),
]


def set_cfg(overrides: dict) -> None:
    """Reset to BASELINE then apply single-filter overrides."""
    _CFG["breakout_retest"].clear()
    _CFG["breakout_retest"].update(BASELINE)
    _CFG["breakout_retest"].update(overrides)


def restore_production() -> None:
    _CFG["breakout_retest"].clear()
    _CFG["breakout_retest"].update(_ORIG_BR)


def run_test(label: str, coins: list[str], from_d: str, to_d: str) -> dict | None:
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
        return {"label": label, "n": 0, "wr": 0, "pf": 0, "net_r": 0, "net_usdt": 0}
    n = len(all_trades)
    wins = sum(1 for t in all_trades if t.outcome == "TP")
    losses = sum(1 for t in all_trades if t.outcome == "SL")
    gw = sum(t.pnl_r for t in all_trades if t.pnl_r > 0)
    gl = sum(-t.pnl_r for t in all_trades if t.pnl_r < 0)
    pf = gw / gl if gl > 0 else 999.0
    wr = wins / n * 100
    net_r = gw - gl
    return {
        "label":    label,
        "n":        n,
        "wins":     wins,
        "losses":   losses,
        "wr":       round(wr, 1),
        "pf":       round(pf, 2),
        "net_r":    round(net_r, 1),
        "net_usdt": round(net_r * 50, 0),
    }


def _print_row(res: dict, baseline_usdt: float = None, marker_thresh: int = 200) -> None:
    delta_str = ""
    marker = " "
    if baseline_usdt is not None:
        delta = res["net_usdt"] - baseline_usdt
        sign = "+" if delta > 0 else ""
        delta_str = f"  {sign}{delta:+7.0f}$"
        if delta > marker_thresh:
            marker = "✓"
        elif delta < -marker_thresh:
            marker = "✗"
        else:
            marker = "·"
    print(f"  {res['label']:<24} {res['n']:>5} {res['wr']:>5.1f}% "
          f"{res['pf']:>6.2f} {res['net_r']:>+9.1f} {res['net_usdt']:>+10.0f}"
          f"{delta_str} {marker}")


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default="BTCUSDT,ETHUSDT",
                    help="comma-separated (default: BTC,ETH for speed)")
    ap.add_argument("--from-date", default="2023-01-01")
    ap.add_argument("--to-date",   default="2026-04-01")
    args = ap.parse_args()

    coins = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]

    print(f"\n{'='*84}")
    print(f"  FILTER ABLATION STUDY — {', '.join(coins)}")
    print(f"  Period: {args.from_date} → {args.to_date}")
    print(f"{'='*84}")
    print(f"  {'run':<24} {'n':>5} {'WR%':>6} {'PF':>6} {'net R':>9} {'net $':>10}"
          f"  {'delta vs A':>11}")
    print(f"  {'-'*24} {'-'*5} {'-'*6} {'-'*6} {'-'*9} {'-'*10}"
          f"  {'-'*11}")

    t0 = _time.time()

    # Run A — Baseline (all off)
    set_cfg({})
    baseline = run_test("A. BASELINE (all OFF)", coins, args.from_date, args.to_date)
    _print_row(baseline)

    # Runs B1-B11 — Baseline + one filter
    filter_results = []
    for i, (name, overrides) in enumerate(FILTERS, 1):
        set_cfg(overrides)
        res = run_test(f"B{i:<2}. +{name}", coins, args.from_date, args.to_date)
        if res:
            _print_row(res, baseline["net_usdt"])
            res["filter_name"] = name
            res["overrides"]   = overrides
            filter_results.append(res)

    # Run C — Production config (all filters ON)
    restore_production()
    current = run_test("C. ALL ON (prod)", coins, args.from_date, args.to_date)
    print(f"  {'-'*24} {'-'*5} {'-'*6} {'-'*6} {'-'*9} {'-'*10}"
          f"  {'-'*11}")
    _print_row(current, baseline["net_usdt"])

    elapsed = _time.time() - t0
    print(f"\n  Completed {2 + len(filter_results)} runs in {elapsed:.0f}s")

    # Rank individual filters by $ impact
    print(f"\n{'='*84}")
    print(f"  FILTER RANKING (impact vs baseline) — baseline = ${baseline['net_usdt']:.0f}")
    print(f"{'-'*84}")
    ranked = [(r, r["net_usdt"] - baseline["net_usdt"]) for r in filter_results]
    ranked.sort(key=lambda x: x[1], reverse=True)

    print(f"  {'filter':<24} {'delta $':>10}  {'new PF':>7}  {'new WR':>7}  {'verdict'}")
    print(f"  {'-'*24} {'-'*10}  {'-'*7}  {'-'*7}  -------")
    for r, delta in ranked:
        if delta > 500:
            verdict = "STRONG KEEP"
        elif delta > 100:
            verdict = "KEEP"
        elif delta > -100:
            verdict = "marginal"
        else:
            verdict = "REMOVE"
        sign = "+" if delta > 0 else ""
        print(f"  {r['filter_name']:<24} {sign}{delta:+9.0f}  {r['pf']:>7.2f}  "
              f"{r['wr']:>6.1f}%  {verdict}")

    print(f"\n{'='*84}")
    print(f"  Baseline:   ${baseline['net_usdt']:.0f}  (PF {baseline['pf']}, "
          f"WR {baseline['wr']}%, n={baseline['n']})")
    print(f"  Production: ${current['net_usdt']:.0f}  (PF {current['pf']}, "
          f"WR {current['wr']}%, n={current['n']})")
    diff = current["net_usdt"] - baseline["net_usdt"]
    print(f"  Production vs baseline: {'+' if diff > 0 else ''}{diff:+.0f}$  "
          f"({current['n'] - baseline['n']:+d} trades)")
    print(f"{'='*84}\n")


if __name__ == "__main__":
    main()
