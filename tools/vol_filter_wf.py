"""
tools/vol_filter_wf.py
Walk-forward test of the volume-ratio filter discovered in winners-vs-losers analysis.

Tests vol_ratio thresholds 1.25 (current), 1.4, 1.5, 1.6, 1.7, 1.8 across:
  IS  = 2023-01-01 → 2023-12-31
  OOS = 2024-01-01 → 2026-04-01
"""
import os, sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from backtest.engine import load, run_breakout_retest, V, TS
import numpy as np


def _ms(d): return int(datetime.strptime(d, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000)


def vol_ratio_at(b5m, idx):
    if idx < 20: return 0.0
    avg = b5m[idx - 20: idx, V].mean()
    return (b5m[idx, V] / avg) if avg > 0 else 0.0


def test_threshold(symbol, from_d, to_d, vol_thr):
    data = load(symbol)
    if data is None: return None
    btc_data = load("BTCUSDT") if symbol != "BTCUSDT" else data
    b5m = data[f"{symbol}:5m"]
    trades = run_breakout_retest(symbol, data, btc_data, _ms(from_d), _ms(to_d))
    kept = [t for t in trades if vol_ratio_at(b5m, t.bar_idx) >= vol_thr]
    if not kept:
        return {"n": 0}
    n = len(kept)
    w = sum(1 for t in kept if t.outcome == "TP")
    gw = sum(t.pnl_r for t in kept if t.pnl_r > 0)
    gl = sum(-t.pnl_r for t in kept if t.pnl_r < 0)
    pf = (gw / gl) if gl > 0 else float("inf")
    net_r = gw - gl
    return {"n": n, "wr": w / n * 100, "pf": pf, "net_r": net_r, "net_usdt": net_r * 50}


def main():
    IS_FROM, IS_TO = "2023-01-01", "2023-12-31"
    OOS_FROM, OOS_TO = "2024-01-01", "2026-04-01"
    thresholds = [1.25, 1.4, 1.5, 1.6, 1.7, 1.8]

    print(f"\n{'='*84}")
    print(f"  VOLUME FILTER WALK-FORWARD")
    print(f"  IS  = {IS_FROM} -> {IS_TO}")
    print(f"  OOS = {OOS_FROM} -> {OOS_TO}")
    print(f"{'='*84}")

    for sym in ["BTCUSDT", "ETHUSDT"]:
        print(f"\n  {sym}")
        print(f"    {'thr':>5} | {'IS n':>5} {'IS WR':>6} {'IS PF':>6} {'IS $':>9} | "
              f"{'OOS n':>5} {'OOS WR':>7} {'OOS PF':>7} {'OOS $':>9}  verdict")
        print(f"    {'-'*5} | {'-'*5} {'-'*6} {'-'*6} {'-'*9} | "
              f"{'-'*5} {'-'*7} {'-'*7} {'-'*9}  -------")
        baseline_oos = None
        for thr in thresholds:
            is_st  = test_threshold(sym, IS_FROM,  IS_TO,  thr)
            oos_st = test_threshold(sym, OOS_FROM, OOS_TO, thr)
            if not is_st or not oos_st or is_st.get("n", 0) == 0:
                continue
            if abs(thr - 1.25) < 1e-6:
                baseline_oos = oos_st
            verdict = ""
            if baseline_oos is not None:
                d_us = oos_st["net_usdt"] - baseline_oos["net_usdt"]
                d_pf = oos_st["pf"] - baseline_oos["pf"]
                if d_us > 200 and d_pf > 0.05:
                    verdict = f"  WIN  {d_us:+.0f}$  PF{d_pf:+.2f}"
                elif d_us < -200 or d_pf < -0.05:
                    verdict = f"  LOSE {d_us:+.0f}$  PF{d_pf:+.2f}"
                else:
                    verdict = f"  flat {d_us:+.0f}$"
            pf_str = f"{is_st['pf']:.2f}" if is_st['pf'] != float('inf') else " inf"
            opf_str = f"{oos_st['pf']:.2f}" if oos_st['pf'] != float('inf') else " inf"
            print(f"    {thr:>5.2f} | {is_st['n']:>5} {is_st['wr']:>5.1f}% "
                  f"{pf_str:>6} {is_st['net_usdt']:>+9.0f} | "
                  f"{oos_st['n']:>5} {oos_st['wr']:>6.1f}% "
                  f"{opf_str:>7} {oos_st['net_usdt']:>+9.0f}{verdict}")

    print(f"\n{'='*84}")


if __name__ == "__main__":
    main()
