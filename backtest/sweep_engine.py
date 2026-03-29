"""Liquidity Sweep Reversal backtest engine — bar-by-bar replay on 15m OHLCV.

Detects swing high/low stop-hunts (wick through, close back) and enters the
reversal. Works in any regime — does not need BTC above EMA200.

Entry  : close of the sweep candle
SL     : lowest (LONG) / highest (SHORT) wick of the sweep candle - 0.05% buffer
TP     : entry ± (entry - SL) × 2.5
"""
import bisect
import logging
import os
import yaml

from backtest.regime_classifier import classify_regime

log = logging.getLogger(__name__)

_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "config.yaml")
with open(_CONFIG_PATH) as _f:
    _cfg = yaml.safe_load(_f)

# Strategy parameters
_SWING_LOOKBACK   = 50
_SWING_PIVOT_N    = 5        # raised from 3 → more significant pivots only
_SWEEP_MARGIN_PCT = 0.005    # wick must go ≥ 0.5% beyond swing level (raised from 0.15%)
_CLOSE_BUFFER_PCT = 0.003    # raised: close must reclaim ≥ 0.3% inside level
_VOL_SPIKE_MULT   = 2.0      # institutional only — ≥ 2.0× average (raised from 1.4×)
_BODY_STRENGTH    = 0.4      # close must be in top/bottom 40% of candle range
_HTF_BLOCK_PCT    = 0.010    # 4H close must be within 1.0% of EMA21 to allow trade
_RSI_PERIOD       = 14
_COOLDOWN_BARS    = 6        # 6 × 15m = 90 min cooldown
_MAX_HOLD         = 8        # 8 × 15m = 2h max hold
_RR               = 2.5
_SL_BUFFER        = 0.0005   # 0.05% beyond the wick


def _ts_str(ts_ms: int) -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


def _rsi(closes: list[float], period: int = _RSI_PERIOD) -> float:
    if len(closes) < period + 1:
        return 50.0
    gains, losses = [], []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gains.append(max(diff, 0.0))
        losses.append(max(-diff, 0.0))
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0.0:
        return 100.0
    return 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)


def _find_swing_lows(bars: list[dict], n: int) -> list[float]:
    """Swing lows from bars slice (pivot-n detection)."""
    levels = []
    for i in range(n, len(bars) - n):
        low = bars[i]["l"]
        if all(bars[i]["l"] <= bars[i - j]["l"] for j in range(1, n + 1)) and \
           all(bars[i]["l"] <= bars[i + j]["l"] for j in range(1, n + 1)):
            levels.append(low)
    return levels


def _find_swing_highs(bars: list[dict], n: int) -> list[float]:
    levels = []
    for i in range(n, len(bars) - n):
        high = bars[i]["h"]
        if all(bars[i]["h"] >= bars[i - j]["h"] for j in range(1, n + 1)) and \
           all(bars[i]["h"] >= bars[i + j]["h"] for j in range(1, n + 1)):
            levels.append(high)
    return levels


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
    """Run sweep reversal backtest on 15m bars.  Returns list of closed trades."""
    all_closed: list[dict] = []
    warmup = _SWING_LOOKBACK + _SWING_PIVOT_N + 22   # need vol MA + pivot lookback

    for sym in symbols:
        bars    = ohlcv.get(f"{sym}:15m", [])
        bars_1d = ohlcv.get(f"{sym}:1d",  [])
        _ts_1d  = [b["ts"] for b in bars_1d]

        if len(bars) < warmup + 10:
            log.warning("Sweep: insufficient 15m data for %s (%d bars)", sym, len(bars))
            continue

        equity     = starting_capital
        eval_bars  = bars[warmup:]
        log.info("Sweep backtest %s: %d bars (%s → %s)",
                 sym, len(eval_bars), _ts_str(eval_bars[0]["ts"]), _ts_str(eval_bars[-1]["ts"]))

        open_trade:     dict | None = None
        cooldown_until: int         = 0

        for bar_idx, bar in enumerate(eval_bars):
            global_idx = warmup + bar_idx

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
                continue

            if bar_idx < cooldown_until:
                continue

            # ── Detect sweep on the *completed* current bar ────────────────────
            # Lookback slice: exclude current bar for pivot detection
            lookback_slice = bars[max(0, global_idx - _SWING_LOOKBACK - _SWING_PIVOT_N): global_idx - 1]

            candle = bar
            low    = candle["l"]
            high   = candle["h"]
            close  = candle["c"]
            open_  = candle["o"]

            # Volume data (exclude current bar from average)
            vol_slice = bars[max(0, global_idx - 21): global_idx]
            avg_vol   = sum(b["v"] for b in vol_slice) / len(vol_slice) if vol_slice else 0
            vol_spike = avg_vol > 0 and candle["v"] >= avg_vol * _VOL_SPIKE_MULT

            closes_slice = [b["c"] for b in bars[max(0, global_idx - 20): global_idx + 1]]
            rsi = _rsi(closes_slice)

            direction = None
            sl = tp = 0.0

            # ── 4H macro bias — hard gate, defaults False when no data ───────
            bar_ts   = bar["ts"]
            bars_4h  = ohlcv.get(f"{sym}:4h", [])
            b4h_now  = [b for b in bars_4h if b["ts"] <= bar_ts]
            htf_long = htf_short_ok = False   # default BLOCK — no data → no trade
            if len(b4h_now) >= 22:
                c4h = [b["c"] for b in b4h_now]
                k4  = 2.0 / 22
                e4  = sum(c4h[:21]) / 21
                for cv in c4h[21:]:
                    e4 = cv * k4 + e4 * (1 - k4)
                # Block LONG if 4H is in strong downtrend (close > 1.0% below EMA21)
                htf_long     = c4h[-1] >= e4 * (1 - _HTF_BLOCK_PCT)
                # Block SHORT if 4H is in strong uptrend (close > 1.0% above EMA21)
                htf_short_ok = c4h[-1] <= e4 * (1 + _HTF_BLOCK_PCT)

            # ── Regime classification (label only — sweep fires in all regimes) ─
            _c4h   = b4h_now[-30:] if len(b4h_now) >= 30 else b4h_now
            _b1d_i = bisect.bisect_right(_ts_1d, bar_ts) - 1
            _c1d   = bars_1d[max(0, _b1d_i - 59): _b1d_i + 1] if _b1d_i >= 0 else []
            regime = classify_regime(
                closes_4h=[b["c"] for b in _c4h],
                highs_4h=[b["h"] for b in _c4h],
                lows_4h=[b["l"] for b in _c4h],
                closes_1d=[b["c"] for b in _c1d],
            )

            # Body strength check: close in top 40% for long, bottom 40% for short
            candle_range = high - low
            body_long  = (close - low)  / candle_range >= _BODY_STRENGTH if candle_range > 0 else False
            body_short = (high  - close) / candle_range >= _BODY_STRENGTH if candle_range > 0 else False

            # ── LONG sweep: wick below swing low, close back above ─────────────
            swing_lows = _find_swing_lows(lookback_slice, _SWING_PIVOT_N)
            if htf_long:
                for level in swing_lows:
                    if low > level * (1 - _SWEEP_MARGIN_PCT):
                        continue
                    if close < level * (1 + _CLOSE_BUFFER_PCT):
                        continue
                    if not vol_spike:
                        continue
                    if not body_long:
                        continue
                    if rsi > 50:
                        continue
                    direction = "LONG"
                    sl = low * (1 - _SL_BUFFER)
                    dist = close - sl
                    if dist > 0:
                        tp = close + dist * _RR
                        break
                    direction = None

            # ── SHORT sweep: wick above swing high, close back below ───────────
            if direction is None and htf_short_ok:
                swing_highs = _find_swing_highs(lookback_slice, _SWING_PIVOT_N)
                for level in swing_highs:
                    if high < level * (1 + _SWEEP_MARGIN_PCT):
                        continue
                    if close > level * (1 - _CLOSE_BUFFER_PCT):
                        continue
                    if not vol_spike:
                        continue
                    if not body_short:
                        continue
                    if rsi < 50:
                        continue
                    direction = "SHORT"
                    sl = high * (1 + _SL_BUFFER)
                    dist = sl - close
                    if dist > 0:
                        tp = close - dist * _RR
                        break
                    direction = None

            if direction is None or sl == 0.0 or tp == 0.0:
                continue

            risk_amount = round(equity * risk_pct, 4)
            open_trade = {
                "symbol":      sym,
                "regime":      regime,
                "direction":   direction,
                "entry":       close,
                "sl":          sl,
                "tp":          tp,
                "rr":          _RR,
                "entry_ts":    bar["ts"],
                "bar_idx":     bar_idx,
                "risk_amount": risk_amount,
                "score":       0.75,
            }

        # Force-close any open trade at end of data
        if open_trade is not None:
            last_bar = eval_bars[-1]
            result = _force_close(open_trade, last_bar)
            equity += result["pnl"]
            result["equity_after"] = round(equity, 4)
            all_closed.append(result)

    total_pnl = sum(t["pnl"] for t in all_closed)
    avg_sym_ret = (total_pnl / starting_capital / max(len(symbols), 1)) * 100
    log.info("Sweep backtest complete: %d trades  total_pnl=$%.2f  avg_return_per_symbol=%.1f%%",
             len(all_closed), total_pnl, avg_sym_ret)
    return all_closed
