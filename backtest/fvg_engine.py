"""Fair Value Gap Fill backtest engine — bar-by-bar replay on 1H OHLCV.

Strategy
--------
- Detect 3-bar FVG imbalance zones (bull: bar[k].low > bar[k-2].high).
- Track virgin gaps (not yet retested) up to lookback_bars.
- Entry when price returns into the gap with 4H EMA21 alignment + RSI confirm.
- SL: gap far edge (± sl_buffer_pct); TP: entry ± gap_width × rr_ratio.

Entry  : close of the bar that first enters the gap
SL     : gap_low × (1 - sl_buffer) for LONG  /  gap_high × (1 + sl_buffer) for SHORT
TP     : entry + dist × rr_ratio            /  entry - dist × rr_ratio
"""
import logging
import os
import yaml

from backtest.cost_model import apply_costs
from backtest.regime_classifier import classify_regime
from signals.volume_momentum import get_volume_params_static

_BAR_MINUTES = 60

log = logging.getLogger(__name__)

_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "config.yaml")
with open(_CONFIG_PATH) as _f:
    _cfg = yaml.safe_load(_f)

_FVG_CFG      = _cfg.get("fvg", {})
_MIN_GAP_PCT  = float(_FVG_CFG.get("min_gap_pct",   0.003))
_LOOKBACK     = int(_FVG_CFG.get("lookback_bars",    50))
_SL_BUFFER    = float(_FVG_CFG.get("sl_buffer_pct",  0.002))
_RR           = float(_FVG_CFG.get("rr_ratio",        2.0))
_RSI_WINDOW   = 14
_EMA_PERIOD   = 21
_MACRO_EMA_PERIOD = int(_FVG_CFG.get("macro_ema_period", 200))
_MACRO_FILTER     = bool(_FVG_CFG.get("macro_filter_enabled", True))
_COOLDOWN_BARS = int(_FVG_CFG.get("cooldown_mins",   45))   # 1 bar = 1 hour
_MAX_HOLD      = 24   # 24 × 1h = 1 day

_FVG_SIGNAL_WEIGHTS = {
    "fvg_detected": 0.30,
    "htf_aligned":  0.25,
    "rsi_confirm":  0.20,
    "vol_confirm":  0.10,
    "vol_not_dist": 0.10,
    "rvol_ok":      0.05,
}
_FIRE_THRESHOLD = 0.67


def _ts_str(ts_ms: int) -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


def _ema(closes: list[float], period: int) -> float:
    if not closes:
        return 0.0
    k = 2.0 / (period + 1)
    e = closes[0]
    for v in closes[1:]:
        e = v * k + e * (1 - k)
    return e


def _ema200_direction(bars_1d: list[dict], bar_ts: int) -> tuple[bool, bool]:
    """Return (macro_long_ok, macro_short_ok) using 1D EMA200 at bar_ts.

    macro_long_ok  = price above 1D EMA200 (allow LONG FVGs)
    macro_short_ok = price below 1D EMA200 (allow SHORT FVGs)
    Both True when filter disabled or insufficient data.
    """
    if not _MACRO_FILTER:
        return True, True
    bars_now = [b for b in bars_1d if b["ts"] <= bar_ts]
    if len(bars_now) < _MACRO_EMA_PERIOD:
        return True, True
    closes = [b["c"] for b in bars_now]
    ema200 = sum(closes[:_MACRO_EMA_PERIOD]) / _MACRO_EMA_PERIOD
    k = 2.0 / (_MACRO_EMA_PERIOD + 1)
    for c in closes[_MACRO_EMA_PERIOD:]:
        ema200 = c * k + ema200 * (1 - k)
    price = closes[-1]
    return price > ema200, price < ema200


def _rsi(closes: list[float], window: int = _RSI_WINDOW) -> float:
    if len(closes) < window + 1:
        return 50.0
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0.0)); losses.append(max(-d, 0.0))
    ag = sum(gains[-window:]) / window
    al = sum(losses[-window:]) / window or 1e-9
    return 100.0 - 100.0 / (1.0 + ag / al)


def _find_virgin_fvgs(bars: list[dict], direction: str) -> list[tuple[float, float]]:
    """Return list of (gap_low, gap_high) for unfilled FVGs in bars slice.

    Scans bars[2:-1] so the 3-bar pattern is fully formed and the last bar
    (which we're about to use for entry detection) is excluded.
    """
    gaps = []
    if len(bars) < 3:
        return gaps
    for k in range(2, len(bars) - 1):
        if direction == "LONG":
            gap_low  = bars[k - 2]["h"]
            gap_high = bars[k]["l"]
            if gap_high <= gap_low:
                continue
            gap_pct = (gap_high - gap_low) / gap_low
            if gap_pct < _MIN_GAP_PCT:
                continue
            # Verify gap is still virgin (no candle close inside it after formation)
            filled = any(
                bars[j]["c"] < gap_high and bars[j]["c"] > gap_low
                for j in range(k + 1, len(bars) - 1)
            )
            if not filled:
                gaps.append((gap_low, gap_high))
        else:  # SHORT
            gap_low  = bars[k]["h"]
            gap_high = bars[k - 2]["l"]
            if gap_low >= gap_high:
                continue
            gap_pct = (gap_high - gap_low) / gap_low
            if gap_pct < _MIN_GAP_PCT:
                continue
            filled = any(
                bars[j]["c"] > gap_low and bars[j]["c"] < gap_high
                for j in range(k + 1, len(bars) - 1)
            )
            if not filled:
                gaps.append((gap_low, gap_high))
    return gaps


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
    all_closed: list[dict] = []
    warmup = _LOOKBACK + _EMA_PERIOD + 5

    for sym in symbols:
        bars_1h = ohlcv.get(f"{sym}:1h", [])
        bars_4h = ohlcv.get(f"{sym}:4h", [])
        bars_1d = ohlcv.get(f"{sym}:1d", [])
        if len(bars_1h) < warmup + 10:
            log.warning("FVG: insufficient 1h data for %s (%d bars)", sym, len(bars_1h))
            continue

        equity        = starting_capital
        eval_bars     = bars_1h[warmup:]
        open_trade: dict | None = None
        cooldown_until = 0

        log.info("FVG backtest %s: %d bars (%s → %s)",
                 sym, len(eval_bars),
                 _ts_str(eval_bars[0]["ts"]), _ts_str(eval_bars[-1]["ts"]))

        for bar_idx, bar in enumerate(eval_bars):
            global_idx = warmup + bar_idx
            bar_ts     = bar["ts"]

            # ── Manage open trade ──────────────────────────────────────────────
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
                continue   # skip new entry when in a trade

            if bar_idx < cooldown_until:
                continue

            # ── 1D EMA200 macro direction gate ────────────────────────────────
            macro_long_ok, macro_short_ok = _ema200_direction(bars_1d, bar_ts)

            # ── 4H HTF EMA21 alignment ─────────────────────────────────────────
            b4h_now    = [b for b in bars_4h if b["ts"] <= bar_ts]
            htf_long   = False
            htf_short  = False
            if len(b4h_now) >= _EMA_PERIOD + 2:
                c4h    = [b["c"] for b in b4h_now[-(  _EMA_PERIOD + 5):]]
                ema4h  = _ema(c4h, _EMA_PERIOD)
                htf_long  = c4h[-1] > ema4h
                htf_short = c4h[-1] < ema4h

            # ── Regime + dynamic volume params ────────────────────────────────
            if len(b4h_now) >= 28:
                _c4h = b4h_now[-30:]
                _c1d = [b for b in bars_1d if b["ts"] <= bar_ts][-60:]
                regime = classify_regime(
                    closes_4h=[b["c"] for b in _c4h],
                    highs_4h =[b["h"] for b in _c4h],
                    lows_4h  =[b["l"] for b in _c4h],
                    closes_1d=[b["c"] for b in _c1d],
                )
            else:
                regime = "TREND"
            vol_params = get_volume_params_static(sym, regime, "1h")

            # ── Close data for RSI / EMA ───────────────────────────────────────
            lookback_slice = bars_1h[max(0, global_idx - _LOOKBACK): global_idx + 1]
            bars_1h_window = lookback_slice  # alias for volume signal computation
            closes = [b["c"] for b in lookback_slice]
            if len(closes) < _RSI_WINDOW + 2:
                continue
            rsi     = _rsi(closes)
            current = bar["c"]

            # ── Volume signals (shared for both directions) ────────────────────
            vol_avg      = sum(b["v"] for b in bars_1h_window[-21:-1]) / 20 if len(bars_1h_window) >= 21 else 0
            vol_confirm  = bars_1h_window[-1]["v"] > vol_avg * 1.2 if vol_avg > 0 else True
            vol_not_dist = not (bars_1h_window[-1]["c"] > bars_1h_window[-5]["c"] and
                                bars_1h_window[-1]["v"] < bars_1h_window[-5]["v"] * 0.8) \
                           if len(bars_1h_window) >= 5 else True
            rvol_ok      = bars_1h_window[-1]["v"] / vol_avg > 0.7 if vol_avg > 0 else True

            direction = None
            sl = tp = 0.0

            # ── Try LONG: price enters a bullish FVG ───────────────────────────
            if macro_long_ok and htf_long and rsi <= 45:
                fvgs = _find_virgin_fvgs(lookback_slice, "LONG")
                for gap_low, gap_high in reversed(fvgs):   # most recent first
                    if current < gap_low or current > gap_high:
                        continue   # price not in gap
                    sl   = gap_low * (1.0 - _SL_BUFFER)
                    dist = abs(current - sl)
                    if dist <= 0:
                        continue
                    signals = {
                        "fvg_detected": True,
                        "htf_aligned":  htf_long,
                        "rsi_confirm":  rsi <= 45,
                        "vol_confirm":  vol_confirm,
                        "vol_not_dist": vol_not_dist,
                        "rvol_ok":      rvol_ok,
                    }
                    score = round(sum(_FVG_SIGNAL_WEIGHTS.get(k, 0.0) for k, v in signals.items() if v), 4)
                    if score < _FIRE_THRESHOLD:
                        continue
                    tp        = current + dist * _RR
                    direction = "LONG"
                    break

            # ── Try SHORT: price enters a bearish FVG ──────────────────────────
            if direction is None and macro_short_ok and htf_short and rsi >= 55:
                fvgs = _find_virgin_fvgs(lookback_slice, "SHORT")
                for gap_low, gap_high in reversed(fvgs):
                    if current < gap_low or current > gap_high:
                        continue
                    sl   = gap_high * (1.0 + _SL_BUFFER)
                    dist = abs(sl - current)
                    if dist <= 0:
                        continue
                    signals = {
                        "fvg_detected": True,
                        "htf_aligned":  htf_short,
                        "rsi_confirm":  rsi >= 55,
                        "vol_confirm":  vol_confirm,
                        "vol_not_dist": vol_not_dist,
                        "rvol_ok":      rvol_ok,
                    }
                    score = round(sum(_FVG_SIGNAL_WEIGHTS.get(k, 0.0) for k, v in signals.items() if v), 4)
                    if score < _FIRE_THRESHOLD:
                        continue
                    tp        = current - dist * _RR
                    direction = "SHORT"
                    break

            if direction is None or sl == 0.0 or tp == 0.0:
                continue

            risk_amount = round(equity * risk_pct, 4)
            sl_dist     = abs(current - sl)
            qty         = round(risk_amount / sl_dist, 8) if sl_dist > 0.0 else 0.0
            open_trade = {
                "symbol":      sym,
                "regime":      "FVG",
                "direction":   direction,
                "entry":       current,
                "sl":          sl,
                "tp":          tp,
                "entry_ts":    bar_ts,
                "bar_idx":     bar_idx,
                "risk_amount": risk_amount,
                "qty":         qty,
                "score":       0.67,
            }

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
    log.info("FVG backtest complete: %d trades  total_pnl=$%.2f",
             len(all_closed), total_pnl)
    return all_closed
