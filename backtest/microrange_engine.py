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
import bisect
import logging
import os
import yaml

from backtest.cost_model import apply_costs
from backtest.regime_classifier import classify_regime
from signals.volume_momentum import get_volume_params_static

_BAR_MINUTES = 5

log = logging.getLogger(__name__)

_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "config.yaml")
with open(_CONFIG_PATH) as _f:
    _cfg = yaml.safe_load(_f)

_MR = _cfg.get("microrange", {})

_WINDOW_BARS    = int(_MR.get("window_bars",    10))
_COOLDOWN_BARS  = int(_MR.get("cooldown_mins",  20) // 5)   # 20 min → 4 × 5m bars
_USE_RSI_FILTER = bool(_MR.get("use_rsi_filter", True))
_WARMUP_BARS    = _WINDOW_BARS + 22   # need vol MA(20) + window + 1


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


# ── Dynamic param helper ───────────────────────────────────────────────────────

def _get_dynamic_params(sym: str, bars_5m: list[dict], bar_idx: int) -> dict:
    """Calculate ATR-based params at a specific point in the backtest."""
    from core.symbol_config import get_symbol_config, _calc_atr, get_symbol_tier

    base = get_symbol_config(sym, "microrange")
    tier = get_symbol_tier(sym)

    window = bars_5m[max(0, bar_idx - 19) : bar_idx + 1]
    if len(window) < 15:
        return base

    atr   = _calc_atr(window, period=14)
    price = window[-1]["c"]
    if atr <= 0 or price <= 0:
        return base

    atr_pct = atr / price

    from core.symbol_config import _microrange_dynamic
    return _microrange_dynamic(base, tier, atr_pct)


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
        bars    = ohlcv.get(f"{sym}:5m", [])
        bars_4h = ohlcv.get(f"{sym}:4h", [])
        bars_1d = ohlcv.get(f"{sym}:1d", [])
        _ts_4h  = [b["ts"] for b in bars_4h]
        _ts_1d  = [b["ts"] for b in bars_1d]

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

            params = _get_dynamic_params(sym, bars, global_idx)
            _RANGE_MAX_PCT  = float(params["range_max_pct"])
            _ENTRY_ZONE_PCT = float(params["entry_zone_pct"])
            _STOP_PCT       = float(params["stop_pct"])
            _TP_RATIO       = float(params["tp_ratio"])
            _MAX_VOL_RATIO  = float(params.get("max_vol_ratio",  1.3))
            _RSI_LONG_MAX   = float(params.get("rsi_long_max",   40.0))
            _RSI_SHORT_MIN  = float(params.get("rsi_short_min",  60.0))
            _THRESHOLD      = float(params.get("fire_threshold", 0.75))
            _MAX_HOLD       = int(params.get("max_hold_bars",    6))
            _MIN_BOX_PCT    = _STOP_PCT * 2.0

            # ── 1. Resolve open trade ─────────────────────────────────────────
            if open_trade is not None:
                result = _check_exit(open_trade, bar)
                if result is not None:
                    hold_bars = bar_idx - open_trade["bar_idx"]
                    gross_pnl = result["pnl"]
                    net_pnl, cost_d = apply_costs(gross_pnl, open_trade["entry"], open_trade["qty"], hold_bars, _BAR_MINUTES)
                    result["gross_pnl"] = gross_pnl; result["cost"] = cost_d["total"]
                    result["cost_fee"]  = cost_d["fee"]; result["cost_slip"] = cost_d["slip"]
                    result["cost_fund"] = cost_d["funding"]; result["pnl"] = net_pnl
                    equity += net_pnl
                    result["equity_after"] = round(equity, 4)
                    all_closed.append(result)
                    cooldown_until = bar_idx + _COOLDOWN_BARS
                    open_trade = None
                elif bar_idx - open_trade["bar_idx"] >= _MAX_HOLD:
                    result = _force_close(open_trade, bar)
                    hold_bars = bar_idx - open_trade["bar_idx"]
                    gross_pnl = result["pnl"]
                    net_pnl, cost_d = apply_costs(gross_pnl, open_trade["entry"], open_trade["qty"], hold_bars, _BAR_MINUTES)
                    result["gross_pnl"] = gross_pnl; result["cost"] = cost_d["total"]
                    result["cost_fee"]  = cost_d["fee"]; result["cost_slip"] = cost_d["slip"]
                    result["cost_fund"] = cost_d["funding"]; result["pnl"] = net_pnl
                    equity += net_pnl
                    result["equity_after"] = round(equity, 4)
                    all_closed.append(result)
                    cooldown_until = bar_idx + _COOLDOWN_BARS
                    open_trade = None
                # still open — continue to next bar
                continue

            # ── 2. Skip if on cooldown ─────────────────────────────────────────
            if bar_idx < cooldown_until:
                continue

            # ── Regime classification ─────────────────────────────────────────
            _b4h_i = bisect.bisect_right(_ts_4h, bar["ts"]) - 1
            _b1d_i = bisect.bisect_right(_ts_1d, bar["ts"]) - 1
            if _b4h_i >= 28:
                _c4h = bars_4h[max(0, _b4h_i - 29): _b4h_i + 1]
                _c1d = bars_1d[max(0, _b1d_i - 59): _b1d_i + 1] if _b1d_i >= 0 else []
                regime = classify_regime(
                    closes_4h=[b["c"] for b in _c4h],
                    highs_4h=[b["h"] for b in _c4h],
                    lows_4h=[b["l"] for b in _c4h],
                    closes_1d=[b["c"] for b in _c1d],
                )
            else:
                regime = "TREND"

            # Dynamic volume params for this bar's context (5m, regime-aware)
            vol_params = get_volume_params_static(sym, regime, "5m")

            # ── 3. Detect box on completed bars (no look-ahead) ───────────────
            window_slice = bars[max(0, global_idx - _WINDOW_BARS - 2) : global_idx]
            box = detect_micro_range(window_slice, _WINDOW_BARS, _RANGE_MAX_PCT)
            if box is None:
                continue

            price  = bar["c"]
            closes = [b["c"] for b in bars[max(0, global_idx - 20) : global_idx + 1]]

            # ── 4. Volume filter (dynamic quiet_mult from vol_params) ─────────
            vol_slice = bars[max(0, global_idx - 21) : global_idx + 1]
            if not low_volume(vol_slice, vol_params.quiet_mult):
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

            # ── Regime gate ───────────────────────────────────────────────────
            if regime == "CRASH" and direction == "LONG":
                continue
            if regime == "PUMP" and direction == "SHORT":
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
            qty         = round(risk_amount / sl_dist, 8) if sl_dist > 0.0 else 0.0

            open_trade = {
                "symbol":          sym,
                "regime":          regime,
                "direction":       direction,
                "entry":           entry,
                "sl":              sl,
                "tp":              tp,
                "rr":              round(rr, 3),
                "entry_ts":        bar["ts"],
                "bar_idx":         bar_idx,
                "risk_amount":     risk_amount,
                "qty":             qty,
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
            hold_bars = len(eval_bars) - 1 - open_trade["bar_idx"]
            gross_pnl = result["pnl"]
            net_pnl, cost_d = apply_costs(gross_pnl, open_trade["entry"], open_trade["qty"], hold_bars, _BAR_MINUTES)
            result["gross_pnl"] = gross_pnl; result["cost"] = cost_d["total"]
            result["cost_fee"]  = cost_d["fee"]; result["cost_slip"] = cost_d["slip"]
            result["cost_fund"] = cost_d["funding"]; result["pnl"] = net_pnl
            equity += net_pnl
            result["equity_after"] = round(equity, 4)
            all_closed.append(result)

    total_pnl = sum(t["pnl"] for t in all_closed)
    avg_return = (total_pnl / len(symbols)) / starting_capital * 100 if symbols else 0
    log.info(
        "MicroRange backtest complete: %d trades  total_pnl=$%.2f  avg_return_per_symbol=%.1f%%",
        len(all_closed), total_pnl, avg_return,
    )
    return all_closed
