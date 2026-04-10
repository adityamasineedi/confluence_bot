"""
tools/full_audit_phase_a.py
Phase A — full statistical audit of breakout_retest using backtest data only.

Sections:
  1. Equity curve metrics: max drawdown, max losing streak, expectancy
  2. Monte Carlo equity simulation: shuffle the trade list 10,000 times
     and report the worst-case drawdown bound
  3. Winners-vs-losers feature comparison: ADX, ATR%, vol ratio, hour, dow,
     hold time, MAE — plus Welch's t-test for significance
  4. Per-coin segmented stats (8 coins blended)
  5. Per-regime / per-direction expectancy

Output: a single text report printed to stdout.
"""
import argparse
import math
import os
import random
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timezone

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from backtest.engine import (
    load, run_breakout_retest, atr,
    O, H, L, C, V, TS,
)
from backtest.regime_classifier import classify_regime, _calc_adx


# ── Helpers ──────────────────────────────────────────────────────────────────

def _ms(date_str: str) -> int:
    return int(
        datetime.strptime(date_str, "%Y-%m-%d")
        .replace(tzinfo=timezone.utc)
        .timestamp() * 1000
    )


def _classify_regime_at(b4h, b1d, ts_ms):
    if b4h is None or len(b4h) == 0: return "TREND"
    j4 = int(np.searchsorted(b4h[:, TS], int(ts_ms), side="right")) - 1
    if j4 < 50: return "TREND"
    win4 = b4h[max(0, j4 - 60): j4 + 1]
    closes_1d = []
    if b1d is not None and len(b1d) > 0:
        j1 = int(np.searchsorted(b1d[:, TS], int(ts_ms), side="right")) - 1
        if j1 >= 0:
            closes_1d = b1d[max(0, j1 - 80): j1 + 1, C].tolist()
    return classify_regime(
        win4[:, C].tolist(), win4[:, H].tolist(), win4[:, L].tolist(),
        closes_1d,
    )


def _adx_at(b4h, ts_ms):
    if b4h is None: return 0.0
    j = int(np.searchsorted(b4h[:, TS], int(ts_ms), side="right")) - 1
    if j < 30: return 0.0
    win = b4h[max(0, j - 30): j + 1]
    return _calc_adx(win[:, H].tolist(), win[:, L].tolist(), win[:, C].tolist())["adx"]


def _atr_pct_at(b1h, ts_ms):
    if b1h is None: return 0.0
    j = int(np.searchsorted(b1h[:, TS], int(ts_ms), side="right")) - 1
    if j < 20: return 0.0
    arr = atr(b1h[:j + 1])
    last_atr = arr[-1] if len(arr) else 0.0
    last_c   = b1h[j, C]
    return (last_atr / last_c) * 100 if last_c > 0 else 0.0


def _vol_ratio_at(b5m, idx):
    """Breakout vol vs 20-bar avg vol."""
    if idx < 20:
        return 0.0
    recent = b5m[idx - 20: idx, V]
    avg = recent.mean()
    bar_v = b5m[idx, V]
    return (bar_v / avg) if avg > 0 else 0.0


def _range_width_pct(b5m, idx, lookback=8):
    """Range width as % of mid price for the 8 bars before the breakout bar."""
    if idx < lookback + 1:
        return 0.0
    win = b5m[idx - lookback - 1: idx - 1]
    rh = float(win[:, H].max())
    rl = float(win[:, L].min())
    mid = (rh + rl) / 2
    return ((rh - rl) / mid * 100) if mid > 0 else 0.0


def _btc_5m_change(btc_5m, ts_ms, lookback_bars=12):
    """BTC 1H momentum (12 × 5M bars) leading up to entry."""
    if btc_5m is None: return 0.0
    j = int(np.searchsorted(btc_5m[:, TS], int(ts_ms), side="right")) - 1
    if j < lookback_bars: return 0.0
    start = btc_5m[j - lookback_bars, C]
    now   = btc_5m[j, C]
    return ((now - start) / start * 100) if start > 0 else 0.0


def _hour(ts_ms): return datetime.fromtimestamp(ts_ms / 1000, timezone.utc).hour
def _dow(ts_ms):  return datetime.fromtimestamp(ts_ms / 1000, timezone.utc).weekday()


def _walk_mfe_mae(b5m, eb, exit_idx, entry, direction, sl_dist):
    if exit_idx <= eb or sl_dist <= 0:
        return 0.0, 0.0, 0
    fut = b5m[eb + 1: exit_idx + 1]
    if len(fut) == 0:
        return 0.0, 0.0, 0
    mh = float(fut[:, H].max())
    ml = float(fut[:, L].min())
    if direction == "LONG":
        mfe = (mh - entry) / sl_dist
        mae = (entry - ml) / sl_dist
    else:
        mfe = (entry - ml) / sl_dist
        mae = (mh - entry) / sl_dist
    return max(0.0, mfe), max(0.0, mae), len(fut)


# ── Statistical helpers ──────────────────────────────────────────────────────

def welch_ttest(a: list[float], b: list[float]) -> tuple[float, float]:
    """Returns (t_stat, two-sided p-value approximation).

    Uses normal approximation for p-value (sample sizes are large enough that
    the t-distribution converges).  No scipy dependency.
    """
    if len(a) < 2 or len(b) < 2:
        return 0.0, 1.0
    ma, mb = statistics.mean(a), statistics.mean(b)
    va, vb = statistics.variance(a), statistics.variance(b)
    na, nb = len(a), len(b)
    se = math.sqrt(va / na + vb / nb)
    if se == 0:
        return 0.0, 1.0
    t = (ma - mb) / se
    # Two-sided p-value via normal approximation (df is large)
    z = abs(t)
    # Abramowitz & Stegun 7.1.26 erfc approximation
    p = math.erfc(z / math.sqrt(2))
    return t, p


def cohen_d(a: list[float], b: list[float]) -> float:
    """Effect size — Cohen's d.  |d| < 0.2 small, 0.2-0.5 medium, > 0.8 large."""
    if len(a) < 2 or len(b) < 2:
        return 0.0
    ma, mb = statistics.mean(a), statistics.mean(b)
    va, vb = statistics.variance(a), statistics.variance(b)
    pooled = math.sqrt(((len(a) - 1) * va + (len(b) - 1) * vb) / (len(a) + len(b) - 2))
    if pooled == 0:
        return 0.0
    return (ma - mb) / pooled


# ── Equity curve analysis ────────────────────────────────────────────────────

def equity_curve(trades_pnl_r: list[float], starting: float = 5000.0,
                 risk_usdt: float = 50.0) -> tuple[list[float], float, float, int]:
    """Walk the trade list, compute equity curve in dollars.

    Returns (curve, max_dd_pct, max_dd_usdt, max_losing_streak).
    """
    eq = [starting]
    peak = starting
    max_dd_pct = 0.0
    max_dd_usdt = 0.0
    streak = 0
    max_streak = 0
    for r in trades_pnl_r:
        eq.append(eq[-1] + r * risk_usdt)
        if eq[-1] > peak:
            peak = eq[-1]
        dd_pct = (peak - eq[-1]) / peak * 100 if peak > 0 else 0
        dd_usd = peak - eq[-1]
        if dd_pct > max_dd_pct:
            max_dd_pct = dd_pct
        if dd_usd > max_dd_usdt:
            max_dd_usdt = dd_usd
        if r < 0:
            streak += 1
            if streak > max_streak:
                max_streak = streak
        else:
            streak = 0
    return eq, max_dd_pct, max_dd_usdt, max_streak


def monte_carlo_drawdown(trades_pnl_r: list[float], iterations: int = 10000,
                         starting: float = 5000.0, risk_usdt: float = 50.0,
                         seed: int = 42) -> dict:
    """Shuffle the trade list `iterations` times and record drawdown distribution.

    The trade-level PnL is fixed (the strategy is what it is) but the *order*
    of wins/losses is randomized.  Reports the percentile distribution of
    max drawdown — answers "what's the worst luck I could have had?"
    """
    rnd = random.Random(seed)
    dd_pcts = []
    streaks = []
    for _ in range(iterations):
        shuffled = trades_pnl_r[:]
        rnd.shuffle(shuffled)
        _, dd_pct, _, streak = equity_curve(shuffled, starting, risk_usdt)
        dd_pcts.append(dd_pct)
        streaks.append(streak)
    dd_pcts.sort()
    streaks.sort()
    n = len(dd_pcts)
    return {
        "dd_p50":      dd_pcts[n // 2],
        "dd_p90":      dd_pcts[int(n * 0.90)],
        "dd_p95":      dd_pcts[int(n * 0.95)],
        "dd_p99":      dd_pcts[int(n * 0.99)],
        "dd_max":      dd_pcts[-1],
        "streak_p50":  streaks[n // 2],
        "streak_p90":  streaks[int(n * 0.90)],
        "streak_p99":  streaks[int(n * 0.99)],
        "streak_max":  streaks[-1],
    }


# ── Trade context builder ────────────────────────────────────────────────────

def build_trade_features(symbol: str, from_date: str, to_date: str) -> tuple[list, list[dict]]:
    """Run the BR engine and tag every trade with feature context."""
    data = load(symbol)
    if data is None:
        return [], []
    btc_data = load("BTCUSDT") if symbol != "BTCUSDT" else data
    b5m = data[f"{symbol}:5m"]
    b1h = data.get(f"{symbol}:1h")
    b4h = data.get(f"{symbol}:4h")
    b1d = data.get(f"{symbol}:1d")
    btc_5m = btc_data.get("BTCUSDT:5m") if btc_data else None

    trades = run_breakout_retest(symbol, data, btc_data, _ms(from_date), _ms(to_date))
    contexts = []
    for t in trades:
        eb = t.bar_idx
        ts = float(b5m[eb, TS])
        sl_dist = abs(t.entry - t.stop)

        # Walk forward to find exit bar (for MFE/MAE)
        exit_idx = eb
        if t.outcome in ("TP", "SL"):
            for k in range(1, min(49, len(b5m) - eb)):
                bar = b5m[eb + k]
                if t.direction == "LONG":
                    if bar[L] <= t.stop or bar[H] >= t.tp:
                        exit_idx = eb + k; break
                else:
                    if bar[H] >= t.stop or bar[L] <= t.tp:
                        exit_idx = eb + k; break
            if exit_idx == eb:
                exit_idx = min(eb + 48, len(b5m) - 1)
        else:
            exit_idx = min(eb + 48, len(b5m) - 1)

        mfe, mae, hold = _walk_mfe_mae(b5m, eb, exit_idx, t.entry, t.direction, sl_dist)

        contexts.append({
            "regime":      _classify_regime_at(b4h, b1d, ts),
            "adx":         _adx_at(b4h, ts),
            "atr_pct":     _atr_pct_at(b1h, ts),
            "vol_ratio":   _vol_ratio_at(b5m, eb),
            "range_pct":   _range_width_pct(b5m, eb),
            "btc_mom_1h":  _btc_5m_change(btc_5m, ts, 12),
            "hour":        _hour(ts),
            "dow":         _dow(ts),
            "mfe":         mfe,
            "mae":         mae,
            "hold":        hold,
        })
    return trades, contexts


# ── Reporting ────────────────────────────────────────────────────────────────

def _section(title: str):
    print(f"\n{'─'*84}")
    print(f"  {title}")
    print(f"{'─'*84}")


def report_part2_stats(symbol: str, trades, mc: dict, eq_max_dd: float,
                       eq_max_streak: int):
    if not trades:
        return
    n = len(trades)
    wins   = [t for t in trades if t.outcome == "TP"]
    losses = [t for t in trades if t.outcome == "SL"]
    tos    = [t for t in trades if t.outcome == "TIMEOUT"]
    wr = len(wins) / n
    avg_win_r  = statistics.mean(t.pnl_r for t in wins)   if wins   else 0
    avg_loss_r = statistics.mean(-t.pnl_r for t in losses) if losses else 0
    expectancy_r = wr * avg_win_r - (1 - wr) * avg_loss_r

    gross_w = sum(t.pnl_r for t in trades if t.pnl_r > 0)
    gross_l = sum(-t.pnl_r for t in trades if t.pnl_r < 0)
    pf = gross_w / gross_l if gross_l > 0 else float("inf")

    print(f"\n  {symbol}")
    print(f"    n={n}  WR={wr*100:.1f}%  PF={pf:.2f}")
    print(f"    avgWin={avg_win_r:+.2f}R  avgLoss=-{avg_loss_r:.2f}R  "
          f"E={expectancy_r:+.3f}R per trade")
    print(f"    Hist max DD: {eq_max_dd:.1f}%   Hist max losing streak: {eq_max_streak}")
    print(f"    MC max DD p50/p90/p95/p99: "
          f"{mc['dd_p50']:.1f}% / {mc['dd_p90']:.1f}% / "
          f"{mc['dd_p95']:.1f}% / {mc['dd_p99']:.1f}%")
    print(f"    MC max streak p50/p90/p99: "
          f"{mc['streak_p50']} / {mc['streak_p90']} / {mc['streak_p99']}")


def report_part3_winners_vs_losers(symbol: str, trades, contexts):
    """For each numeric feature, compare winners vs losers and report mean,
    Cohen's d, and significance."""
    if not trades or not contexts:
        return
    win_ctx = [c for t, c in zip(trades, contexts) if t.outcome == "TP"]
    los_ctx = [c for t, c in zip(trades, contexts) if t.outcome == "SL"]
    if not win_ctx or not los_ctx:
        return

    print(f"\n  {symbol}  (winners n={len(win_ctx)}  losers n={len(los_ctx)})")
    print(f"    {'feature':<14} {'win mean':>10} {'los mean':>10} "
          f"{'diff':>9} {'Cohen d':>9} {'p-value':>10}  signif")
    print(f"    {'-'*14} {'-'*10} {'-'*10} {'-'*9} {'-'*9} {'-'*10}  ------")

    features = ["adx", "atr_pct", "vol_ratio", "range_pct", "btc_mom_1h",
                "hour", "hold", "mae", "mfe"]
    for f in features:
        w_vals = [c[f] for c in win_ctx if c[f] is not None]
        l_vals = [c[f] for c in los_ctx if c[f] is not None]
        if not w_vals or not l_vals:
            continue
        mw = statistics.mean(w_vals)
        ml = statistics.mean(l_vals)
        d  = cohen_d(w_vals, l_vals)
        _, p = welch_ttest(w_vals, l_vals)
        signif = ""
        if p < 0.001: signif = "***"
        elif p < 0.01: signif = "**"
        elif p < 0.05: signif = "*"
        print(f"    {f:<14} {mw:>10.3f} {ml:>10.3f} {mw-ml:>+9.3f} "
              f"{d:>+9.3f} {p:>10.4f}  {signif}")


def report_per_coin(coins: list[str], from_date: str, to_date: str):
    rows = []
    for sym in coins:
        trades, _ = build_trade_features(sym, from_date, to_date)
        if not trades:
            continue
        n = len(trades)
        wins = sum(1 for t in trades if t.outcome == "TP")
        gross_w = sum(t.pnl_r for t in trades if t.pnl_r > 0)
        gross_l = sum(-t.pnl_r for t in trades if t.pnl_r < 0)
        pf = gross_w / gross_l if gross_l > 0 else float("inf")
        wr = wins / n * 100
        net_r = gross_w - gross_l
        # Equity curve drawdown
        pnl_seq = [t.pnl_r for t in trades]
        _, dd_pct, _, max_streak = equity_curve(pnl_seq)
        rows.append((sym, n, wr, pf, net_r, dd_pct, max_streak))
    print(f"\n  {'symbol':<10} {'n':>5} {'WR%':>6} {'PF':>6} {'netR':>9} "
          f"{'maxDD%':>7} {'maxLoseStrk':>11}")
    print(f"  {'-'*10} {'-'*5} {'-'*6} {'-'*6} {'-'*9} {'-'*7} {'-'*11}")
    for r in rows:
        print(f"  {r[0]:<10} {r[1]:>5} {r[2]:>5.1f}% {r[3]:>6.2f} "
              f"{r[4]:>+9.1f} {r[5]:>6.1f}% {r[6]:>11}")


def report_per_regime(symbol: str, trades, contexts):
    if not trades: return
    by_regime = defaultdict(list)
    for t, c in zip(trades, contexts):
        by_regime[c["regime"]].append(t)
    print(f"\n  {symbol}")
    print(f"    {'regime':<10} {'n':>5} {'WR%':>6} {'PF':>6} {'netR':>9}  E_per_trade")
    print(f"    {'-'*10} {'-'*5} {'-'*6} {'-'*6} {'-'*9}  -----------")
    for reg, tr in sorted(by_regime.items(), key=lambda x: -len(x[1])):
        if len(tr) < 10: continue
        n = len(tr)
        wins = sum(1 for t in tr if t.outcome == "TP")
        gross_w = sum(t.pnl_r for t in tr if t.pnl_r > 0)
        gross_l = sum(-t.pnl_r for t in tr if t.pnl_r < 0)
        pf = gross_w / gross_l if gross_l > 0 else float("inf")
        wr = wins / n * 100
        net_r = gross_w - gross_l
        e = net_r / n
        print(f"    {reg:<10} {n:>5} {wr:>5.1f}% {pf:>6.2f} {net_r:>+9.1f}  {e:+.4f}R")


# ── Main ─────────────────────────────────────────────────────────────────────

def run_audit(from_date: str = "2023-01-01",
              to_date:   str = "2026-04-01",
              mc_iters:  int = 10000,
              coins:     list[str] | None = None) -> dict:
    """Run the full Phase A audit and return a structured result dict.

    This is the function the dashboard /api/audit/run endpoint calls.
    Returns a dict with keys: per_coin, btc_stats, eth_stats, btc_features,
    eth_features, btc_regimes, eth_regimes, generated_at.
    """
    if coins is None:
        coins = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT",
                 "XRPUSDT", "LINKUSDT", "DOGEUSDT", "SUIUSDT"]

    # Per-coin summary
    per_coin = []
    for sym in coins:
        trades, _ = build_trade_features(sym, from_date, to_date)
        if not trades:
            continue
        n = len(trades)
        wins = sum(1 for t in trades if t.outcome == "TP")
        losses = sum(1 for t in trades if t.outcome == "SL")
        gw = sum(t.pnl_r for t in trades if t.pnl_r > 0)
        gl = sum(-t.pnl_r for t in trades if t.pnl_r < 0)
        pf = gw / gl if gl > 0 else 999.0
        wr = wins / n * 100
        net_r = gw - gl
        pnl_seq = [t.pnl_r for t in trades]
        _, dd_pct, _, max_streak = equity_curve(pnl_seq)
        per_coin.append({
            "symbol":     sym,
            "n":          n,
            "wr":         round(wr, 1),
            "pf":         round(pf, 2),
            "net_r":      round(net_r, 1),
            "net_usdt":   round(net_r * 50, 0),
            "max_dd_pct": round(dd_pct, 1),
            "max_streak": max_streak,
        })

    # BTC + ETH deep dive
    btc_trades, btc_ctx = build_trade_features("BTCUSDT", from_date, to_date)
    eth_trades, eth_ctx = build_trade_features("ETHUSDT", from_date, to_date)

    def _stats_with_mc(trades, ctx):
        if not trades:
            return None
        pnl = [t.pnl_r for t in trades]
        _, dd_pct, _, max_streak = equity_curve(pnl)
        mc = monte_carlo_drawdown(pnl, iterations=mc_iters)
        n = len(trades)
        wins = [t for t in trades if t.outcome == "TP"]
        losses = [t for t in trades if t.outcome == "SL"]
        wr = len(wins) / n
        avg_win_r = statistics.mean(t.pnl_r for t in wins) if wins else 0
        avg_loss_r = statistics.mean(-t.pnl_r for t in losses) if losses else 0
        E = wr * avg_win_r - (1 - wr) * avg_loss_r
        gw = sum(t.pnl_r for t in trades if t.pnl_r > 0)
        gl = sum(-t.pnl_r for t in trades if t.pnl_r < 0)
        pf = gw / gl if gl > 0 else 999.0
        return {
            "n":            n,
            "wr":           round(wr * 100, 1),
            "pf":           round(pf, 2),
            "avg_win_r":    round(avg_win_r, 2),
            "avg_loss_r":   round(avg_loss_r, 2),
            "expectancy_r": round(E, 3),
            "expectancy_usdt": round(E * 50, 2),
            "hist_max_dd_pct": round(dd_pct, 1),
            "hist_max_streak": max_streak,
            "mc_dd_p50":    round(mc["dd_p50"], 1),
            "mc_dd_p90":    round(mc["dd_p90"], 1),
            "mc_dd_p95":    round(mc["dd_p95"], 1),
            "mc_dd_p99":    round(mc["dd_p99"], 1),
            "mc_dd_max":    round(mc["dd_max"], 1),
            "mc_streak_p50": mc["streak_p50"],
            "mc_streak_p90": mc["streak_p90"],
            "mc_streak_p99": mc["streak_p99"],
            "mc_streak_max": mc["streak_max"],
        }

    def _features(trades, contexts):
        if not trades or not contexts:
            return None
        win_ctx = [c for t, c in zip(trades, contexts) if t.outcome == "TP"]
        los_ctx = [c for t, c in zip(trades, contexts) if t.outcome == "SL"]
        if not win_ctx or not los_ctx:
            return None
        out = {"n_winners": len(win_ctx), "n_losers": len(los_ctx), "rows": []}
        feats = ["adx", "atr_pct", "vol_ratio", "range_pct", "btc_mom_1h",
                 "hour", "hold", "mae", "mfe"]
        for f in feats:
            wv = [c[f] for c in win_ctx if c[f] is not None]
            lv = [c[f] for c in los_ctx if c[f] is not None]
            if not wv or not lv:
                continue
            mw = statistics.mean(wv)
            ml = statistics.mean(lv)
            d = cohen_d(wv, lv)
            _, p = welch_ttest(wv, lv)
            out["rows"].append({
                "feature":  f,
                "win_mean": round(mw, 3),
                "los_mean": round(ml, 3),
                "diff":     round(mw - ml, 3),
                "cohen_d":  round(d, 3),
                "p_value":  round(p, 4),
                "signif":   "***" if p < 0.001 else "**" if p < 0.01 else "*" if p < 0.05 else "",
            })
        return out

    def _regimes(trades, contexts):
        if not trades or not contexts:
            return []
        from collections import defaultdict
        by = defaultdict(list)
        for t, c in zip(trades, contexts):
            by[c["regime"]].append(t)
        out = []
        for reg, tr in sorted(by.items(), key=lambda x: -len(x[1])):
            if len(tr) < 10:
                continue
            n = len(tr)
            wins = sum(1 for t in tr if t.outcome == "TP")
            gw = sum(t.pnl_r for t in tr if t.pnl_r > 0)
            gl = sum(-t.pnl_r for t in tr if t.pnl_r < 0)
            pf = gw / gl if gl > 0 else 999.0
            net_r = gw - gl
            out.append({
                "regime":     reg,
                "n":          n,
                "wr":         round(wins / n * 100, 1),
                "pf":         round(pf, 2),
                "net_r":      round(net_r, 1),
                "expectancy": round(net_r / n, 4),
            })
        return out

    return {
        "from_date":      from_date,
        "to_date":        to_date,
        "mc_iters":       mc_iters,
        "generated_at":   datetime.now(timezone.utc).isoformat(),
        "per_coin":       per_coin,
        "btc_stats":      _stats_with_mc(btc_trades, btc_ctx),
        "eth_stats":      _stats_with_mc(eth_trades, eth_ctx),
        "btc_features":   _features(btc_trades, btc_ctx),
        "eth_features":   _features(eth_trades, eth_ctx),
        "btc_regimes":    _regimes(btc_trades, btc_ctx),
        "eth_regimes":    _regimes(eth_trades, eth_ctx),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--from-date", default="2023-01-01")
    ap.add_argument("--to-date",   default="2026-04-01")
    ap.add_argument("--mc-iters",  type=int, default=10000)
    args = ap.parse_args()

    coins = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT",
             "XRPUSDT", "LINKUSDT", "DOGEUSDT", "SUIUSDT"]

    print(f"\n{'='*84}")
    print(f"  PHASE A AUDIT — breakout_retest")
    print(f"  Period: {args.from_date} → {args.to_date}")
    print(f"  Coins:  {', '.join(coins)}")
    print(f"  MC iterations: {args.mc_iters}")
    print(f"{'='*84}")

    # Build trade features for BTC + ETH (deep analysis)
    print("\n[Building features for BTC + ETH ...]")
    btc_trades, btc_ctx = build_trade_features("BTCUSDT", args.from_date, args.to_date)
    eth_trades, eth_ctx = build_trade_features("ETHUSDT", args.from_date, args.to_date)

    # ── PART 2: Equity curve metrics + Monte Carlo ───────────────────────
    _section("PART 2 — STATISTICAL HEALTH (BTC + ETH deep dive)")

    for sym, tr in [("BTCUSDT", btc_trades), ("ETHUSDT", eth_trades)]:
        if not tr: continue
        pnl = [t.pnl_r for t in tr]
        _, dd_pct, dd_usd, max_streak = equity_curve(pnl)
        mc = monte_carlo_drawdown(pnl, iterations=args.mc_iters)
        report_part2_stats(sym, tr, mc, dd_pct, max_streak)

    # ── PART 3: Winners vs losers ────────────────────────────────────────
    _section("PART 3 — REVERSE ENGINEERING: WINNERS vs LOSERS")
    print("  *** Statistical comparison.  Cohen's d > 0.2 = small effect, > 0.5 = medium, > 0.8 = large")
    print("  *** p-value: *** < 0.001  ** < 0.01  * < 0.05")
    report_part3_winners_vs_losers("BTCUSDT", btc_trades, btc_ctx)
    report_part3_winners_vs_losers("ETHUSDT", eth_trades, eth_ctx)

    # ── PART 4: Strategy validation per regime ───────────────────────────
    _section("PART 4 — STRATEGY VALIDATION: PER REGIME EXPECTANCY")
    report_per_regime("BTCUSDT", btc_trades, btc_ctx)
    report_per_regime("ETHUSDT", eth_trades, eth_ctx)

    # ── Per-coin summary ─────────────────────────────────────────────────
    _section("PER-COIN SUMMARY (all 8 coins)")
    report_per_coin(coins, args.from_date, args.to_date)

    print(f"\n{'='*84}")
    print("  AUDIT COMPLETE")
    print(f"{'='*84}\n")


if __name__ == "__main__":
    main()
