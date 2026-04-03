"""Sliding-window backtest — replays historical OHLCV bar-by-bar
through every active scorer and records entry/exit results."""
import asyncio, json, os, sys
from dataclasses import dataclass
from typing import Optional

# Add project root to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


# ── Pre-computed indicator cache ──────────────────────────────────────────────

import numpy as np

def _ema_series(closes: list[float], period: int) -> list[float]:
    """Compute full EMA series in one pass. Returns list same length as closes."""
    if len(closes) < period:
        return [0.0] * len(closes)
    k   = 2.0 / (period + 1)
    ema = [0.0] * len(closes)
    # seed with SMA
    ema[period - 1] = sum(closes[:period]) / period
    for i in range(period, len(closes)):
        ema[i] = closes[i] * k + ema[i - 1] * (1.0 - k)
    return ema


def _atr_series(bars: list[dict], period: int = 14) -> list[float]:
    """Compute full ATR series. Returns list same length as bars (0 for warmup)."""
    trs  = [0.0]
    for i in range(1, len(bars)):
        h, l, pc = bars[i]["h"], bars[i]["l"], bars[i-1]["c"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    atrs = [0.0] * len(trs)
    if len(trs) >= period:
        atrs[period - 1] = sum(trs[1:period]) / (period - 1)
        for i in range(period, len(trs)):
            atrs[i] = (atrs[i-1] * (period - 1) + trs[i]) / period
    return atrs


def _adx_series(bars: list[dict], period: int = 14) -> list[float]:
    """Compute full ADX series. Returns list same length as bars."""
    n    = len(bars)
    adx  = [0.0] * n
    if n < period + 1:
        return adx
    plus_dm  = [0.0] * n
    minus_dm = [0.0] * n
    atr_s    = [0.0] * n
    for i in range(1, n):
        up   = bars[i]["h"] - bars[i-1]["h"]
        down = bars[i-1]["l"] - bars[i]["l"]
        plus_dm[i]  = up   if up > down and up > 0   else 0.0
        minus_dm[i] = down if down > up and down > 0 else 0.0
        h, l, pc    = bars[i]["h"], bars[i]["l"], bars[i-1]["c"]
        atr_s[i]    = max(h - l, abs(h - pc), abs(l - pc))
    # Smooth with Wilder's method
    def wilder(vals, p):
        out = [0.0] * len(vals)
        out[p] = sum(vals[1:p+1])
        for i in range(p+1, len(vals)):
            out[i] = out[i-1] - out[i-1]/p + vals[i]
        return out
    satr = wilder(atr_s, period)
    sp   = wilder(plus_dm, period)
    sm   = wilder(minus_dm, period)
    di_plus  = [100 * sp[i] / satr[i] if satr[i] > 0 else 0.0 for i in range(n)]
    di_minus = [100 * sm[i] / satr[i] if satr[i] > 0 else 0.0 for i in range(n)]
    dx = [abs(di_plus[i] - di_minus[i]) / (di_plus[i] + di_minus[i]) * 100
          if (di_plus[i] + di_minus[i]) > 0 else 0.0
          for i in range(n)]
    # ADX = Wilder smoothed DX
    out_adx = [0.0] * n
    out_adx[period * 2] = sum(dx[period:period*2]) / period
    for i in range(period * 2 + 1, n):
        out_adx[i] = (out_adx[i-1] * (period-1) + dx[i]) / period
    return out_adx


def _fvg_signals_series(bars: list[dict]) -> list[tuple[bool, bool]]:
    """Pre-scan all FVGs across the bar series.
    Returns list of (is_bullish_fvg, is_bearish_fvg) per bar.
    A bullish FVG exists at bar[i] when bar[i-2].high < bar[i].low (gap up).
    A bearish FVG exists at bar[i] when bar[i-2].low > bar[i].high (gap down).
    """
    n      = len(bars)
    result = [(False, False)] * n
    for i in range(2, n):
        bull = bars[i-2]["h"] < bars[i]["l"]   # gap up between bar[-3] and bar[-1]
        bear = bars[i-2]["l"] > bars[i]["h"]   # gap down
        result[i] = (bull, bear)
    return result


class PrecomputedIndicators:
    """Holds all pre-computed indicator series for one symbol.
    Lookup at bar index is O(1) instead of O(n) recalculation."""

    def __init__(self, symbol: str, ohlcv_data: dict) -> None:
        self.symbol = symbol

        # Load bar series for each timeframe
        self.bars_1h  = ohlcv_data.get(f"{symbol}:1h",  [])
        self.bars_4h  = ohlcv_data.get(f"{symbol}:4h",  [])
        self.bars_15m = ohlcv_data.get(f"{symbol}:15m", [])
        self.bars_5m  = ohlcv_data.get(f"{symbol}:5m",  [])
        self.bars_1d  = ohlcv_data.get(f"{symbol}:1d",  [])
        self.bars_1w  = ohlcv_data.get(f"{symbol}:1w",  [])

        # Pre-compute everything once
        closes_1h  = [b["c"] for b in self.bars_1h]
        closes_4h  = [b["c"] for b in self.bars_4h]
        closes_15m = [b["c"] for b in self.bars_15m]
        closes_1d  = [b["c"] for b in self.bars_1d]
        closes_1w  = [b["c"] for b in self.bars_1w]

        # EMA series
        self.ema21_1h  = _ema_series(closes_1h,  21)
        self.ema50_1h  = _ema_series(closes_1h,  50)
        self.ema200_1d = _ema_series(closes_1d,  200)
        self.ema21_4h  = _ema_series(closes_4h,  21)
        self.ema10_1w  = _ema_series(closes_1w,  10)
        self.ema21_15m = _ema_series(closes_15m, 21)
        self.ema50_15m = _ema_series(closes_15m, 50)

        # ATR series
        self.atr_1h  = _atr_series(self.bars_1h)
        self.atr_15m = _atr_series(self.bars_15m)
        self.atr_4h  = _atr_series(self.bars_4h)

        # ADX series (4H is used for regime)
        self.adx_4h = _adx_series(self.bars_4h)

        # FVG scan
        self.fvg_1h = _fvg_signals_series(self.bars_1h)

        # Timestamp index maps for fast bar lookups across timeframes
        self._ts_idx_1h  = {b["ts"]: i for i, b in enumerate(self.bars_1h)}
        self._ts_idx_4h  = {b["ts"]: i for i, b in enumerate(self.bars_4h)}
        self._ts_idx_1d  = {b["ts"]: i for i, b in enumerate(self.bars_1d)}
        self._ts_idx_1w  = {b["ts"]: i for i, b in enumerate(self.bars_1w)}

        print(f"  [PRECOMPUTE] {symbol}: 1H={len(self.bars_1h)} 4H={len(self.bars_4h)} "
              f"15M={len(self.bars_15m)} 1D={len(self.bars_1d)} 1W={len(self.bars_1w)}")


# ── FastBacktestCache ─────────────────────────────────────────────────────────

class FastBacktestCache:
    """O(1) indicator lookups using pre-computed series.
    50-100x faster than the naive BacktestCache for large datasets."""

    def __init__(self, precomp: "PrecomputedIndicators", bar_idx_1h: int) -> None:
        self._p   = precomp
        self._idx = bar_idx_1h   # current 1H bar index

    # ── Core OHLCV access ─────────────────────────────────────────────────────

    def get_ohlcv(self, symbol: str, window: int, tf: str) -> list[dict]:
        bars = self._bars(tf)
        idx  = self._idx_for_tf(tf)
        end  = min(idx, len(bars))
        start = max(0, end - window)
        return bars[start:end]

    def get_closes(self, symbol: str, window: int, tf: str) -> list[float]:
        return [b["c"] for b in self.get_ohlcv(symbol, window, tf)]

    def _bars(self, tf: str) -> list[dict]:
        m = {"1h": self._p.bars_1h, "4h": self._p.bars_4h,
             "15m": self._p.bars_15m, "5m": self._p.bars_5m,
             "1d": self._p.bars_1d, "1w": self._p.bars_1w}
        return m.get(tf, self._p.bars_1h)

    def _idx_for_tf(self, tf: str) -> int:
        """Convert 1H bar index to equivalent index in another timeframe."""
        if tf == "1h":  return self._idx
        # Use current bar timestamp to find matching bar in other TF
        if self._idx >= len(self._p.bars_1h):
            return len(self._bars(tf))
        current_ts = self._p.bars_1h[self._idx - 1]["ts"] if self._idx > 0 else 0
        ts_maps = {"4h": self._p._ts_idx_4h, "1d": self._p._ts_idx_1d,
                   "1w": self._p._ts_idx_1w}
        if tf in ts_maps:
            # Find the last bar at or before current timestamp
            bars_tf = self._bars(tf)
            idx = 0
            for i, b in enumerate(bars_tf):
                if b["ts"] <= current_ts:
                    idx = i + 1
                else:
                    break
            return idx
        # Fallback: estimate by ratio
        ratios = {"15m": 4, "5m": 12, "4h": 0.25, "1d": 1/24, "1w": 1/168}
        r = ratios.get(tf, 1.0)
        return max(1, int(self._idx * r))

    # ── Pre-computed indicator lookups — all O(1) ────────────────────────────

    def get_last_price(self, symbol: str) -> float:
        if self._idx > 0 and self._idx <= len(self._p.bars_1h):
            return self._p.bars_1h[self._idx - 1]["c"]
        return 0.0

    def get_account_balance(self) -> float:
        return 10000.0

    def get_range_high(self, symbol: str):
        bars_4h = self.get_ohlcv(symbol, 50, "4h")
        return max(b["h"] for b in bars_4h) if bars_4h else None

    def get_range_low(self, symbol: str):
        bars_4h = self.get_ohlcv(symbol, 50, "4h")
        return min(b["l"] for b in bars_4h) if bars_4h else None

    def get_funding_rate(self, symbol: str): return 0.0001
    def get_oi(self, symbol: str, offset_hours: int = 0, exchange: str = "binance"): return None
    def get_liq_clusters(self, symbol: str): return []
    def get_long_short_ratio(self, symbol: str): return None
    def get_vol_24h(self, symbol: str): return 1e9
    def get_btc_dominance(self): return 0.50
    def get_range_start_ts(self, symbol: str): return 0
    def get_cvd(self, symbol: str, window: int, tf: str): return list(range(window))
    def get_vol_ma(self, symbol: str, window: int, tf: str) -> float:
        bars = self.get_ohlcv(symbol, window, tf)
        return sum(b["v"] for b in bars) / len(bars) if bars else 0.0
    def get_basis_history(self, s, n): return []
    def get_skew_history(self, s, n): return []
    def get_agg_trades(self, s, w): return []
    def get_exchange_inflow(self, s): return None
    def get_inflow_ma(self, s, d): return None
    def push_btc_dominance(self, v): pass
    def set_funding_rate(self, *a): pass
    def push_candle(self, *a): pass
    def push_oi(self, *a): pass
    def push_agg_trade(self, *a): pass
    def push_cvd_value(self, *a): pass

    def get_key_levels(self, symbol: str) -> dict:
        idx_1d = self._idx_for_tf("1d")
        idx_1w = self._idx_for_tf("1w")
        d = self._p.bars_1d
        w = self._p.bars_1w
        return {
            "pdh": d[idx_1d - 2]["h"] if idx_1d >= 2 else 0.0,
            "pdl": d[idx_1d - 2]["l"] if idx_1d >= 2 else 0.0,
            "pwh": w[idx_1w - 2]["h"] if idx_1w >= 2 else 0.0,
            "pwl": w[idx_1w - 2]["l"] if idx_1w >= 2 else 0.0,
        }

    def near_key_level(self, symbol: str, price: float, tol: float = 0.003) -> bool:
        levels = self.get_key_levels(symbol)
        return any(v > 0 and abs(price - v) / v <= tol for v in levels.values())


# ── Trade simulation ──────────────────────────────────────────────────────────

@dataclass
class SimTrade:
    symbol:    str
    direction: str
    strategy:  str
    entry:     float
    stop:      float
    tp:        float
    bar_idx:   int
    outcome:   str   = "OPEN"   # "TP" | "SL" | "TIMEOUT"
    exit_price: float = 0.0
    pnl_r:     float = 0.0      # PnL in R (multiples of risk)


def simulate_trade(
    trade: SimTrade,
    future_bars: list[dict],
    max_bars: int = 48,
) -> SimTrade:
    """Walk forward through future_bars to find TP/SL hit."""
    sl_dist = abs(trade.entry - trade.stop)
    if sl_dist == 0:
        trade.outcome = "TIMEOUT"
        return trade

    for bar in future_bars[:max_bars]:
        if trade.direction == "LONG":
            if bar["l"] <= trade.stop:
                trade.outcome    = "SL"
                trade.exit_price = trade.stop
                trade.pnl_r      = -1.0
                return trade
            if bar["h"] >= trade.tp:
                trade.outcome    = "TP"
                trade.exit_price = trade.tp
                trade.pnl_r      = abs(trade.tp - trade.entry) / sl_dist
                return trade
        else:  # SHORT
            if bar["h"] >= trade.stop:
                trade.outcome    = "SL"
                trade.exit_price = trade.stop
                trade.pnl_r      = -1.0
                return trade
            if bar["l"] <= trade.tp:
                trade.outcome    = "TP"
                trade.exit_price = trade.tp
                trade.pnl_r      = abs(trade.entry - trade.tp) / sl_dist
                return trade

    # Timed out — close at last bar close
    last_price = future_bars[min(max_bars - 1, len(future_bars) - 1)]["c"] if future_bars else trade.entry
    trade.outcome    = "TIMEOUT"
    trade.exit_price = last_price
    if trade.direction == "LONG":
        trade.pnl_r = (last_price - trade.entry) / sl_dist
    else:
        trade.pnl_r = (trade.entry - last_price) / sl_dist
    return trade


# ── Stats ─────────────────────────────────────────────────────────────────────

def compute_stats(trades: list[SimTrade]) -> dict:
    if not trades:
        return {"trades": 0, "wr": 0, "pf": 0, "avg_r": 0,
                "wins": 0, "losses": 0, "timeouts": 0,
                "gross_win": 0, "gross_loss": 0}

    wins    = [t for t in trades if t.outcome == "TP"]
    losses  = [t for t in trades if t.outcome in ("SL", "TIMEOUT") and t.pnl_r < 0]
    gross_w = sum(t.pnl_r for t in wins)
    gross_l = abs(sum(t.pnl_r for t in losses))
    pf      = round(gross_w / gross_l, 2) if gross_l > 0 else 9.99
    wr      = round(len(wins) / len(trades) * 100, 1)
    avg_r   = round(sum(t.pnl_r for t in trades) / len(trades), 3)

    return {
        "trades":     len(trades),
        "wins":       len(wins),
        "losses":     len(losses),
        "timeouts":   len([t for t in trades if t.outcome == "TIMEOUT"]),
        "wr":         wr,
        "pf":         pf,
        "avg_r":      avg_r,
        "gross_win":  round(gross_w, 2),
        "gross_loss": round(gross_l, 2),
    }


# ── Routing pre-check ────────────────────────────────────────────────────────

def strategy_ever_active(symbol: str, strategy: str) -> bool:
    """Check if this strategy appears in ANY regime for this symbol.
    If not — skip entirely, no point scanning a single bar.
    """
    import yaml, os
    cfg_path = os.path.join(os.path.dirname(__file__), "..", "config.yaml")
    with open(cfg_path) as f:
        cfg = yaml.safe_load(f)

    routing  = cfg.get("strategy_routing", {})
    sym_cfg  = routing.get(symbol.upper(), routing.get("_default", {}))

    # Check all 5 regimes
    for regime_strats in sym_cfg.values():
        if strategy in (regime_strats or []):
            return True
    return False


# ── Sliding-window runner ─────────────────────────────────────────────────────

async def run_strategy_backtest(
    symbol:        str,
    strategy:      str,
    ohlcv_data:    dict,
    warmup_bars:   int = 210,
    eval_tf:       str = "1h",
    max_hold_bars: int = 48,
    from_date:     str = "",    # optional filter e.g. "2023-01-01"
    to_date:       str = "",    # optional filter e.g. "2024-12-31"
) -> list[SimTrade]:

    if not strategy_ever_active(symbol, strategy):
        print(f"  [SKIP] {symbol} + {strategy} — not in routing table for any regime")
        return []

    from core.strategy_router import get_active_strategies
    from core.regime_detector import RegimeDetector

    # Pre-compute all indicators ONCE before the loop
    precomp  = PrecomputedIndicators(symbol, ohlcv_data)
    detector = RegimeDetector()
    trades:  list[SimTrade] = []
    primary  = precomp.bars_1h   # always step through 1H bars

    if len(primary) < warmup_bars + 10:
        print(f"  [SKIP] {symbol}: {len(primary)} bars — need {warmup_bars + 10}")
        return []

    # Apply date filter if provided
    start_idx = warmup_bars
    end_idx   = len(primary) - max_hold_bars

    if from_date:
        from datetime import datetime, timezone
        from_ts = int(datetime.strptime(from_date, "%Y-%m-%d")
                      .replace(tzinfo=timezone.utc).timestamp() * 1000)
        for i, b in enumerate(primary):
            if b["ts"] >= from_ts:
                start_idx = max(warmup_bars, i)
                break

    if to_date:
        from datetime import datetime, timezone
        to_ts = int(datetime.strptime(to_date, "%Y-%m-%d")
                    .replace(tzinfo=timezone.utc).timestamp() * 1000)
        for i, b in enumerate(primary):
            if b["ts"] > to_ts:
                end_idx = min(end_idx, i)
                break

    total_bars   = end_idx - start_idx
    open_signals: dict[tuple, int] = {}   # (symbol, direction) -> entry bar idx

    print(f"  [{strategy.upper()}] {symbol}: scanning {total_bars} bars "
          f"({primary[start_idx]['ts']//1000} -> {primary[end_idx-1]['ts']//1000})")

    for i, cursor in enumerate(range(start_idx, end_idx)):
        # ── Progress indicator every 5% ──────────────────────────────────────
        if total_bars > 100 and i % max(1, total_bars // 20) == 0:
            pct     = i / total_bars * 100
            bar_len = 20
            filled  = int(bar_len * i // total_bars)
            bar_str = "#" * filled + "." * (bar_len - filled)
            print(f"\r    [{bar_str}] {pct:4.0f}%  {len(trades)} signals found", end="", flush=True)

        cache = FastBacktestCache(precomp, cursor)

        # Detect regime — use pre-computed ADX for speed
        try:
            regime = str(detector.detect(symbol, cache))
        except Exception:
            continue

        active = get_active_strategies(symbol, regime)
        if strategy not in active:
            continue

        # Expire open positions that have reached max_hold_bars
        expired = [k for k, v in open_signals.items() if cursor - v >= max_hold_bars]
        for k in expired:
            del open_signals[k]

        try:
            if strategy == "fvg":
                from core.fvg_scorer import score
            elif strategy == "ema_pullback":
                from core.ema_pullback_scorer import score
            elif strategy == "vwap_band":
                from core.vwap_band_scorer import score
            elif strategy == "microrange":
                from core.microrange_scorer import score
            else:
                continue

            results = await score(symbol, cache)
        except Exception:
            continue

        for result in results:
            if not result.get("fire"):
                continue

            direction = result["direction"]
            key = (symbol, direction)
            if key in open_signals:
                continue   # already in a trade

            entry = cache.get_last_price(symbol)
            if entry == 0:
                continue

            stop_key = {"fvg":"fvg_stop","ema_pullback":"ep_stop",
                        "vwap_band":"vb_stop","microrange":"mr_stop"}.get(strategy,"")
            tp_key   = {"fvg":"fvg_tp","ema_pullback":"ep_tp",
                        "vwap_band":"vb_tp","microrange":"mr_tp"}.get(strategy,"")
            stop = result.get(stop_key, 0.0)
            tp   = result.get(tp_key,   0.0)

            if not stop or not tp or abs(entry - stop) < entry * 0.001:
                continue

            trade = SimTrade(symbol=symbol, direction=direction,
                             strategy=strategy, entry=entry,
                             stop=stop, tp=tp, bar_idx=cursor)

            # Simulate on future bars
            future = primary[cursor: cursor + max_hold_bars]
            trade  = simulate_trade(trade, future, max_hold_bars)
            trades.append(trade)

            if trade.outcome == "OPEN":
                open_signals[key] = cursor

    print(f"\r    [{'#'*20}] 100%  {len(trades)} signals found")
    return trades


# ── Main entry point ──────────────────────────────────────────────────────────

async def main():
    import argparse

    parser = argparse.ArgumentParser(description="Confluence Bot Backtest")
    parser.add_argument("--from-date", default="2023-01-01",
                        help="Start date YYYY-MM-DD (default 2023-01-01)")
    parser.add_argument("--to-date",   default="2026-04-01",
                        help="End date YYYY-MM-DD (default 2026-04-01)")
    parser.add_argument("--symbol",    default="ALL")
    parser.add_argument("--strategy",  default="fvg",
                        choices=["fvg", "ema_pullback", "vwap_band", "microrange", "all"])
    parser.add_argument("--data-dir",  default=os.path.join(os.path.dirname(__file__), "..", "backtest", "data"))
    args = parser.parse_args()

    import yaml
    cfg_path = os.path.join(os.path.dirname(__file__), "..", "config.yaml")
    with open(cfg_path) as f:
        cfg = yaml.safe_load(f)
    symbols = [args.symbol] if args.symbol != "ALL" else cfg.get("symbols", [])

    strategies = (
        ["fvg", "ema_pullback", "vwap_band", "microrange"]
        if args.strategy == "all"
        else [args.strategy]
    )

    # Load data
    all_data: dict = {}
    for sym in symbols:
        fpath = os.path.join(args.data_dir, f"{sym}_all.json")
        if not os.path.exists(fpath):
            print(f"[MISSING] {fpath} — run: python backtest/download_data.py")
            continue
        with open(fpath) as f:
            all_data.update(json.load(f))

    if not all_data:
        print("No data loaded. Run: python backtest/download_data.py")
        return

    print(f"\n{'='*60}")
    print(f"BACKTEST: {', '.join(strategies)} | {', '.join(symbols)}")
    print(f"PERIOD:   {args.from_date} -> {args.to_date}")
    print(f"{'='*60}\n")

    async def run_one_symbol(sym: str) -> tuple[list[dict], list[SimTrade]]:
        """Run all strategies for one symbol. Returns (result dicts, trades)."""
        sym_results: list[dict]     = []
        sym_trades:  list[SimTrade] = []

        for strategy in strategies:
            if not strategy_ever_active(sym, strategy):
                continue

            trades = await run_strategy_backtest(
                sym, strategy, all_data,
                from_date=args.from_date,
                to_date=args.to_date,
            )
            sym_trades.extend(trades)
            stats = compute_stats(trades)
            sym_results.append({"symbol": sym, "strategy": strategy, **stats})

        return sym_results, sym_trades

    # Run all symbols concurrently
    print(f"\nRunning {len(symbols)} symbols in parallel...\n")
    tasks   = [run_one_symbol(sym) for sym in symbols]
    outputs = await asyncio.gather(*tasks)

    all_trades:    list[SimTrade] = []
    results_table: list[dict]    = []
    for sym_results, sym_trades in outputs:
        results_table.extend(sym_results)
        all_trades.extend(sym_trades)

    # Print summary table sorted by PF descending
    print(f"\n{'='*65}")
    print(f"{'SYMBOL':<10} {'STRATEGY':<16} {'TRADES':>6} {'WR':>6} {'PF':>6} {'PASS'}")
    print(f"{'='*65}")
    for r in sorted(results_table, key=lambda x: x["pf"], reverse=True):
        flag = "PASS" if r["pf"] >= 1.50 else "WARN" if r["pf"] >= 1.20 else "FAIL"
        print(f"{r['symbol']:<10} {r['strategy']:<16} {r['trades']:>6} "
              f"{r['wr']:>5.1f}% {r['pf']:>6.2f}  {flag}")
    print(f"{'='*65}")

    if all_trades:
        total = compute_stats(all_trades)
        print(f"\nOVERALL: {total['trades']} trades | "
              f"WR {total['wr']}% | PF {total['pf']} | AvgR {total['avg_r']:+.3f}\n")


if __name__ == "__main__":
    asyncio.run(main())
