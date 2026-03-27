"""1H inside bar flip backtest engine — bar-by-bar replay on 1H OHLCV history.

For each symbol
---------------
1. Iterate 1H bars.
2. At each bar, detect compression zone using all completed prior bars.
3. If price (bar close) is near zone boundary: enter the fade.
4. Manage the open trade bar-by-bar: SL/TP hit or max_hold timeout.
5. Per-symbol cooldown: skip entries for cooldown_bars after each trade.

Entries are taken at bar close (market order proxy — no look-ahead).
"""
import logging
import os
import yaml
from datetime import datetime, timezone

log = logging.getLogger(__name__)

_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "config.yaml")
with open(_CONFIG_PATH) as _f:
    _cfg = yaml.safe_load(_f)

_IB = _cfg.get("insidebar", {})

# Patchable by tuner
_MIN_INSIDE     = int(_IB.get("min_inside_bars",  3))
_MAX_ZONE_PCT   = float(_IB.get("max_zone_pct",   0.010))   # tightened from 1.5% → 1.0%
_NEAR_POC_PCT   = float(_IB.get("near_poc_pct",   0.005))   # within 0.5% of POC
_ENTRY_ZONE_PCT = float(_IB.get("entry_zone_pct", 0.002))
_SL_BUFFER_PCT  = float(_IB.get("sl_buffer_pct",  0.002))
_RR_RATIO       = float(_IB.get("rr_ratio",        1.5))
_MAX_HOLD       = int(_IB.get("max_hold_bars",      6))    # 6 × 1H = 6h
_COOLDOWN_BARS  = int(_IB.get("cooldown_mins",      60) // 60)   # hours
_WARMUP_BARS    = max(_MIN_INSIDE + 4, 8)


def _ts_str(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


def _check_exit(trade: dict, bar: dict) -> dict | None:
    risk = trade["risk_amount"]
    if trade["direction"] == "LONG":
        if bar["l"] <= trade["sl"]:
            return {**trade, "exit_ts": bar["ts"], "outcome": "LOSS",
                    "pnl": round(-risk, 4)}
        if bar["h"] >= trade["tp"]:
            return {**trade, "exit_ts": bar["ts"], "outcome": "WIN",
                    "pnl": round(risk * _RR_RATIO, 4)}
    else:
        if bar["h"] >= trade["sl"]:
            return {**trade, "exit_ts": bar["ts"], "outcome": "LOSS",
                    "pnl": round(-risk, 4)}
        if bar["l"] <= trade["tp"]:
            return {**trade, "exit_ts": bar["ts"], "outcome": "WIN",
                    "pnl": round(risk * _RR_RATIO, 4)}
    return None


def _force_close(trade: dict, bar: dict) -> dict:
    entry   = trade["entry"]
    close   = bar["c"]
    risk    = trade["risk_amount"]
    sl_dist = abs(entry - trade["sl"])
    if sl_dist <= 0.0:
        return {**trade, "exit_ts": bar["ts"], "outcome": "TIMEOUT", "pnl": 0.0}
    if trade["direction"] == "LONG":
        pct_move = (close - entry) / entry
    else:
        pct_move = (entry - close) / entry
    pnl = pct_move / (sl_dist / entry) * risk
    pnl = max(pnl, -risk)
    return {**trade, "exit_ts": bar["ts"], "outcome": "TIMEOUT", "pnl": round(pnl, 4)}


def run(
    symbols:          list[str],
    ohlcv:            dict[str, list[dict]],
    starting_capital: float = 100.0,
    risk_pct:         float = 0.01,
) -> list[dict]:
    """Run the inside bar flip backtest.  Returns closed trade list."""
    from signals.insidebar.detector import (
        detect_compression,
        near_zone_low,
        near_zone_high,
        compute_levels,
    )

    all_closed: list[dict] = []
    equity = starting_capital

    for sym in symbols:
        bars = ohlcv.get(f"{sym}:1h", [])
        if len(bars) < _WARMUP_BARS + 10:
            log.warning("InsideBar: insufficient 1h data for %s (%d bars)", sym, len(bars))
            continue

        eval_bars = bars[_WARMUP_BARS:]
        log.info("InsideBar backtest %s: %d bars (%s → %s)",
                 sym, len(eval_bars),
                 _ts_str(eval_bars[0]["ts"]), _ts_str(eval_bars[-1]["ts"]))

        open_trade:     dict | None = None
        cooldown_until: int         = 0

        for bar_idx, bar in enumerate(eval_bars):
            global_idx = _WARMUP_BARS + bar_idx

            # ── 1. Resolve open trade ──────────────────────────────────────────
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
                else:
                    continue

            # ── 2. Cooldown ────────────────────────────────────────────────────
            if bar_idx < cooldown_until:
                continue

            # ── 3. Detect compression zone (completed bars, no look-ahead) ─────
            window_slice = bars[max(0, global_idx - 10) : global_idx + 1]
            zone = detect_compression(window_slice, min_inside=_MIN_INSIDE)
            if zone is None:
                continue
            if zone["zone_pct"] > _MAX_ZONE_PCT:
                continue

            # Hard gate: volume must be declining into the compression
            n_inside = zone["bar_count"]
            inside_vols = [b.get("v", 0) for b in window_slice[-(n_inside + 1):-1]]
            vol_before  = window_slice[-(n_inside + 2)].get("v", 0) if len(window_slice) >= n_inside + 3 else 0
            vol_declining = (inside_vols and vol_before > 0
                             and sum(inside_vols) / len(inside_vols) < vol_before)
            if not vol_declining:
                continue

            price = bar["c"]

            # Hard gate: price must be within near_poc_pct of zone POC
            poc = zone["poc"]
            poc_dist = abs(price - poc) / poc if poc > 0 else 1.0
            if poc_dist > _NEAR_POC_PCT:
                continue

            direction = None

            if near_zone_low(price, zone["zone_low"], _ENTRY_ZONE_PCT):
                direction = "LONG"
            elif near_zone_high(price, zone["zone_high"], _ENTRY_ZONE_PCT):
                direction = "SHORT"

            if direction is None:
                continue

            # ── 4. Entry ───────────────────────────────────────────────────────
            sl, tp = compute_levels(direction, zone["zone_low"], zone["zone_high"],
                                    _SL_BUFFER_PCT, _RR_RATIO, price)

            risk_amount = round(equity * risk_pct, 4)
            open_trade  = {
                "symbol":    sym,
                "regime":    "INSIDEBAR",
                "direction": direction,
                "entry":     price,
                "sl":        sl,
                "tp":        tp,
                "entry_ts":  bar["ts"],
                "bar_idx":   bar_idx,
                "risk_amount": risk_amount,
                "zone_pct":  round(zone["zone_pct"] * 100, 3),
                "bar_count": zone["bar_count"],
                "score":     0.75,
                "signals": {
                    "compression_ok": True,
                    "entry_zone":     True,
                    "zone_tight":     True,
                },
            }

        if open_trade is not None:
            result = _force_close(open_trade, eval_bars[-1])
            equity += result["pnl"]
            result["equity_after"] = round(equity, 4)
            all_closed.append(result)

    log.info(
        "InsideBar backtest complete: %d trades  final=$%.2f  return=%.1f%%",
        len(all_closed), equity,
        (equity - starting_capital) / starting_capital * 100,
    )
    return all_closed
