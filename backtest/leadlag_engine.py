"""Lead-lag backtest engine — replays 5m history through the BTC VWAP breakout strategy.

Key differences from the main (1h) engine
------------------------------------------
- Runs bar-by-bar on 5m BTCUSDT candles as the anchor
- All alt candles are looked up by matching timestamp
- SL / TP are fixed-percentage (not ATR-based)
- Max hold = 6 bars (30 min); force-close at current bar's close
- Per-symbol cooldown mirrors live (30 min = 6 bars)
- Simultaneous entries capped at max_alts_per_signal

Position sizing
---------------
    risk_amount = equity × risk_pct
    qty         = risk_amount / (entry × stop_pct)   [not used in PnL calc]
    pnl_win     = risk_amount × (tp_pct / stop_pct)  = risk_amount × 2.5
    pnl_loss    = -risk_amount
"""
import logging
import os
import time
import yaml

log = logging.getLogger(__name__)

_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "config.yaml")
with open(_CONFIG_PATH) as _f:
    _cfg = yaml.safe_load(_f)

_LL = _cfg.get("leadlag", {})

# Strategy parameters (read from config, with safe defaults)
_VWAP_WINDOW   = int(_LL.get("vwap_window_bars",    12))
_MIN_BREAK     = float(_LL.get("min_vwap_break_pct", 0.003))
_VOL_MULT      = float(_LL.get("vol_spike_mult",      1.5))
_MAX_PREMOVE   = float(_LL.get("max_alt_premove_pct", 0.003))
_COOLDOWN_BARS = int(_LL.get("cooldown_mins",  30) // 5)   # 30 min → 6 × 5m bars
_MAX_ALTS      = int(_LL.get("max_alts_per_signal",    3))
_STOP_PCT      = float(_LL.get("stop_pct",   0.0020))
_TP_PCT        = float(_LL.get("tp_pct",     0.0050))
_RR            = _TP_PCT / _STOP_PCT                       # 2.5
_MAX_HOLD      = int(_LL.get("max_hold_bars",     6))      # bars (= 30 min at 5m)
_WARMUP_BARS   = max(_VWAP_WINDOW + 2, 22)                 # need vol_ma(20) + VWAP window

# Optional filters (also settable by tuner via module attribute patching)
_TREND_FILTER  = bool(_LL.get("require_trend_aligned", False))  # BTC EMA alignment
_HOUR_START    = int(_LL.get("hour_start_utc",  0))             # 0 = any hour
_HOUR_END      = int(_LL.get("hour_end_utc",   24))


def _ts_str(ts_ms: int) -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


# ── VWAP helper ───────────────────────────────────────────────────────────────

def _vwap(bars: list[dict]) -> float:
    num = sum((b["h"] + b["l"] + b["c"]) / 3.0 * b["v"] for b in bars)
    den = sum(b["v"] for b in bars)
    return num / den if den > 0.0 else 0.0


def _vol_ma(bars: list[dict], n: int = 20) -> float:
    sample = bars[-n:]
    return sum(b["v"] for b in sample) / len(sample) if sample else 0.0


# ── BTC signal detection ──────────────────────────────────────────────────────

def _check_btc_breakout(btc_bars_upto_now: list[dict]) -> dict | None:
    """Identical logic to signals/leadlag/btc_momentum.py but operates on a slice."""
    if len(btc_bars_upto_now) < _VWAP_WINDOW + 2:
        return None

    anchor = btc_bars_upto_now[-(  _VWAP_WINDOW + 1):-1]
    curr   = btc_bars_upto_now[-1]
    prev   = btc_bars_upto_now[-2]

    vwap = _vwap(anchor)
    if vwap == 0.0:
        return None

    vm = _vol_ma(btc_bars_upto_now, n=20)
    if vm == 0.0 or curr["v"] / vm < _VOL_MULT:
        return None

    gap = (curr["c"] - vwap) / vwap

    if gap >= _MIN_BREAK and prev["c"] <= vwap * 1.0005:
        return {"direction": "LONG",  "vwap": vwap, "strength": min(gap / (_MIN_BREAK * 4), 1.0)}
    if -gap >= _MIN_BREAK and prev["c"] >= vwap * 0.9995:
        return {"direction": "SHORT", "vwap": vwap, "strength": min(-gap / (_MIN_BREAK * 4), 1.0)}
    return None


# ── Alt readiness ─────────────────────────────────────────────────────────────

def _alt_ready(alt_bars_upto_now: list[dict], direction: str) -> bool:
    if len(alt_bars_upto_now) < 4:
        return False
    start = alt_bars_upto_now[-4]["c"]
    curr  = alt_bars_upto_now[-1]["c"]
    if start <= 0:
        return False
    premove = (curr - start) / start
    if direction == "LONG"  and premove >= _MAX_PREMOVE:
        return False
    if direction == "SHORT" and premove <= -_MAX_PREMOVE:
        return False
    return True


# ── Exit checker ─────────────────────────────────────────────────────────────

def _check_exit(trade: dict, bar: dict) -> dict | None:
    """Return closed trade dict on SL/TP hit, else None."""
    risk = trade["risk_amount"]

    if trade["direction"] == "LONG":
        if bar["l"] <= trade["sl"]:
            return {**trade, "exit_ts": bar["ts"], "outcome": "LOSS",    "pnl": round(-risk, 4)}
        if bar["h"] >= trade["tp"]:
            return {**trade, "exit_ts": bar["ts"], "outcome": "WIN",     "pnl": round(risk * _RR, 4)}
    else:
        if bar["h"] >= trade["sl"]:
            return {**trade, "exit_ts": bar["ts"], "outcome": "LOSS",    "pnl": round(-risk, 4)}
        if bar["l"] <= trade["tp"]:
            return {**trade, "exit_ts": bar["ts"], "outcome": "WIN",     "pnl": round(risk * _RR, 4)}
    return None


def _force_close(trade: dict, bar: dict) -> dict:
    """Close at bar close after max-hold timeout."""
    entry = trade["entry"]
    close = bar["c"]
    risk  = trade["risk_amount"]

    if trade["direction"] == "LONG":
        pct_move = (close - entry) / entry
    else:
        pct_move = (entry - close) / entry

    # Scale PnL: each 1× stop-distance of move = 1× risk
    pnl = pct_move / _STOP_PCT * risk
    pnl = max(pnl, -risk)   # cap loss at risk_amount (no gap-through beyond SL)

    return {**trade, "exit_ts": bar["ts"], "outcome": "TIMEOUT", "pnl": round(pnl, 4)}


# ── Main backtest runner ──────────────────────────────────────────────────────

def run(
    symbols:          list[str],
    ohlcv:            dict[str, list[dict]],
    starting_capital: float = 100.0,
    risk_pct:         float = 0.02,
) -> list[dict]:
    """
    Run the lead-lag backtest.  Returns a list of closed trade dicts.

    Parameters
    ----------
    symbols          : all configured symbols; BTCUSDT used as trigger anchor
    ohlcv            : {"{SYMBOL}:{tf}": [bar, ...]} from fetcher
    starting_capital : initial equity in USD
    risk_pct         : fraction of equity risked per trade
    """
    btc_5m = ohlcv.get("BTCUSDT:5m", [])
    if len(btc_5m) < _WARMUP_BARS + 10:
        log.error("Insufficient BTCUSDT 5m data (need >%d bars, got %d)", _WARMUP_BARS, len(btc_5m))
        return []

    alts = [s for s in symbols if s != "BTCUSDT"]

    # Build per-symbol lookup: ts_ms → bar index for O(1) access
    alt_index: dict[str, dict[int, int]] = {}
    alt_bars_list: dict[str, list[dict]] = {}
    for sym in alts:
        bars = ohlcv.get(f"{sym}:5m", [])
        alt_bars_list[sym] = bars
        alt_index[sym] = {b["ts"]: i for i, b in enumerate(bars)}

    eval_bars = btc_5m[_WARMUP_BARS:]
    log.info(
        "LeadLag backtest: %d BTC 5m bars (%s → %s)  capital=$%.2f  risk=%.0f%%",
        len(eval_bars),
        _ts_str(eval_bars[0]["ts"]),
        _ts_str(eval_bars[-1]["ts"]),
        starting_capital, risk_pct * 100,
    )

    equity        = starting_capital
    open_trades:   list[dict] = []
    closed_trades: list[dict] = []
    # Per-symbol cooldown: bar index when cooldown expires
    cooldown_until: dict[str, int] = {s: 0 for s in alts}

    for bar_idx, btc_bar in enumerate(eval_bars):
        btc_ts      = btc_bar["ts"]
        global_idx  = _WARMUP_BARS + bar_idx   # index into full btc_5m list

        # ── 1. Resolve open trades on every alt 5m bar ───────────────────────
        still_open: list[dict] = []
        for trade in open_trades:
            sym     = trade["symbol"]
            sym_bar = _get_alt_bar_at(alt_bars_list[sym], alt_index[sym], btc_ts)
            if sym_bar is None:
                still_open.append(trade)
                continue

            result = _check_exit(trade, sym_bar)
            if result is not None:
                equity += result["pnl"]
                result["equity_after"] = round(equity, 4)
                closed_trades.append(result)
            elif bar_idx - trade["bar_idx"] >= _MAX_HOLD:
                result = _force_close(trade, sym_bar)
                equity += result["pnl"]
                result["equity_after"] = round(equity, 4)
                closed_trades.append(result)
            else:
                still_open.append(trade)

        open_trades = still_open

        if bar_idx % 2000 == 0 and bar_idx > 0:
            log.info("  %d / %d bars  open=%d  closed=%d  equity=$%.2f",
                     bar_idx, len(eval_bars), len(open_trades), len(closed_trades), equity)

        # ── 2. Hour-of-day filter ────────────────────────────────────────────
        if _HOUR_START < _HOUR_END:   # normal range e.g. 8-20
            bar_hour = (btc_ts // 3_600_000) % 24
            if not (_HOUR_START <= bar_hour < _HOUR_END):
                continue

        # ── 3. Check BTC for a breakout ──────────────────────────────────────
        btc_slice = btc_5m[max(0, global_idx - _VWAP_WINDOW - 1) : global_idx + 1]
        btc_info  = _check_btc_breakout(btc_slice)
        if btc_info is None:
            continue

        direction = btc_info["direction"]

        # ── 4. Optional BTC trend-alignment filter ────────────────────────────
        # Only take LONG when BTC is in a short-term uptrend (above EMA20 on 1h)
        # Only take SHORT when BTC is in a short-term downtrend (below EMA20 on 1h)
        if _TREND_FILTER:
            btc_1h = ohlcv.get("BTCUSDT:1h", [])
            # Find most recent 1h bar at or before btc_ts
            h1_idx = next(
                (i for i in range(len(btc_1h) - 1, -1, -1) if btc_1h[i]["ts"] <= btc_ts),
                None,
            )
            if h1_idx is not None and h1_idx >= 20:
                closes_1h = [b["c"] for b in btc_1h[h1_idx - 19 : h1_idx + 1]]
                ema20 = sum(closes_1h[:20]) / 20   # seed
                k = 2.0 / 21.0
                for c in closes_1h[1:]:
                    ema20 = c * k + ema20 * (1.0 - k)
                btc_close_1h = btc_1h[h1_idx]["c"]
                if direction == "LONG"  and btc_close_1h < ema20:
                    continue
                if direction == "SHORT" and btc_close_1h > ema20:
                    continue

        # ── 3. Score alts and enter up to max_alts ────────────────────────────
        fired = 0
        active_syms = {t["symbol"] for t in open_trades}

        for sym in alts:
            if fired >= _MAX_ALTS:
                break
            if sym in active_syms:
                continue                               # already in a position
            if bar_idx < cooldown_until.get(sym, 0):
                continue                               # cooldown active

            sym_bars_all = alt_bars_list[sym]
            sym_idx      = alt_index[sym].get(btc_ts)
            if sym_idx is None or sym_idx < 4:
                continue

            alt_slice = sym_bars_all[max(0, sym_idx - 3) : sym_idx + 1]
            if not _alt_ready(alt_slice, direction):
                continue

            # ── Entry ────────────────────────────────────────────────────────
            entry_bar = sym_bars_all[sym_idx]
            entry     = entry_bar["c"]   # market order proxy: current close

            if direction == "LONG":
                sl = round(entry * (1.0 - _STOP_PCT), 8)
                tp = round(entry * (1.0 + _TP_PCT),   8)
            else:
                sl = round(entry * (1.0 + _STOP_PCT), 8)
                tp = round(entry * (1.0 - _TP_PCT),   8)

            risk_amount = round(equity * risk_pct, 4)

            open_trades.append({
                "symbol":      sym,
                "regime":      "LEADLAG",
                "direction":   direction,
                "entry":       entry,
                "sl":          sl,
                "tp":          tp,
                "entry_ts":    btc_ts,
                "bar_idx":     bar_idx,
                "risk_amount": risk_amount,
                "score":       round(0.75 + btc_info["strength"] * 0.10, 3),
                "signals": {
                    "btc_vwap_break":   True,
                    "vol_spike":        True,
                    "alt_not_premoved": True,
                    "cooldown_ok":      True,
                },
            })

            cooldown_until[sym] = bar_idx + _COOLDOWN_BARS
            fired += 1

    # Force-close any still-open trades at end of data
    last_btc = eval_bars[-1]
    for trade in open_trades:
        sym_bar = _get_alt_bar_at(
            alt_bars_list[trade["symbol"]],
            alt_index[trade["symbol"]],
            last_btc["ts"],
        ) or last_btc
        result = _force_close(trade, sym_bar)
        equity += result["pnl"]
        result["equity_after"] = round(equity, 4)
        closed_trades.append(result)

    log.info(
        "LeadLag backtest complete: %d trades  final equity=$%.2f  return=%.1f%%",
        len(closed_trades), equity,
        (equity - starting_capital) / starting_capital * 100,
    )
    return closed_trades


def _get_alt_bar_at(
    bars: list[dict],
    index: dict[int, int],
    ts_ms: int,
) -> dict | None:
    """Return the alt bar whose open timestamp matches ts_ms, or None."""
    idx = index.get(ts_ms)
    if idx is None:
        return None
    return bars[idx]
