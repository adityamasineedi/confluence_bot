"""Micro-range flip backtest engine — bar-by-bar replay on 5m OHLCV history.

Uses the live microrange_scorer directly via BacktestCache so the backtest always
tests the same logic as production.  Trade management (exit, force-close,
cost model) is handled here; signal detection is fully delegated to the scorer.

Entry  : close of the bar where the scorer fires
SL/TP  : boundary-anchored (from scorer: mr_stop / mr_tp)
RR     : computed from scorer's SL/TP geometry
"""
import asyncio
import concurrent.futures
import logging
import os
import yaml

from backtest.cache import BacktestCache
from backtest.cost_model import apply_costs

_BAR_MINUTES = 5

log = logging.getLogger(__name__)

_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "config.yaml")
with open(_CONFIG_PATH) as _f:
    _cfg = yaml.safe_load(_f)

_MR = _cfg.get("microrange", {})

_WINDOW_BARS   = int(_MR.get("window_bars",   12))
_COOLDOWN_BARS = int(_MR.get("cooldown_mins",  60) // 5)   # 60 min → 12 × 5m bars
_WARMUP_BARS   = _WINDOW_BARS + 22   # need vol MA(20) + window + 1


def _run_scorer(coro):
    """Run an async scorer synchronously in the backtest."""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            with concurrent.futures.ThreadPoolExecutor() as pool:
                future = pool.submit(asyncio.run, coro)
                return future.result()
        return loop.run_until_complete(coro)
    except Exception:
        return []


def _ts_str(ts_ms: int) -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


def _check_exit(trade: dict, bar: dict) -> dict | None:
    """Return closed trade dict on SL/TP hit within this bar, else None."""
    risk = trade["risk_amount"]
    sl   = trade["sl"]
    tp   = trade["tp"]

    if trade["direction"] == "LONG":
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
    pnl = max(pnl, -risk)

    return {**trade, "exit_ts": bar["ts"], "outcome": "TIMEOUT", "pnl": round(pnl, 4)}


def run(
    symbols:          list[str],
    ohlcv:            dict[str, list[dict]],
    starting_capital: float = 100.0,
    risk_pct:         float = 0.01,
) -> list[dict]:
    """Run the micro-range backtest.  Returns a list of closed trade dicts."""
    from core.microrange_scorer import score as mr_score, clear_cooldown as mr_clear_cooldown

    all_closed: list[dict] = []

    for sym in symbols:
        bars = ohlcv.get(f"{sym}:5m", [])
        if len(bars) < _WARMUP_BARS + 10:
            log.warning("MicroRange: insufficient 5m data for %s (%d bars)", sym, len(bars))
            continue

        bt_cache = BacktestCache(ohlcv=ohlcv, oi={}, funding={})
        mr_clear_cooldown(sym)   # prevent stale DB cooldown from blocking backtest

        equity = starting_capital

        eval_bars = bars[_WARMUP_BARS:]
        log.info(
            "MicroRange backtest %s: %d bars (%s → %s)",
            sym, len(eval_bars),
            _ts_str(eval_bars[0]["ts"]),
            _ts_str(eval_bars[-1]["ts"]),
        )

        open_trade:     dict | None = None
        cooldown_until: int         = 0

        for bar_idx, bar in enumerate(eval_bars):
            # Derive max_hold from scorer config each bar (dynamic per symbol)
            _MAX_HOLD = int(_MR.get("max_hold_bars", 6))

            # ── 1. Resolve open trade ─────────────────────────────────────────
            if open_trade is not None:
                result = _check_exit(open_trade, bar)
                if result is not None:
                    hold_bars = bar_idx - open_trade["bar_idx"]
                    gross_pnl = result["pnl"]
                    net_pnl, cost_d = apply_costs(gross_pnl, open_trade["entry"], open_trade["qty"], hold_bars, _BAR_MINUTES, bars=eval_bars[max(0, bar_idx - 15): bar_idx + 1])
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
                    net_pnl, cost_d = apply_costs(gross_pnl, open_trade["entry"], open_trade["qty"], hold_bars, _BAR_MINUTES, bars=eval_bars[max(0, bar_idx - 15): bar_idx + 1])
                    result["gross_pnl"] = gross_pnl; result["cost"] = cost_d["total"]
                    result["cost_fee"]  = cost_d["fee"]; result["cost_slip"] = cost_d["slip"]
                    result["cost_fund"] = cost_d["funding"]; result["pnl"] = net_pnl
                    equity += net_pnl
                    result["equity_after"] = round(equity, 4)
                    all_closed.append(result)
                    cooldown_until = bar_idx + _COOLDOWN_BARS
                    open_trade = None
                continue

            # ── 2. Skip if on cooldown ─────────────────────────────────────────
            if bar_idx < cooldown_until:
                continue

            # ── 3. Score via live scorer ───────────────────────────────────────
            bt_cache.advance(bar["ts"])
            score_dicts = _run_scorer(mr_score(sym, bt_cache))

            for sd in score_dicts:
                if not sd.get("fire"):
                    continue
                entry = bar["c"]
                sl    = sd.get("mr_stop", 0.0)
                tp    = sd.get("mr_tp",   0.0)
                if sl == 0.0 or tp == 0.0:
                    continue
                sl_dist = abs(entry - sl)
                tp_dist = abs(tp - entry)
                if sl_dist <= 0.0:
                    continue
                rr          = tp_dist / sl_dist if sl_dist > 0 else 2.5
                risk_amount = round(equity * risk_pct, 4)
                qty         = round(risk_amount / sl_dist, 8)
                open_trade = {
                    "symbol":          sym,
                    "regime":          sd.get("regime", "MICRORANGE"),
                    "direction":       sd["direction"],
                    "entry":           entry,
                    "sl":              sl,
                    "tp":              tp,
                    "rr":              round(rr, 3),
                    "entry_ts":        bar["ts"],
                    "bar_idx":         bar_idx,
                    "risk_amount":     risk_amount,
                    "qty":             qty,
                    "score":           sd["score"],
                    "range_width_pct": sd.get("range_width_pct", 0),
                    "signals":         sd.get("signals", {}),
                }
                break   # one trade per bar

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
