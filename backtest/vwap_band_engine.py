"""VWAP Band Reversion backtest engine — bar-by-bar replay on 15m OHLCV.

Uses the live vwap_band_scorer directly via BacktestCache so the backtest
always tests the same logic as production.  VWAP is still re-computed each
bar so the dynamic TP (vwap_mid) tracks correctly during the trade.

Entry  : close of the bar where the scorer fires
SL/TP  : from scorer at entry; TP dynamically updated to current VWAP each bar
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

_VB_CFG = _cfg.get("vwap_band", {})

_WINDOW        = int(_VB_CFG.get("window_bars",    20))
_BAND_MULT     = float(_VB_CFG.get("band_mult",     2.0))
_COOLDOWN_BARS = int(_VB_CFG.get("cooldown_mins",   30) // 15)   # 30min → 2 × 15m bars
_MAX_HOLD      = int(_VB_CFG.get("max_hold_bars",    4))


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


def _compute_vwap_bands(bars: list[dict]) -> dict | None:
    """Compute rolling VWAP and ±2σ bands over bars slice."""
    if len(bars) < 2:
        return None
    total_vol = sum(b["v"] for b in bars)
    if total_vol <= 0:
        return None
    vwap = sum((b["h"] + b["l"] + b["c"]) / 3 * b["v"] for b in bars) / total_vol
    var  = sum(b["v"] * (((b["h"] + b["l"] + b["c"]) / 3) - vwap) ** 2 for b in bars) / total_vol
    std  = var ** 0.5
    return {
        "vwap":    vwap,
        "upper_2": vwap + _BAND_MULT * std,
        "lower_2": vwap - _BAND_MULT * std,
        "std_dev": std,
    }


def _check_exit(trade: dict, bar: dict, vwap_mid: float) -> dict | None:
    """Check SL/TP; TP is the dynamic VWAP midline."""
    risk = trade["risk_amount"]
    sl   = trade["sl"]
    tp   = vwap_mid   # chase VWAP dynamically

    if trade["direction"] == "LONG":
        if bar["l"] <= sl:
            return {**trade, "exit_ts": bar["ts"], "outcome": "LOSS",
                    "pnl": round(-risk, 4)}
        if bar["h"] >= tp:
            pnl_r = abs(tp - trade["entry"]) / abs(trade["entry"] - sl) if abs(trade["entry"] - sl) > 0 else 0
            return {**trade, "exit_ts": bar["ts"], "outcome": "WIN",
                    "pnl": round(risk * pnl_r, 4)}
    else:
        if bar["h"] >= sl:
            return {**trade, "exit_ts": bar["ts"], "outcome": "LOSS",
                    "pnl": round(-risk, 4)}
        if bar["l"] <= tp:
            pnl_r = abs(trade["entry"] - tp) / abs(sl - trade["entry"]) if abs(sl - trade["entry"]) > 0 else 0
            return {**trade, "exit_ts": bar["ts"], "outcome": "WIN",
                    "pnl": round(risk * pnl_r, 4)}
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
    """Run VWAP Band Reversion backtest on 15m bars. Returns list of closed trades."""
    from core.vwap_band_scorer import score as vb_score, clear_cooldown as vb_clear_cooldown

    all_closed: list[dict] = []
    warmup = _WINDOW + 20   # VWAP window + RSI buffer

    for sym in symbols:
        bars_15m = ohlcv.get(f"{sym}:15m", [])
        if len(bars_15m) < warmup + 10:
            log.warning("VWAPBand: insufficient 15m data for %s (%d bars)", sym, len(bars_15m))
            continue

        bt_cache = BacktestCache(ohlcv=ohlcv, oi={}, funding={})
        vb_clear_cooldown(sym)   # prevent stale DB cooldown from blocking backtest

        equity         = starting_capital
        eval_bars      = bars_15m[warmup:]
        open_trade: dict | None  = None
        cooldown_until = 0

        log.info("VWAPBand backtest %s: %d bars (%s → %s)",
                 sym, len(eval_bars),
                 _ts_str(eval_bars[0]["ts"]), _ts_str(eval_bars[-1]["ts"]))

        for bar_idx, bar in enumerate(eval_bars):
            global_idx = warmup + bar_idx
            bar_ts     = bar["ts"]

            # ── Compute current VWAP (for dynamic TP tracking during exit) ─────
            band_slice = bars_15m[max(0, global_idx - _WINDOW): global_idx + 1]
            bands      = _compute_vwap_bands(band_slice)
            vwap_mid   = bands["vwap"] if bands else (open_trade["entry"] if open_trade else bar["c"])

            # ── Manage open trade ──────────────────────────────────────────────
            if open_trade is not None:
                result = _check_exit(open_trade, bar, vwap_mid)
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

            if bands is None:
                continue

            # ── Score via live scorer ──────────────────────────────────────────
            bt_cache.advance(bar_ts)
            score_dicts = _run_scorer(vb_score(sym, bt_cache))

            for sd in score_dicts:
                if not sd.get("fire"):
                    continue
                entry = bar["c"]
                sl    = sd.get("vb_stop", 0.0)
                tp    = sd.get("vb_tp",   0.0)
                if sl == 0.0 or tp == 0.0:
                    continue
                sl_dist = abs(entry - sl)
                if sl_dist <= 0.0:
                    continue
                risk_amount = round(equity * risk_pct, 4)
                qty         = round(risk_amount / sl_dist, 8)
                open_trade = {
                    "symbol":      sym,
                    "regime":      sd.get("regime", "VWAPBAND"),
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

        # Force-close remaining
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
    log.info("VWAPBand backtest complete: %d trades  total_pnl=$%.2f", len(all_closed), total_pnl)
    return all_closed
