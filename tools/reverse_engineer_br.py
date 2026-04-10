"""
tools/reverse_engineer_br.py
Data-driven reverse engineering of breakout_retest for BTC + ETH.

Reuses the existing backtest engine to generate trades, then post-processes
each trade to extract:
  - Market regime at entry (TREND / RANGE / CRASH / PUMP / BREAKOUT)
  - 4H ADX bucket (low / mid / high) at entry
  - Time of day (UTC hour bucket)
  - Day of week
  - 1H ATR percentile (volatility regime)
  - Maximum Favorable Excursion (MFE) — how far it ran in our favor
  - Maximum Adverse Excursion (MAE) — how far it ran against us
  - Hold time in 5M bars
  - Win/loss outcome and R-multiple

Aggregates by every dimension and prints a brutal honest report:
  - Which regimes are profitable, which are net losers
  - Whether SL is too tight (high MAE on winners → SL eats good trades)
  - Whether TP is too far (low MFE on losers → TP never reached)
  - Time-of-day buckets that should be filtered out

Usage:
    python tools/reverse_engineer_br.py
    python tools/reverse_engineer_br.py --symbol BTCUSDT
    python tools/reverse_engineer_br.py --symbol ETHUSDT --from-date 2024-01-01
"""
import argparse
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from backtest.engine import (
    load, run_breakout_retest, atr, ema,
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


def _classify_regime_at(bars_4h: np.ndarray, bars_1d: np.ndarray, ts_ms: float) -> str:
    """Classify regime using only 4H/1D bars whose close <= ts_ms."""
    if bars_4h is None or len(bars_4h) == 0:
        return "TREND"
    j4 = int(np.searchsorted(bars_4h[:, TS], int(ts_ms), side="right")) - 1
    if j4 < 50:
        return "TREND"
    win4_h = bars_4h[max(0, j4 - 60): j4 + 1]
    closes_4h = win4_h[:, C].tolist()
    highs_4h  = win4_h[:, H].tolist()
    lows_4h   = win4_h[:, L].tolist()

    closes_1d: list[float] = []
    if bars_1d is not None and len(bars_1d) > 0:
        j1 = int(np.searchsorted(bars_1d[:, TS], int(ts_ms), side="right")) - 1
        if j1 >= 0:
            win1 = bars_1d[max(0, j1 - 80): j1 + 1]
            closes_1d = win1[:, C].tolist()

    return classify_regime(closes_4h, highs_4h, lows_4h, closes_1d)


def _adx_bucket_at(bars_4h: np.ndarray, ts_ms: float) -> str:
    if bars_4h is None or len(bars_4h) == 0:
        return "?"
    j4 = int(np.searchsorted(bars_4h[:, TS], int(ts_ms), side="right")) - 1
    if j4 < 30:
        return "?"
    win = bars_4h[max(0, j4 - 30): j4 + 1]
    info = _calc_adx(
        win[:, H].tolist(), win[:, L].tolist(), win[:, C].tolist(),
    )
    adx = info["adx"]
    if adx < 18:    return "low<18"
    if adx < 25:    return "mid18-25"
    if adx < 35:    return "high25-35"
    return "veryhigh35+"


def _atr_pct_at(bars_1h: np.ndarray, ts_ms: float) -> float:
    """Return current 1H ATR as % of price (volatility level)."""
    if bars_1h is None or len(bars_1h) == 0:
        return 0.0
    j = int(np.searchsorted(bars_1h[:, TS], int(ts_ms), side="right")) - 1
    if j < 20:
        return 0.0
    atr_arr = atr(bars_1h[:j + 1], period=14)
    last_atr = atr_arr[-1] if len(atr_arr) else 0.0
    last_close = bars_1h[j, C]
    return (last_atr / last_close) * 100 if last_close > 0 else 0.0


def _hour_of_day(ts_ms: float) -> int:
    return datetime.fromtimestamp(ts_ms / 1000, timezone.utc).hour


def _day_of_week(ts_ms: float) -> int:
    return datetime.fromtimestamp(ts_ms / 1000, timezone.utc).weekday()


# ── MFE / MAE walk ───────────────────────────────────────────────────────────

def _walk_mfe_mae(
    bars_5m: np.ndarray,
    entry_idx: int,
    exit_idx: int,
    entry_price: float,
    direction: str,
    sl_dist: float,
) -> tuple[float, float, int]:
    """Return (mfe_R, mae_R, hold_bars).

    MFE = best favourable excursion in R-multiples (positive value)
    MAE = worst adverse excursion in R-multiples (positive value, magnitude)
    """
    if exit_idx <= entry_idx:
        return 0.0, 0.0, 0
    fut = bars_5m[entry_idx + 1: exit_idx + 1]
    if len(fut) == 0 or sl_dist <= 0:
        return 0.0, 0.0, 0
    if direction == "LONG":
        max_h = float(fut[:, H].max())
        min_l = float(fut[:, L].min())
        mfe_r = (max_h - entry_price) / sl_dist
        mae_r = (entry_price - min_l) / sl_dist
    else:
        max_h = float(fut[:, H].max())
        min_l = float(fut[:, L].min())
        mfe_r = (entry_price - min_l) / sl_dist
        mae_r = (max_h - entry_price) / sl_dist
    return max(0.0, mfe_r), max(0.0, mae_r), len(fut)


# ── Aggregator ───────────────────────────────────────────────────────────────

class Bucket:
    __slots__ = ("n", "wins", "losses", "tos", "gross_win", "gross_loss",
                 "mfe_sum", "mae_sum", "hold_sum")

    def __init__(self):
        self.n = 0
        self.wins = 0
        self.losses = 0
        self.tos = 0
        self.gross_win = 0.0
        self.gross_loss = 0.0
        self.mfe_sum = 0.0
        self.mae_sum = 0.0
        self.hold_sum = 0

    def add(self, outcome: str, pnl_r: float, mfe: float, mae: float, hold: int):
        self.n += 1
        if outcome == "TP":
            self.wins += 1
        elif outcome == "SL":
            self.losses += 1
        else:
            self.tos += 1
        if pnl_r > 0:
            self.gross_win += pnl_r
        else:
            self.gross_loss += -pnl_r
        self.mfe_sum += mfe
        self.mae_sum += mae
        self.hold_sum += hold

    def stats(self) -> dict:
        if self.n == 0:
            return {"n": 0}
        wr = self.wins / self.n * 100
        pf = (self.gross_win / self.gross_loss) if self.gross_loss > 0 else float("inf")
        avg_mfe = self.mfe_sum / self.n
        avg_mae = self.mae_sum / self.n
        avg_hold = self.hold_sum / self.n
        net_r = self.gross_win - self.gross_loss
        return {
            "n":       self.n,
            "wins":    self.wins,
            "losses":  self.losses,
            "tos":     self.tos,
            "wr":      wr,
            "pf":      pf,
            "net_r":   net_r,
            "avg_mfe": avg_mfe,
            "avg_mae": avg_mae,
            "avg_hold": avg_hold,
        }


def _pf_str(pf: float) -> str:
    return "inf" if pf == float("inf") else f"{pf:.2f}"


def _print_table(title: str, buckets: dict[str, Bucket], min_n: int = 10):
    print(f"\n── {title} ──")
    print(f"  {'bucket':<20} {'n':>5} {'WR%':>6} {'PF':>6} "
          f"{'netR':>8} {'avgMFE':>7} {'avgMAE':>7} {'hold':>6}")
    print(f"  {'-'*20} {'-'*5} {'-'*6} {'-'*6} {'-'*8} {'-'*7} {'-'*7} {'-'*6}")
    rows = []
    for key, b in buckets.items():
        st = b.stats()
        if st["n"] < min_n:
            continue
        rows.append((key, st))
    rows.sort(key=lambda x: x[1]["pf"], reverse=True)
    for key, st in rows:
        print(f"  {str(key):<20} {st['n']:>5} {st['wr']:>5.1f}% "
              f"{_pf_str(st['pf']):>6} {st['net_r']:>+8.1f} "
              f"{st['avg_mfe']:>7.2f} {st['avg_mae']:>7.2f} {st['avg_hold']:>6.1f}")


# ── Main analysis ────────────────────────────────────────────────────────────

def analyze_symbol(symbol: str, from_date: str, to_date: str) -> dict:
    print(f"\n{'='*78}")
    print(f"  REVERSE ENGINEERING — {symbol}  ({from_date} → {to_date})")
    print(f"{'='*78}")

    data = load(symbol)
    if data is None:
        print(f"  No data for {symbol}")
        return {}
    btc_data = load("BTCUSDT") if symbol != "BTCUSDT" else data

    b5m = data.get(f"{symbol}:5m")
    b1h = data.get(f"{symbol}:1h")
    b4h = data.get(f"{symbol}:4h")
    b1d = data.get(f"{symbol}:1d")
    if b5m is None:
        print("  No 5m data")
        return {}

    from_ts = _ms(from_date)
    to_ts   = _ms(to_date)

    # Step 1: run the backtest engine to get the trade list
    trades = run_breakout_retest(symbol, data, btc_data, from_ts, to_ts)
    if not trades:
        print("  No trades generated")
        return {}
    print(f"  Generated {len(trades)} trades from backtest engine")

    # Step 2: post-process each trade
    by_regime    = defaultdict(Bucket)
    by_adx       = defaultdict(Bucket)
    by_atrpct    = defaultdict(Bucket)
    by_hour      = defaultdict(Bucket)
    by_dow       = defaultdict(Bucket)
    by_dir       = defaultdict(Bucket)
    by_regime_dir = defaultdict(Bucket)

    overall = Bucket()
    mfe_winners = []
    mae_losers  = []
    mfe_losers  = []
    mae_winners = []
    timeout_pnl = []

    for t in trades:
        eb        = t.bar_idx
        entry     = t.entry
        stop      = t.stop
        sl_dist   = abs(entry - stop)
        outcome   = t.outcome
        pnl_r     = t.pnl_r
        direction = t.direction
        ts_entry  = float(b5m[eb, TS])

        # Find exit bar by re-walking until first SL/TP/timeout (need this for MFE/MAE)
        if outcome == "SL" or outcome == "TP":
            # Walk forward to find the actual exit bar
            exit_idx = eb
            for k in range(1, min(49, len(b5m) - eb)):
                bar = b5m[eb + k]
                if direction == "LONG":
                    if bar[L] <= stop:  exit_idx = eb + k; break
                    if bar[H] >= t.tp:   exit_idx = eb + k; break
                else:
                    if bar[H] >= stop:  exit_idx = eb + k; break
                    if bar[L] <= t.tp:   exit_idx = eb + k; break
            if exit_idx == eb:
                exit_idx = min(eb + 48, len(b5m) - 1)
        else:
            exit_idx = min(eb + 48, len(b5m) - 1)

        mfe, mae, hold = _walk_mfe_mae(b5m, eb, exit_idx, entry, direction, sl_dist)

        regime  = _classify_regime_at(b4h, b1d, ts_entry)
        adx_buc = _adx_bucket_at(b4h, ts_entry)
        atr_pct = _atr_pct_at(b1h, ts_entry)
        if   atr_pct < 0.4: atr_buc = "atr<0.4%"
        elif atr_pct < 0.7: atr_buc = "atr0.4-0.7%"
        elif atr_pct < 1.0: atr_buc = "atr0.7-1.0%"
        else:               atr_buc = "atr>1.0%"
        hour    = _hour_of_day(ts_entry)
        dow     = _day_of_week(ts_entry)
        if   0 <= hour < 4:    hour_buc = "00-04UTC"
        elif 4 <= hour < 8:    hour_buc = "04-08UTC"
        elif 8 <= hour < 12:   hour_buc = "08-12UTC"
        elif 12 <= hour < 16:  hour_buc = "12-16UTC"
        elif 16 <= hour < 20:  hour_buc = "16-20UTC"
        else:                  hour_buc = "20-24UTC"
        dow_buc = ["Mon","Tue","Wed","Thu","Fri","Sat","Sun"][dow]

        overall.add(outcome, pnl_r, mfe, mae, hold)
        by_regime[regime].add(outcome, pnl_r, mfe, mae, hold)
        by_adx[adx_buc].add(outcome, pnl_r, mfe, mae, hold)
        by_atrpct[atr_buc].add(outcome, pnl_r, mfe, mae, hold)
        by_hour[hour_buc].add(outcome, pnl_r, mfe, mae, hold)
        by_dow[dow_buc].add(outcome, pnl_r, mfe, mae, hold)
        by_dir[direction].add(outcome, pnl_r, mfe, mae, hold)
        by_regime_dir[f"{regime}/{direction}"].add(outcome, pnl_r, mfe, mae, hold)

        if outcome == "TP":
            mae_winners.append(mae)   # how close did winners come to SL?
            mfe_winners.append(mfe)
        elif outcome == "SL":
            mfe_losers.append(mfe)    # how far did losers run in our favour first?
            mae_losers.append(mae)
        else:
            timeout_pnl.append(pnl_r)

    # ── Print report ──
    st = overall.stats()
    print(f"\n  OVERALL    n={st['n']}  WR={st['wr']:.1f}%  PF={_pf_str(st['pf'])}  "
          f"netR={st['net_r']:+.1f}  avgMFE={st['avg_mfe']:.2f}R  "
          f"avgMAE={st['avg_mae']:.2f}R  avgHold={st['avg_hold']:.0f} bars "
          f"({st['avg_hold']*5:.0f} min)")

    _print_table("BY REGIME (4H ADX + 7D return classification)", by_regime, min_n=20)
    _print_table("BY DIRECTION", by_dir, min_n=10)
    _print_table("BY REGIME × DIRECTION", by_regime_dir, min_n=15)
    _print_table("BY 4H ADX BUCKET", by_adx, min_n=20)
    _print_table("BY 1H ATR%  (volatility regime)", by_atrpct, min_n=20)
    _print_table("BY HOUR OF DAY (UTC)", by_hour, min_n=20)
    _print_table("BY DAY OF WEEK", by_dow, min_n=20)

    # SL/TP quality analysis
    print(f"\n── SL/TP QUALITY ──")
    if mae_winners:
        avg_mae_w = sum(mae_winners) / len(mae_winners)
        max_mae_w = max(mae_winners)
        p90_mae_w = sorted(mae_winners)[int(len(mae_winners) * 0.9)]
        print(f"  Winners (TP hit, n={len(mae_winners)}):")
        print(f"    avgMAE = {avg_mae_w:.2f}R   p90MAE = {p90_mae_w:.2f}R   maxMAE = {max_mae_w:.2f}R")
        print(f"    → Winners came within {avg_mae_w*100:.0f}% of SL on avg before reversing.")
        if avg_mae_w > 0.7:
            print(f"    ⚠ SL might be tighter than necessary — winners often touch -{avg_mae_w:.2f}R")
        elif avg_mae_w < 0.3:
            print(f"    ✓ SL has good buffer — winners rarely come close")
    if mfe_losers:
        avg_mfe_l = sum(mfe_losers) / len(mfe_losers)
        max_mfe_l = max(mfe_losers)
        p90_mfe_l = sorted(mfe_losers)[int(len(mfe_losers) * 0.9)]
        print(f"  Losers (SL hit, n={len(mfe_losers)}):")
        print(f"    avgMFE = {avg_mfe_l:.2f}R   p90MFE = {p90_mfe_l:.2f}R   maxMFE = {max_mfe_l:.2f}R")
        print(f"    → Losers ran +{avg_mfe_l:.2f}R in our favour before turning around.")
        if avg_mfe_l > 1.0:
            print(f"    ⚠ Losers often hit +{avg_mfe_l:.2f}R first — partial TP at +1R would salvage many")
        elif avg_mfe_l < 0.5:
            print(f"    ✓ Losers fail fast — they don't tease before stopping out")
    if mfe_winners:
        avg_mfe_w = sum(mfe_winners) / len(mfe_winners)
        print(f"  Winners avg MFE: {avg_mfe_w:.2f}R  (TP at {2.2:.1f}R)")
        if avg_mfe_w > 3.5:
            print(f"    ⚠ Winners ran much further than TP — moving TP to {avg_mfe_w*0.7:.1f}R could capture more")
    if timeout_pnl:
        avg_to = sum(timeout_pnl) / len(timeout_pnl)
        print(f"  Timeouts (n={len(timeout_pnl)}): avg pnl_R = {avg_to:+.2f}R")
        if avg_to < -0.3:
            print(f"    ⚠ Timeouts are net losers — shorter max_hold could help")
        elif avg_to > 0.3:
            print(f"    → Timeouts are net winners — longer max_hold might capture more")

    return {"overall": st, "by_regime": {k: b.stats() for k, b in by_regime.items()}}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="BOTH",
                    help="BTCUSDT, ETHUSDT, or BOTH (default)")
    ap.add_argument("--from-date", default="2023-01-01")
    ap.add_argument("--to-date",   default="2026-04-01")
    args = ap.parse_args()

    if args.symbol.upper() == "BOTH":
        syms = ["BTCUSDT", "ETHUSDT"]
    else:
        syms = [args.symbol.upper()]

    for s in syms:
        analyze_symbol(s, args.from_date, args.to_date)

    print(f"\n{'='*78}")
    print("  Done.")
    print(f"{'='*78}\n")


if __name__ == "__main__":
    main()
