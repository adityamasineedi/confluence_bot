"""Micro-range flip parameter tuner — grid search over key config knobs.

Runs the backtest engine with 108 parameter combinations and prints a ranked
table so we can pick the best config before updating config.yaml.

Usage:
    python -m backtest.microrange_tune
"""
import itertools
import logging
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(level=logging.WARNING)   # suppress engine noise during sweep

_CUTOFF_DAYS = 180


def _trim_ohlcv(ohlcv: dict, cutoff_ms: int, warmup_bars: int = 32) -> dict:
    trimmed = {}
    for key, bars in ohlcv.items():
        idx   = next((i for i, b in enumerate(bars) if b["ts"] >= cutoff_ms), len(bars))
        start = max(0, idx - warmup_bars)
        trimmed[key] = bars[start:]
    return trimmed


def _run_config(ohlcv: dict, symbols: list[str], cfg_override: dict) -> dict:
    """Run a single backtest with overridden config params.  Returns summary dict."""
    import backtest.microrange_engine as eng

    # Patch module-level constants for this run
    eng._WINDOW_BARS    = int(cfg_override.get("window_bars",      10))
    eng._RANGE_MAX_PCT  = float(cfg_override.get("range_max_pct",   0.010))
    eng._ENTRY_ZONE_PCT = float(cfg_override.get("entry_zone_pct",  0.002))
    eng._STOP_PCT       = float(cfg_override.get("stop_pct",        0.003))
    eng._TP_RATIO       = float(cfg_override.get("tp_ratio",        0.75))
    eng._MAX_VOL_RATIO  = float(cfg_override.get("max_vol_ratio",   1.3))
    eng._RSI_LONG_MAX   = float(cfg_override.get("rsi_long_max",    40.0))
    eng._RSI_SHORT_MIN  = float(cfg_override.get("rsi_short_min",   60.0))
    eng._COOLDOWN_BARS  = int(cfg_override.get("cooldown_mins",     20) // 5)
    eng._MAX_HOLD       = int(cfg_override.get("max_hold_bars",      6))
    eng._USE_RSI_FILTER = bool(cfg_override.get("use_rsi_filter",  True))
    eng._WARMUP_BARS    = eng._WINDOW_BARS + 22

    trades = eng.run(symbols=symbols, ohlcv=ohlcv,
                     starting_capital=100.0, risk_pct=0.01)
    if not trades:
        return {"trades": 0, "wr": 0.0, "pnl": 0.0, "final": 100.0, "pf": 0.0, "dd": 0.0}

    wins   = sum(1 for t in trades if t["outcome"] == "WIN")
    losses = sum(1 for t in trades if t["outcome"] == "LOSS")
    pnl    = sum(t["pnl"] for t in trades)
    gw     = sum(t["pnl"] for t in trades if t["pnl"] > 0)
    gl     = abs(sum(t["pnl"] for t in trades if t["pnl"] < 0))
    pf     = gw / gl if gl > 0 else float("inf")

    # Max drawdown on equity curve
    eq = 100.0; peak = 100.0; max_dd = 0.0
    for t in sorted(trades, key=lambda x: x.get("exit_ts", 0)):
        eq += t["pnl"]
        peak = max(peak, eq)
        max_dd = max(max_dd, (peak - eq) / peak * 100)

    avg_rr = sum(t.get("rr", 2.5) for t in trades) / len(trades)

    return {
        "trades": len(trades),
        "wr":     round(wins / len(trades) * 100, 1),
        "wins":   wins,
        "losses": losses,
        "pnl":    round(pnl, 2),
        "final":  round(100 + pnl, 2),
        "pf":     round(pf, 2),
        "dd":     round(max_dd, 1),
        "avg_rr": round(avg_rr, 2),
    }


def main():
    from backtest.fetcher import fetch_all_sync

    symbols = [
        "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT",
        "AVAXUSDT", "ADAUSDT", "DOTUSDT", "DOGEUSDT", "SUIUSDT",
    ]

    print("Loading cached data ...")
    data  = fetch_all_sync(symbols, force=False)
    ohlcv = data["ohlcv"]

    cutoff_ms = int((time.time() - _CUTOFF_DAYS * 86_400) * 1000)
    ohlcv     = _trim_ohlcv(ohlcv, cutoff_ms, warmup_bars=32)
    print(f"5m bars (BTC): {len(ohlcv.get('BTCUSDT:5m', []))}\n")

    # ── Parameter grid ─────────────────────────────────────────────────────────
    grid = {
        "window_bars":    [8, 10, 12],
        "range_max_pct":  [0.008, 0.010, 0.012],
        "tp_ratio":       [0.60, 0.75, 0.85],
        "max_vol_ratio":  [1.2, 1.5],
        "use_rsi_filter": [True, False],
    }

    # Fixed params across all runs
    base = {
        "entry_zone_pct": 0.002,
        "stop_pct":       0.003,
        "rsi_long_max":   40.0,
        "rsi_short_min":  60.0,
        "cooldown_mins":  20,
        "max_hold_bars":  6,
    }

    keys   = list(grid.keys())
    combos = list(itertools.product(*[grid[k] for k in keys]))
    print(f"Running {len(combos)} configurations...\n")

    results = []
    for i, combo in enumerate(combos):
        cfg = {**base, **dict(zip(keys, combo))}
        r   = _run_config(ohlcv, symbols, cfg)
        r["cfg"] = cfg
        results.append(r)
        if (i + 1) % 20 == 0:
            print(f"  {i+1}/{len(combos)} done...")

    # ── Sort by PnL, print top 20 ──────────────────────────────────────────────
    results.sort(key=lambda x: x["pnl"], reverse=True)

    print()
    print("=" * 110)
    print("  MICRO-RANGE FLIP PARAMETER SWEEP  (sorted by PnL, top 20)")
    print("=" * 110)
    print(f"  {'#':>3}  {'PnL':>7}  {'WR%':>5}  {'PF':>5}  {'DD%':>5}  {'Trades':>7}  "
          f"{'win':>5}  {'AvgRR':>6}  {'wbars':>6}  {'rng%':>6}  {'tp':>5}  {'vol':>5}  {'rsi':>5}")
    print("  " + "-" * 106)

    for rank, r in enumerate(results[:20], 1):
        c      = r["cfg"]
        pnl_s  = f"{'+' if r['pnl']>=0 else ''}${r['pnl']:.2f}"
        rng_s  = f"{c['range_max_pct']*100:.1f}%"
        rsi_s  = "yes" if c["use_rsi_filter"] else "no"
        print(f"  {rank:>3}  {pnl_s:>7}  {r['wr']:>5}  {r['pf']:>5}  "
              f"{r['dd']:>5}  {r['trades']:>7}  "
              f"{r['wins']:>5}  {r.get('avg_rr', 0):>6.2f}  "
              f"{c['window_bars']:>6}  {rng_s:>6}  "
              f"{c['tp_ratio']:>5}  {c['max_vol_ratio']:>5}  {rsi_s:>5}")

    print()
    print("BEST CONFIG:")
    best = results[0]
    for k, v in best["cfg"].items():
        print(f"  {k}: {v}")
    print(f"  → PnL: {best['pnl']:+.2f}  WR: {best['wr']}%  PF: {best['pf']}  "
          f"DD: {best['dd']}%  Trades: {best['trades']}  AvgRR: {best.get('avg_rr', 0):.2f}")


if __name__ == "__main__":
    main()
