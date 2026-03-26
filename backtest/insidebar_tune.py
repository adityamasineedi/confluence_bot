"""1H inside bar flip parameter tuner — grid search.

Usage:
    python -m backtest.insidebar_tune
"""
import itertools
import logging
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
logging.basicConfig(level=logging.WARNING)

_CUTOFF_DAYS = 180


def _trim_ohlcv(ohlcv: dict, cutoff_ms: int, warmup: int = 10) -> dict:
    trimmed = {}
    for key, bars in ohlcv.items():
        if ":1h" not in key:
            continue
        idx = next((i for i, b in enumerate(bars) if b["ts"] >= cutoff_ms), len(bars))
        trimmed[key] = bars[max(0, idx - warmup):]
    return trimmed


def _run_config(ohlcv: dict, symbols: list[str], cfg: dict) -> dict:
    import backtest.insidebar_engine as eng
    eng._MIN_INSIDE     = int(cfg.get("min_inside_bars",  2))
    eng._MAX_ZONE_PCT   = float(cfg.get("max_zone_pct",   0.015))
    eng._ENTRY_ZONE_PCT = float(cfg.get("entry_zone_pct", 0.002))
    eng._SL_BUFFER_PCT  = float(cfg.get("sl_buffer_pct",  0.002))
    eng._RR_RATIO       = float(cfg.get("rr_ratio",        1.5))
    eng._MAX_HOLD       = int(cfg.get("max_hold_bars",      6))
    eng._COOLDOWN_BARS  = max(1, int(cfg.get("cooldown_mins", 60) // 60))
    eng._WARMUP_BARS    = max(eng._MIN_INSIDE + 4, 8)

    trades = eng.run(symbols=symbols, ohlcv=ohlcv,
                     starting_capital=100.0, risk_pct=0.01)
    if not trades:
        return {"trades": 0, "wr": 0.0, "pnl": 0.0, "pf": 0.0, "dd": 0.0}

    wins = sum(1 for t in trades if t["outcome"] == "WIN")
    pnl  = sum(t["pnl"] for t in trades)
    gw   = sum(t["pnl"] for t in trades if t["pnl"] > 0)
    gl   = abs(sum(t["pnl"] for t in trades if t["pnl"] < 0))
    pf   = gw / gl if gl > 0 else float("inf")

    eq = 100.0; peak = 100.0; max_dd = 0.0
    for t in sorted(trades, key=lambda x: x.get("exit_ts", 0)):
        eq += t["pnl"]
        peak = max(peak, eq)
        max_dd = max(max_dd, (peak - eq) / peak * 100)

    return {
        "trades": len(trades),
        "wr":     round(wins / len(trades) * 100, 1),
        "pnl":    round(pnl, 2),
        "pf":     round(pf, 2),
        "dd":     round(max_dd, 1),
    }


def main():
    from backtest.fetcher import fetch_all_sync

    symbols = [
        "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT",
        "AVAXUSDT", "ADAUSDT", "DOTUSDT", "DOGEUSDT", "SUIUSDT",
    ]
    print("Loading cached data ...")
    data  = fetch_all_sync(symbols, force=False)
    # Keep only 1h timeframe data for speed
    ohlcv = {k: v for k, v in data["ohlcv"].items() if ":1h" in k}
    cutoff_ms = int((time.time() - _CUTOFF_DAYS * 86_400) * 1000)
    ohlcv = _trim_ohlcv(ohlcv, cutoff_ms)
    print(f"1h bars (BTC): {len(ohlcv.get('BTCUSDT:1h', []))}\n")

    grid = {
        "min_inside_bars": [2, 3],
        "max_zone_pct":    [0.010, 0.015, 0.020],
        "entry_zone_pct":  [0.001, 0.002, 0.003],
        "rr_ratio":        [1.2, 1.5, 2.0],
        "max_hold_bars":   [4, 6, 8],
    }
    base = {"sl_buffer_pct": 0.002, "cooldown_mins": 60}

    keys   = list(grid.keys())
    combos = list(itertools.product(*[grid[k] for k in keys]))
    print(f"Running {len(combos)} configurations...\n")

    results = []
    for i, combo in enumerate(combos):
        cfg = {**base, **dict(zip(keys, combo))}
        r   = _run_config(ohlcv, symbols, cfg)
        r["cfg"] = cfg
        results.append(r)
        if (i + 1) % 25 == 0:
            print(f"  {i+1}/{len(combos)} done...")

    results.sort(key=lambda x: x["pnl"], reverse=True)

    print()
    print("=" * 105)
    print("  INSIDE BAR FLIP SWEEP  (top 20 by PnL)")
    print("=" * 105)
    print(f"  {'#':>3}  {'PnL':>7}  {'WR%':>5}  {'PF':>5}  {'DD%':>5}  "
          f"{'Trades':>7}  {'min_in':>7}  {'zn%':>6}  {'ez%':>6}  {'RR':>5}  {'hold':>5}")
    print("  " + "-" * 101)

    for rank, r in enumerate(results[:20], 1):
        c     = r["cfg"]
        pnl_s = f"{'+' if r['pnl']>=0 else ''}${r['pnl']:.2f}"
        print(f"  {rank:>3}  {pnl_s:>7}  {r['wr']:>5}  {r['pf']:>5}  {r['dd']:>5}  "
              f"{r['trades']:>7}  {c['min_inside_bars']:>7}  "
              f"{c['max_zone_pct']*100:>6.1f}  {c['entry_zone_pct']*100:>6.2f}  "
              f"{c['rr_ratio']:>5}  {c['max_hold_bars']:>5}")

    print()
    best = results[0]
    print("BEST CONFIG:")
    for k, v in best["cfg"].items():
        print(f"  {k}: {v}")
    print(f"  → PnL: {best['pnl']:+.2f}  WR: {best['wr']}%  PF: {best['pf']}  "
          f"DD: {best['dd']}%  Trades: {best['trades']}")


if __name__ == "__main__":
    main()
