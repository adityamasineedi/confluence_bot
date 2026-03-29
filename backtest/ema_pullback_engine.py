"""15m EMA Pullback backtest engine — trend-continuation entries at EMA21.

Uses the live ema_pullback_scorer directly via BacktestCache so the backtest
always tests the same logic as production.

Entry  : close of the bar where the scorer fires
SL/TP  : from scorer (ep_stop / ep_tp)
"""
import asyncio
import concurrent.futures
import logging
import os
import yaml

from backtest.cache import BacktestCache
from backtest.cost_model import apply_costs

_BAR_MINUTES = 15

log = logging.getLogger(__name__)

_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "config.yaml")
with open(_CONFIG_PATH) as _f:
    _cfg = yaml.safe_load(_f)

_EP = _cfg.get("ema_pullback", {})

_RR            = float(_EP.get("rr_ratio",    1.5))
_COOLDOWN_BARS = int(_EP.get("cooldown_mins", 45) // 15)   # 45 min → 3 × 15m bars
_MAX_HOLD      = int(_EP.get("max_hold_bars",  4))


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
    risk = trade["risk_amount"]
    sl, tp = trade["sl"], trade["tp"]
    if trade["direction"] == "LONG":
        if bar["l"] <= sl:
            return {**trade, "exit_ts": bar["ts"], "outcome": "LOSS", "pnl": round(-risk, 4)}
        if bar["h"] >= tp:
            return {**trade, "exit_ts": bar["ts"], "outcome": "WIN", "pnl": round(risk * _RR, 4)}
    else:
        if bar["h"] >= sl:
            return {**trade, "exit_ts": bar["ts"], "outcome": "LOSS", "pnl": round(-risk, 4)}
        if bar["l"] <= tp:
            return {**trade, "exit_ts": bar["ts"], "outcome": "WIN", "pnl": round(risk * _RR, 4)}
    return None


def _force_close(trade: dict, bar: dict) -> dict:
    entry = trade["entry"]
    close = bar["c"]
    risk  = trade["risk_amount"]
    sl    = trade["sl"]
    sl_dist = abs(entry - sl)
    if sl_dist <= 0.0:
        return {**trade, "exit_ts": bar["ts"], "outcome": "TIMEOUT", "pnl": 0.0}
    pct_move = ((close - entry) / entry) if trade["direction"] == "LONG" else ((entry - close) / entry)
    pnl = pct_move / (sl_dist / entry) * risk
    pnl = max(pnl, -risk)
    return {**trade, "exit_ts": bar["ts"], "outcome": "TIMEOUT", "pnl": round(pnl, 4)}


def run(
    symbols:          list[str],
    ohlcv:            dict[str, list[dict]],
    starting_capital: float = 100.0,
    risk_pct:         float = 0.01,
) -> list[dict]:
    """Run 15m EMA pullback backtest. Returns list of closed trades."""
    from core.ema_pullback_scorer import score as ep_score, clear_cooldown as ep_clear_cooldown

    all_closed: list[dict] = []
    warmup = 60   # EMA50 (50) + buffer — mirrors scorer's minimum bar requirement

    for sym in symbols:
        bars_15m = ohlcv.get(f"{sym}:15m", [])
        if len(bars_15m) < warmup + 10:
            log.warning("EMA Pullback: insufficient 15m data for %s (%d bars)", sym, len(bars_15m))
            continue

        bt_cache = BacktestCache(ohlcv=ohlcv, oi={}, funding={})
        ep_clear_cooldown(sym)   # prevent stale DB cooldown from blocking backtest

        equity     = starting_capital
        eval_bars  = bars_15m[warmup:]
        log.info("EMA Pullback backtest %s: %d × 15m bars (%s → %s)",
                 sym, len(eval_bars), _ts_str(eval_bars[0]["ts"]), _ts_str(eval_bars[-1]["ts"]))

        open_trade:     dict | None = None
        cooldown_until: int         = 0

        for bar_idx, bar in enumerate(eval_bars):

            # ── Manage open trade ──────────────────────────────────────────────
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

            if bar_idx < cooldown_until:
                continue

            # ── Score via live scorer ──────────────────────────────────────────
            bt_cache.advance(bar["ts"])
            score_dicts = _run_scorer(ep_score(sym, bt_cache))

            for sd in score_dicts:
                if not sd.get("fire"):
                    continue
                entry = bar["c"]
                sl    = sd.get("ep_stop", 0.0)
                tp    = sd.get("ep_tp",   0.0)
                if sl == 0.0 or tp == 0.0 or entry == 0.0:
                    continue
                sl_dist = abs(entry - sl)
                if sl_dist <= 0.0:
                    continue
                risk_amount = round(equity * risk_pct, 4)
                qty         = round(risk_amount / sl_dist, 8)
                open_trade = {
                    "symbol":      sym,
                    "regime":      sd.get("regime", "EMA_PULLBACK"),
                    "direction":   sd["direction"],
                    "entry":       entry,
                    "sl":          sl,
                    "tp":          tp,
                    "rr":          _RR,
                    "entry_ts":    bar["ts"],
                    "bar_idx":     bar_idx,
                    "risk_amount": risk_amount,
                    "qty":         qty,
                    "score":       sd["score"],
                }
                break   # one trade per bar

        if open_trade is not None:
            last_bar = eval_bars[-1]
            result = _force_close(open_trade, last_bar)
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
    avg_sym_ret = (total_pnl / starting_capital / max(len(symbols), 1)) * 100
    log.info("EMA Pullback backtest complete: %d trades  total_pnl=$%.2f  avg_return=%.1f%%",
             len(all_closed), total_pnl, avg_sym_ret)
    return all_closed
