"""
tools/walk_forward_br.py
Walk-forward validation of breakout_retest filter recommendations.

Splits 2023-2026 into:
  IS  = 2023-01-01 → 2023-12-31    (in-sample,  what we'd "train" on)
  OOS = 2024-01-01 → 2026-04-01    (out-of-sample, the unseen future)

Generates the trade list for each period via the existing engine, then
evaluates 8 candidate filters on each period:

  1. Skip BTC PUMP regime
  2. Skip BTC ATR > 1.0% (high volatility)
  3. Skip BTC 16-24 UTC (NY close → Asia open)
  4. Skip BTC Sat + Thu
  5. Skip ETH 4H ADX > 30
  6. Skip ETH TREND/SHORT direction
  7. Skip ETH 16-24 UTC
  8. Skip ETH Sat + Wed + Fri

A filter is considered ROBUST if it improves both IS and OOS net dollar
profit (or improves OOS net while keeping IS at-or-near baseline).  A
filter that wins IS but degrades OOS is OVERFIT and must be rejected.

Usage:
    python tools/walk_forward_br.py
"""
import os
import sys
from datetime import datetime, timezone

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from backtest.engine import (
    load, run_breakout_retest, atr,
    O, H, L, C, V, TS,
)
from backtest.regime_classifier import classify_regime, _calc_adx


def _ms(date_str: str) -> int:
    return int(
        datetime.strptime(date_str, "%Y-%m-%d")
        .replace(tzinfo=timezone.utc)
        .timestamp() * 1000
    )


# ── Per-trade context lookups (same as reverse_engineer_br.py) ───────────────

def _classify_regime_at(bars_4h, bars_1d, ts_ms):
    if bars_4h is None or len(bars_4h) == 0: return "TREND"
    j4 = int(np.searchsorted(bars_4h[:, TS], int(ts_ms), side="right")) - 1
    if j4 < 50: return "TREND"
    win4 = bars_4h[max(0, j4 - 60): j4 + 1]
    closes_1d = []
    if bars_1d is not None and len(bars_1d) > 0:
        j1 = int(np.searchsorted(bars_1d[:, TS], int(ts_ms), side="right")) - 1
        if j1 >= 0:
            closes_1d = bars_1d[max(0, j1 - 80): j1 + 1, C].tolist()
    return classify_regime(
        win4[:, C].tolist(), win4[:, H].tolist(), win4[:, L].tolist(),
        closes_1d,
    )


def _adx_at(bars_4h, ts_ms):
    if bars_4h is None: return 0.0
    j = int(np.searchsorted(bars_4h[:, TS], int(ts_ms), side="right")) - 1
    if j < 30: return 0.0
    win = bars_4h[max(0, j - 30): j + 1]
    return _calc_adx(win[:, H].tolist(), win[:, L].tolist(), win[:, C].tolist())["adx"]


def _atr_pct_at(bars_1h, ts_ms):
    if bars_1h is None: return 0.0
    j = int(np.searchsorted(bars_1h[:, TS], int(ts_ms), side="right")) - 1
    if j < 20: return 0.0
    arr = atr(bars_1h[:j + 1])
    last_atr = arr[-1] if len(arr) else 0.0
    last_c   = bars_1h[j, C]
    return (last_atr / last_c) * 100 if last_c > 0 else 0.0


def _hour(ts_ms): return datetime.fromtimestamp(ts_ms / 1000, timezone.utc).hour
def _dow(ts_ms):  return datetime.fromtimestamp(ts_ms / 1000, timezone.utc).weekday()


# ── Filter definitions ───────────────────────────────────────────────────────

def filter_btc_no_pump(t, ctx):
    """Block BTC PUMP regime entries."""
    return ctx["regime"] == "PUMP"


def filter_btc_no_high_atr(t, ctx):
    """Block BTC entries when 1H ATR > 1.0% of price."""
    return ctx["atr_pct"] > 1.0


def filter_btc_no_late_hours(t, ctx):
    """Block BTC entries 16-24 UTC."""
    return 16 <= ctx["hour"] < 24


def filter_btc_no_bad_days(t, ctx):
    """Block BTC entries Sat + Thu."""
    return ctx["dow"] in (3, 5)  # 3=Thu, 5=Sat


def filter_eth_no_high_adx(t, ctx):
    """Block ETH entries when 4H ADX > 30."""
    return ctx["adx"] > 30


def filter_eth_no_trend_short(t, ctx):
    """Block ETH SHORTs in TREND regime."""
    return ctx["regime"] == "TREND" and t.direction == "SHORT"


def filter_eth_no_late_hours(t, ctx):
    """Block ETH entries 16-24 UTC."""
    return 16 <= ctx["hour"] < 24


def filter_eth_no_bad_days(t, ctx):
    """Block ETH entries Sat + Wed + Fri."""
    return ctx["dow"] in (2, 4, 5)  # 2=Wed, 4=Fri, 5=Sat


SYMBOL_FILTERS = {
    "BTCUSDT": [
        ("no_PUMP",          filter_btc_no_pump),
        ("no_ATR>1%",        filter_btc_no_high_atr),
        ("no_16-24UTC",      filter_btc_no_late_hours),
        ("no_Thu+Sat",       filter_btc_no_bad_days),
    ],
    "ETHUSDT": [
        ("no_ADX>30",        filter_eth_no_high_adx),
        ("no_TREND/SHORT",   filter_eth_no_trend_short),
        ("no_16-24UTC",      filter_eth_no_late_hours),
        ("no_Wed+Fri+Sat",   filter_eth_no_bad_days),
    ],
}


# ── Stat helper ──────────────────────────────────────────────────────────────

def _stats(filtered_trades, fixed_risk_usdt=50.0):
    if not filtered_trades:
        return {"n": 0, "wr": 0, "pf": 0, "net_r": 0, "net_usdt": 0}
    n  = len(filtered_trades)
    w  = sum(1 for t in filtered_trades if t.outcome == "TP")
    gw = sum(t.pnl_r for t in filtered_trades if t.pnl_r > 0)
    gl = sum(-t.pnl_r for t in filtered_trades if t.pnl_r < 0)
    pf = (gw / gl) if gl > 0 else float("inf")
    net_r = gw - gl
    return {
        "n":   n,
        "wr":  w / n * 100,
        "pf":  pf,
        "net_r":  net_r,
        "net_usdt": net_r * fixed_risk_usdt,
    }


def _pf(pf): return "inf" if pf == float("inf") else f"{pf:.2f}"


# ── Run engine + tag each trade with context ─────────────────────────────────

def _run_with_context(symbol, from_date, to_date):
    data = load(symbol)
    if data is None: return None, []
    btc_data = load("BTCUSDT") if symbol != "BTCUSDT" else data
    b5m = data[f"{symbol}:5m"]
    b1h = data.get(f"{symbol}:1h")
    b4h = data.get(f"{symbol}:4h")
    b1d = data.get(f"{symbol}:1d")
    trades = run_breakout_retest(symbol, data, btc_data, _ms(from_date), _ms(to_date))
    contexts = []
    for t in trades:
        ts = float(b5m[t.bar_idx, TS])
        contexts.append({
            "regime":  _classify_regime_at(b4h, b1d, ts),
            "adx":     _adx_at(b4h, ts),
            "atr_pct": _atr_pct_at(b1h, ts),
            "hour":    _hour(ts),
            "dow":     _dow(ts),
        })
    return trades, contexts


def _apply_filter(trades, contexts, filter_fn):
    """Return trades that PASS the filter (i.e. were not blocked)."""
    return [t for t, ctx in zip(trades, contexts) if not filter_fn(t, ctx)]


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    IS_FROM, IS_TO  = "2023-01-01", "2023-12-31"
    OOS_FROM, OOS_TO = "2024-01-01", "2026-04-01"

    print(f"\n{'='*84}")
    print(f"  WALK-FORWARD VALIDATION — breakout_retest")
    print(f"  IS  = {IS_FROM} → {IS_TO}")
    print(f"  OOS = {OOS_FROM} → {OOS_TO}")
    print(f"{'='*84}")

    for symbol, filters in SYMBOL_FILTERS.items():
        print(f"\n{'─'*84}")
        print(f"  {symbol}")
        print(f"{'─'*84}")

        is_trades,  is_ctx  = _run_with_context(symbol, IS_FROM, IS_TO)
        oos_trades, oos_ctx = _run_with_context(symbol, OOS_FROM, OOS_TO)
        if not is_trades or not oos_trades:
            print("  No trades generated"); continue

        is_base  = _stats(is_trades)
        oos_base = _stats(oos_trades)
        print(f"\n  BASELINE  IS:  n={is_base['n']:>4}  WR={is_base['wr']:>5.1f}%  "
              f"PF={_pf(is_base['pf']):>5}  net=${is_base['net_usdt']:>+9.2f}")
        print(f"  BASELINE  OOS: n={oos_base['n']:>4}  WR={oos_base['wr']:>5.1f}%  "
              f"PF={_pf(oos_base['pf']):>5}  net=${oos_base['net_usdt']:>+9.2f}")

        print(f"\n  {'filter':<22} {'IS Δn':>6} {'IS ΔPF':>7} {'IS Δ$':>10} "
              f"{'OOS Δn':>6} {'OOS ΔPF':>7} {'OOS Δ$':>10}  verdict")
        print(f"  {'-'*22} {'-'*6} {'-'*7} {'-'*10} {'-'*6} {'-'*7} {'-'*10}  -------")

        for fname, ffn in filters:
            is_filt  = _stats(_apply_filter(is_trades,  is_ctx,  ffn))
            oos_filt = _stats(_apply_filter(oos_trades, oos_ctx, ffn))

            d_is_n  = is_filt['n']  - is_base['n']
            d_is_pf = (is_filt['pf'] if is_filt['pf'] != float('inf') else 99) - \
                      (is_base['pf'] if is_base['pf'] != float('inf') else 99)
            d_is_us = is_filt['net_usdt'] - is_base['net_usdt']
            d_oos_n  = oos_filt['n']  - oos_base['n']
            d_oos_pf = (oos_filt['pf'] if oos_filt['pf'] != float('inf') else 99) - \
                       (oos_base['pf'] if oos_base['pf'] != float('inf') else 99)
            d_oos_us = oos_filt['net_usdt'] - oos_base['net_usdt']

            # Verdict logic:
            #   ROBUST = improves OOS PF AND OOS $ net (IS doesn't matter — it's training)
            #   OVERFIT = improves IS but degrades OOS
            #   NEUTRAL = small change either way
            if d_oos_us > 200 and d_oos_pf > 0:
                verdict = "ROBUST ✓"
            elif d_oos_us < -200 or d_oos_pf < -0.1:
                verdict = "REJECT ✗"
            elif d_is_us > 200 and d_oos_us < 0:
                verdict = "OVERFIT ⚠"
            else:
                verdict = "neutral"

            print(f"  {fname:<22} {d_is_n:>+6} {d_is_pf:>+7.2f} "
                  f"{d_is_us:>+10.2f} {d_oos_n:>+6} {d_oos_pf:>+7.2f} "
                  f"{d_oos_us:>+10.2f}  {verdict}")

    print(f"\n{'='*84}")
    print("  Done.  ROBUST = improvement in BOTH metrics on out-of-sample (real validation).")
    print("  OVERFIT = looked good in-sample but degraded out-of-sample (don't ship).")
    print(f"{'='*84}\n")


if __name__ == "__main__":
    main()
