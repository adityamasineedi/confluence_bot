"""Fair Value Gap Fill backtest engine — bar-by-bar replay on 1H OHLCV.

Uses the live fvg_scorer directly via BacktestCache so the backtest always
tests the same logic as production.  Trade management (exit, force-close,
cost model) is handled here; signal detection is fully delegated to the scorer.

Entry  : close of the bar where the scorer fires
SL/TP  : from scorer (fvg_stop / fvg_tp)
"""
import asyncio
import concurrent.futures
import logging
import os
import yaml

from backtest.cache import BacktestCache
from backtest.cost_model import apply_costs

_BAR_MINUTES = 60

log = logging.getLogger(__name__)

_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "config.yaml")
with open(_CONFIG_PATH) as _f:
    _cfg = yaml.safe_load(_f)

_FVG_CFG = _cfg.get("fvg", {})

_LOOKBACK      = int(_FVG_CFG.get("lookback_bars",  50))
_RR            = float(_FVG_CFG.get("rr_ratio",      2.0))
_COOLDOWN_BARS = int(_FVG_CFG.get("cooldown_mins",   45))   # 1 bar = 1 h
_MAX_HOLD      = 24   # 24 × 1h = 1 day


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
    if trade["direction"] == "LONG":
        if bar["l"] <= trade["sl"]:
            return {**trade, "exit_ts": bar["ts"], "outcome": "LOSS",
                    "pnl": round(-risk, 4)}
        if bar["h"] >= trade["tp"]:
            return {**trade, "exit_ts": bar["ts"], "outcome": "WIN",
                    "pnl": round(risk * _RR, 4)}
    else:
        if bar["h"] >= trade["sl"]:
            return {**trade, "exit_ts": bar["ts"], "outcome": "LOSS",
                    "pnl": round(-risk, 4)}
        if bar["l"] <= trade["tp"]:
            return {**trade, "exit_ts": bar["ts"], "outcome": "WIN",
                    "pnl": round(risk * _RR, 4)}
    return None


def _force_close(trade: dict, bar: dict) -> dict:
    entry = trade["entry"]; close = bar["c"]; risk = trade["risk_amount"]
    sl_dist = abs(entry - trade["sl"])
    if sl_dist <= 0:
        return {**trade, "exit_ts": bar["ts"], "outcome": "TIMEOUT", "pnl": 0.0}
    pct = ((close - entry) / entry) if trade["direction"] == "LONG" else ((entry - close) / entry)
    pnl = max(pct / (sl_dist / entry) * risk, -risk)
    return {**trade, "exit_ts": bar["ts"], "outcome": "TIMEOUT", "pnl": round(pnl, 4)}


def run(
    symbols:          list[str],
    ohlcv:            dict[str, list[dict]],
    starting_capital: float = 100.0,
    risk_pct:         float = 0.01,
) -> list[dict]:
    """Run FVG Fill backtest on 1H bars. Returns list of closed trades."""
    from core.fvg_scorer import score as fvg_score, clear_cooldown as fvg_clear_cooldown
    from signals.trend.fvg import clear_symbol_touched

    all_closed: list[dict] = []
    warmup = _LOOKBACK + 26   # enough 1H bars for scorer + 4H EMA21 buffer

    for sym in symbols:
        bars_1h = ohlcv.get(f"{sym}:1h", [])
        if len(bars_1h) < warmup + 10:
            log.warning("FVG: insufficient 1h data for %s (%d bars)", sym, len(bars_1h))
            continue

        bt_cache = BacktestCache(ohlcv=ohlcv, oi={}, funding={})
        fvg_clear_cooldown(sym)   # prevent stale DB cooldown from blocking backtest

        equity         = starting_capital
        eval_bars      = bars_1h[warmup:]
        open_trade: dict | None = None
        cooldown_until = 0

        log.info("FVG backtest %s: %d bars (%s → %s)",
                 sym, len(eval_bars),
                 _ts_str(eval_bars[0]["ts"]), _ts_str(eval_bars[-1]["ts"]))

        for bar_idx, bar in enumerate(eval_bars):
            bar_ts = bar["ts"]

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
                continue   # skip new entry when in a trade

            if bar_idx < cooldown_until:
                continue

            # ── Score via live scorer ──────────────────────────────────────────
            clear_symbol_touched(sym)   # reset per-bar so scorer detects gaps fresh
            bt_cache.advance(bar_ts)
            score_dicts = _run_scorer(fvg_score(sym, bt_cache))

            for sd in score_dicts:
                if not sd.get("fire"):
                    continue
                entry = bar["c"]
                sl    = sd.get("fvg_stop", 0.0)
                tp    = sd.get("fvg_tp",   0.0)
                if sl == 0.0 or tp == 0.0:
                    continue
                sl_dist = abs(entry - sl)
                if sl_dist <= 0.0:
                    continue
                risk_amount = round(equity * risk_pct, 4)
                qty         = round(risk_amount / sl_dist, 8)
                open_trade = {
                    "symbol":      sym,
                    "regime":      sd.get("regime", "FVG"),
                    "direction":   sd["direction"],
                    "entry":       entry,
                    "sl":          sl,
                    "tp":          tp,
                    "entry_ts":    bar_ts,
                    "bar_idx":     bar_idx,
                    "risk_amount": risk_amount,
                    "qty":         qty,
                    "score":       sd["score"],
                }
                break   # one trade per bar

        # Force-close remaining open trade at end of data
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
    log.info("FVG backtest complete: %d trades  total_pnl=$%.2f",
             len(all_closed), total_pnl)
    return all_closed
