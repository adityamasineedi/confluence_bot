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
_COOLDOWN_BARS = int(_FVG_CFG.get("cooldown_mins",   45))   # 1 bar = 1 hour
_MAX_HOLD      = 24   # 24 × 1h = 1 day


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
                continue   # skip new entry when in a trade

            if bar_idx < cooldown_until:
                continue

            # ── 4H HTF EMA21 alignment ─────────────────────────────────────────
            b4h_now    = [b for b in bars_4h if b["ts"] <= bar_ts]
            htf_long   = False
            htf_short  = False
            if len(b4h_now) >= _EMA_PERIOD + 2:
                c4h    = [b["c"] for b in b4h_now[-(  _EMA_PERIOD + 5):]]
                ema4h  = _ema(c4h, _EMA_PERIOD)
                htf_long  = c4h[-1] > ema4h
                htf_short = c4h[-1] < ema4h

            # ── Close data for RSI / EMA ───────────────────────────────────────
            lookback_slice = bars_1h[max(0, global_idx - _LOOKBACK): global_idx]
            closes = [b["c"] for b in lookback_slice]
            if len(closes) < _RSI_WINDOW + 2:
                continue
            rsi     = _rsi(closes)
            current = bar["c"]

            direction = None
            sl = tp = 0.0

            # ── Try LONG: price enters a bullish FVG ───────────────────────────
            if htf_long and rsi <= 45:
                fvgs = _find_virgin_fvgs(lookback_slice, "LONG")
                for gap_low, gap_high in reversed(fvgs):   # most recent first
                    if current < gap_low or current > gap_high:
                        continue   # price not in gap
                    sl   = gap_low * (1.0 - _SL_BUFFER)
                    dist = abs(current - sl)
                    if dist <= 0:
                        continue
                    tp         = current + dist * _RR
                    direction  = "LONG"
                    break

            # ── Try SHORT: price enters a bearish FVG ──────────────────────────
            if direction is None and htf_short and rsi >= 55:
                fvgs = _find_virgin_fvgs(lookback_slice, "SHORT")
                for gap_low, gap_high in reversed(fvgs):
                    if current < gap_low or current > gap_high:
                        continue
                    sl   = gap_high * (1.0 + _SL_BUFFER)
                    dist = abs(sl - current)
                    if dist <= 0:
                        continue
                    tp        = current - dist * _RR
                    direction = "SHORT"
                    break

            if direction is None or sl == 0.0 or tp == 0.0:
                continue

            risk_amount = round(equity * risk_pct, 4)
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
                "score":       0.67,
            }

        # Force-close remaining
        if open_trade is not None:
            result = _force_close(open_trade, eval_bars[-1])
            equity += result["pnl"]
            result["equity_after"] = round(equity, 4)
            all_closed.append(result)

    total_pnl = sum(t["pnl"] for t in all_closed)
    log.info("FVG backtest complete: %d trades  total_pnl=$%.2f",
             len(all_closed), total_pnl)
    return all_closed
