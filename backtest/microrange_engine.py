"""Micro-range flip backtest engine — bar-by-bar replay on 5m OHLCV history.

Key design choices
------------------
- Runs per-symbol independently (no BTC anchor needed — this is a mean-reversion
  strategy, not a trend-follow / lead-lag strategy).
- Box is detected from bars[i - window_bars - 1 : i] (closed bars only, no look-ahead).
- Entry: market order proxy at current bar's close.
- SL/TP: boundary-anchored (range_low/high), not entry-relative.
- Max hold: force-close at bar close after max_hold_bars.
- Per-symbol cooldown: skip entries for cooldown_bars after any trade closes.

Position sizing
---------------
    risk_amount = equity × risk_pct
    pnl_win     = risk_amount × (tp_dist / sl_dist)   — variable RR from box geometry
    pnl_loss    = -risk_amount
    pnl_timeout = risk_amount × (pct_move / sl_pct)   — scaled to stop distance
"""
import logging
import os
import yaml

log = logging.getLogger(__name__)

_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "config.yaml")
with open(_CONFIG_PATH) as _f:
    _cfg = yaml.safe_load(_f)

_MR = _cfg.get("microrange", {})

# Default parameters (overridden by tuner via module-level attribute patching)
_WINDOW_BARS    = int(_MR.get("window_bars",      10))
_RANGE_MAX_PCT  = float(_MR.get("range_max_pct",   0.010))
_ENTRY_ZONE_PCT = float(_MR.get("entry_zone_pct",  0.002))
_STOP_PCT       = float(_MR.get("stop_pct",        0.003))
_TP_RATIO       = float(_MR.get("tp_ratio",        0.75))
_MAX_VOL_RATIO  = float(_MR.get("max_vol_ratio",   1.3))
_RSI_LONG_MAX   = float(_MR.get("rsi_long_max",    40.0))
_RSI_SHORT_MIN  = float(_MR.get("rsi_short_min",   60.0))
_COOLDOWN_BARS  = int(_MR.get("cooldown_mins",     20) // 5)   # 20 min → 4 × 5m bars
_MAX_HOLD       = int(_MR.get("max_hold_bars",      6))
_USE_RSI_FILTER = bool(_MR.get("use_rsi_filter",   True))
_THRESHOLD      = float(_MR.get("fire_threshold",   0.75))
_WARMUP_BARS = _WINDOW_BARS + 22   # need vol MA(20) + window + 1


def _ts_str(ts_ms: int) -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


# ── Exit checker ──────────────────────────────────────────────────────────────

def _check_exit(trade: dict, bar: dict) -> dict | None:
    """Return closed trade dict on SL/TP hit within this bar, else None."""
    risk = trade["risk_amount"]
    sl   = trade["sl"]
    tp   = trade["tp"]

    if trade["direction"] == "LONG":
        # SL check first (conservative — assume worst case intrabar)
        if bar["l"] <= sl:
            return {**trade, "exit_ts": bar["ts"], "outcome": "LOSS",
                    "pnl": round(-risk, 4)}
        if bar["h"] >= tp:
            return {**trade, "exit_ts": bar["ts"], "outcome": "WIN",
                    "pnl": round(risk * trade["rr"], 4)}
    else:
        if bar["h"] >= sl:
            return {**trade, "exit_ts": bar["ts"], "outcome": "LOSS",
                    "pnl": round(-risk, 4)}
        if bar["l"] <= tp:
            return {**trade, "exit_ts": bar["ts"], "outcome": "WIN",
                    "pnl": round(risk * trade["rr"], 4)}
    return None


def _force_close(trade: dict, bar: dict) -> dict:
    """Close at bar close after max-hold timeout."""
    entry = trade["entry"]
    close = bar["c"]
    risk  = trade["risk_amount"]
    sl    = trade["sl"]

    sl_dist = abs(entry - sl)
    if sl_dist <= 0.0:
        return {**trade, "exit_ts": bar["ts"], "outcome": "TIMEOUT", "pnl": 0.0}

    if trade["direction"] == "LONG":
        pct_move = (close - entry) / entry
    else:
        pct_move = (entry - close) / entry

    pnl = pct_move / (sl_dist / entry) * risk
    pnl = max(pnl, -risk)   # cap at full risk_amount loss

    return {**trade, "exit_ts": bar["ts"], "outcome": "TIMEOUT", "pnl": round(pnl, 4)}


# ── Main backtest runner ───────────────────────────────────────────────────────

def run(
    symbols:          list[str],
    ohlcv:            dict[str, list[dict]],
    starting_capital: float = 100.0,
    risk_pct:         float = 0.01,
) -> list[dict]:
    """Run the micro-range backtest.  Returns a list of closed trade dicts.

    Parameters
    ----------
    symbols          : list of symbols to trade (no BTC anchor needed)
    ohlcv            : {"{SYMBOL}:5m": [bar, ...]} from fetcher
    starting_capital : initial equity in USD
    risk_pct         : fraction of equity risked per trade
    """
    from signals.microrange.detector import (
        detect_micro_range,
        near_range_low,
        near_range_high,
        low_volume,
        rsi_supports_long,
        rsi_supports_short,
        compute_levels,
    )

    all_closed: list[dict] = []

    for sym in symbols:
        bars = ohlcv.get(f"{sym}:5m", [])
        if len(bars) < _WARMUP_BARS + 10:
            log.warning("MicroRange: insufficient 5m data for %s (%d bars)", sym, len(bars))
            continue

        # Each symbol gets its own isolated equity — prevents compounding across symbols
        equity = starting_capital

        eval_bars = bars[_WARMUP_BARS:]
        log.info(
            "MicroRange backtest %s: %d bars (%s → %s)",
            sym, len(eval_bars),
            _ts_str(eval_bars[0]["ts"]),
            _ts_str(eval_bars[-1]["ts"]),
        )

        open_trade:     dict | None = None
        cooldown_until: int         = 0   # bar index

        for bar_idx, bar in enumerate(eval_bars):
            global_idx = _WARMUP_BARS + bar_idx   # index into full `bars` list

            # ── 1. Resolve open trade ─────────────────────────────────────────
            if open_trade is not None:
                result = _check_exit(open_trade, bar)
                if result is not None:
                    equity += result["pnl"]
                    result["equity_after"] = round(equity, 4)
                    all_closed.append(result)
                    cooldown_until = bar_idx + _COOLDOWN_BARS
                    open_trade = None
                elif bar_idx - open_trade["bar_idx"] >= _MAX_HOLD:
                    result = _force_close(open_trade, bar)
                    equity += result["pnl"]
                    result["equity_after"] = round(equity, 4)
                    all_closed.append(result)
                    cooldown_until = bar_idx + _COOLDOWN_BARS
                    open_trade = None
                # still open — continue to next bar
                continue

            # ── 2. Skip if on cooldown ─────────────────────────────────────────
            if bar_idx < cooldown_until:
                continue

            # ── 3. Detect box on completed bars (no look-ahead) ───────────────
            window_slice = bars[max(0, global_idx - _WINDOW_BARS - 2) : global_idx]
            box = detect_micro_range(window_slice, _WINDOW_BARS, _RANGE_MAX_PCT)
            if box is None:
                continue

            price  = bar["c"]
            closes = [b["c"] for b in bars[max(0, global_idx - 20) : global_idx + 1]]

            # ── 4. Volume filter ──────────────────────────────────────────────
            vol_slice = bars[max(0, global_idx - 21) : global_idx + 1]
            if not low_volume(vol_slice, _MAX_VOL_RATIO):
                continue

            # ── 5. Entry zone check — prefer LONG (range_low) over SHORT ──────
            direction = None
            if near_range_low(price, box["range_low"], _ENTRY_ZONE_PCT):
                if not _USE_RSI_FILTER or rsi_supports_long(closes, _RSI_LONG_MAX):
                    direction = "LONG"
            elif near_range_high(price, box["range_high"], _ENTRY_ZONE_PCT):
                if not _USE_RSI_FILTER or rsi_supports_short(closes, _RSI_SHORT_MIN):
                    direction = "SHORT"

            if direction is None:
                continue

            # ── 6. Compute boundary-anchored SL/TP ────────────────────────────
            sl, tp = compute_levels(
                direction,
                box["range_low"], box["range_high"], box["range_width"],
                _STOP_PCT, _TP_RATIO,
            )

            # Compute actual RR from box geometry
            entry    = price
            tp_dist  = abs(tp - entry)
            sl_dist  = abs(sl - entry)
            rr       = (tp_dist / sl_dist) if sl_dist > 0.0 else 2.5

            risk_amount = round(equity * risk_pct, 4)

            open_trade = {
                "symbol":          sym,
                "regime":          "MICRORANGE",
                "direction":       direction,
                "entry":           entry,
                "sl":              sl,
                "tp":              tp,
                "rr":              round(rr, 3),
                "entry_ts":        bar["ts"],
                "bar_idx":         bar_idx,
                "risk_amount":     risk_amount,
                "score":           _THRESHOLD,   # all 3 required signals fired
                "range_width_pct": round(box["range_width_pct"] * 100, 4),
                "signals": {
                    "box_detected": True,
                    "entry_zone":   True,
                    "volume_ok":    True,
                    "rsi_aligned":  True,
                },
            }

        # Force-close any open trade at end of data
        if open_trade is not None:
            result = _force_close(open_trade, eval_bars[-1])
            equity += result["pnl"]
            result["equity_after"] = round(equity, 4)
            all_closed.append(result)

    total_pnl = sum(t["pnl"] for t in all_closed)
    avg_return = (total_pnl / len(symbols)) / starting_capital * 100 if symbols else 0
    log.info(
        "MicroRange backtest complete: %d trades  total_pnl=$%.2f  avg_return_per_symbol=%.1f%%",
        len(all_closed), total_pnl, avg_return,
    )
    return all_closed
