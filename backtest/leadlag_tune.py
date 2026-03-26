"""Lead-lag parameter tuner — grid search over key config knobs.

Runs the backtest engine with multiple parameter combinations and prints
a ranked table so we can pick the best config before updating config.yaml.

Usage:
    python -m backtest.leadlag_tune
"""
import itertools
import logging
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(level=logging.WARNING)   # suppress engine noise during sweep

_CUTOFF_DAYS = 180


def _trim_ohlcv(ohlcv: dict, cutoff_ms: int, warmup_h: int = 22) -> dict:
    trimmed = {}
    for key, bars in ohlcv.items():
        idx   = next((i for i, b in enumerate(bars) if b["ts"] >= cutoff_ms), len(bars))
        start = max(0, idx - warmup_h)
        trimmed[key] = bars[start:]
    return trimmed


def _run_config(ohlcv, symbols, cfg_override: dict) -> dict:
    """Run a single backtest with overridden config params. Returns summary dict."""
    import yaml
    import importlib
    import backtest.leadlag_engine as eng

    # Patch the engine's module-level constants for this run
    eng._VWAP_WINDOW    = int(cfg_override.get("vwap_window_bars",    12))
    eng._MIN_BREAK      = float(cfg_override.get("min_vwap_break_pct", 0.003))
    eng._VOL_MULT       = float(cfg_override.get("vol_spike_mult",      1.5))
    eng._MAX_PREMOVE    = float(cfg_override.get("max_alt_premove_pct", 0.003))
    eng._COOLDOWN_BARS  = int(cfg_override.get("cooldown_mins",  30) // 5)
    eng._MAX_ALTS       = int(cfg_override.get("max_alts_per_signal",    3))
    eng._STOP_PCT       = float(cfg_override.get("stop_pct",   0.0020))
    eng._TP_PCT         = float(cfg_override.get("tp_pct",     0.0050))
    eng._RR             = eng._TP_PCT / eng._STOP_PCT
    eng._MAX_HOLD       = int(cfg_override.get("max_hold_bars",   6))

    # Optional filters stored for use in patched _check_btc_breakout
    eng._TREND_FILTER   = cfg_override.get("require_trend_aligned", False)
    eng._HOUR_START     = cfg_override.get("hour_start_utc", 0)
    eng._HOUR_END       = cfg_override.get("hour_end_utc",  24)

    trades = eng.run(symbols=symbols, ohlcv=ohlcv,
                     starting_capital=100.0, risk_pct=0.02)
    if not trades:
        return {"trades": 0, "wr": 0.0, "pnl": 0.0, "final": 100.0, "pf": 0.0, "dd": 0.0}

    wins   = sum(1 for t in trades if t["outcome"] == "WIN")
    losses = sum(1 for t in trades if t["outcome"] == "LOSS")
    pnl    = sum(t["pnl"] for t in trades)
    gw     = sum(t["pnl"] for t in trades if t["pnl"] > 0)
    gl     = abs(sum(t["pnl"] for t in trades if t["pnl"] < 0))
    pf     = gw / gl if gl > 0 else float("inf")

    # Max drawdown
    eq = 100.0; peak = 100.0; max_dd = 0.0
    for t in sorted(trades, key=lambda x: x.get("exit_ts", 0)):
        eq += t["pnl"]
        peak = max(peak, eq)
        max_dd = max(max_dd, (peak - eq) / peak * 100)

    return {
        "trades": len(trades),
        "wr":     round(wins / len(trades) * 100, 1),
        "wins":   wins,
        "losses": losses,
        "pnl":    round(pnl, 2),
        "final":  round(100 + pnl, 2),
        "pf":     round(pf, 2),
        "dd":     round(max_dd, 1),
    }


def main():
    from backtest.fetcher import fetch_all_sync

    symbols = ["BTCUSDT","ETHUSDT","SOLUSDT","BNBUSDT","AVAXUSDT","ADAUSDT","DOTUSDT","DOGEUSDT","SUIUSDT"]

    print("Loading cached data ...")
    data   = fetch_all_sync(symbols, force=False)
    ohlcv  = data["ohlcv"]

    cutoff_ms = int((time.time() - _CUTOFF_DAYS * 86_400) * 1000)
    ohlcv     = _trim_ohlcv(ohlcv, cutoff_ms, warmup_h=22)
    print(f"5m bars (BTC): {len(ohlcv.get('BTCUSDT:5m', []))}\n")

    # ── Parameter grid ────────────────────────────────────────────────────────
    grid = {
        "min_vwap_break_pct":  [0.004, 0.006, 0.008],
        "vol_spike_mult":      [1.5,   2.0],
        "tp_pct":              [0.005, 0.010],
        "max_hold_bars":       [6,     12],
        "require_trend_aligned": [False, True],
    }

    # Fixed params across all runs
    base = {
        "vwap_window_bars":   12,
        "stop_pct":          0.0020,
        "max_alt_premove_pct": 0.003,
        "cooldown_mins":      30,
        "max_alts_per_signal": 3,
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
        if (i + 1) % 10 == 0:
            print(f"  {i+1}/{len(combos)} done...")

    # ── Sort by PnL and print top 15 ─────────────────────────────────────────
    results.sort(key=lambda x: x["pnl"], reverse=True)

    print()
    print("=" * 100)
    print("  LEAD-LAG PARAMETER SWEEP RESULTS  (sorted by PnL, top 20)")
    print("=" * 100)
    print(f"  {'#':>3}  {'PnL':>7}  {'WR%':>5}  {'PF':>5}  {'DD%':>5}  {'Trades':>7}  "
          f"{'break%':>7}  {'vol':>5}  {'tp%':>5}  {'hold':>5}  {'trend':>6}")
    print("  " + "-" * 96)

    for rank, r in enumerate(results[:20], 1):
        c     = r["cfg"]
        brk   = f"{c['min_vwap_break_pct']*100:.1f}%"
        tp    = f"{c['tp_pct']*100:.2f}%"
        pnl_s = f"{'+' if r['pnl']>=0 else ''}${r['pnl']:.2f}"
        trend = "yes" if c["require_trend_aligned"] else "no"
        print(f"  {rank:>3}  {pnl_s:>7}  {r['wr']:>5}  {r['pf']:>5}  "
              f"{r['dd']:>5}  {r['trades']:>7}  "
              f"{brk:>7}  {c['vol_spike_mult']:>5}  {tp:>5}  "
              f"{c['max_hold_bars']:>5}  {trend:>6}")

    print()
    print("BEST CONFIG:")
    best = results[0]
    for k, v in best["cfg"].items():
        print(f"  {k}: {v}")
    print(f"  → PnL: {best['pnl']:+.2f}  WR: {best['wr']}%  PF: {best['pf']}  DD: {best['dd']}%  Trades: {best['trades']}")


if __name__ == "__main__":
    main()
