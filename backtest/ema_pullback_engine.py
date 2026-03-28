"""15m EMA Pullback backtest engine — trend-continuation entries at EMA21.

Entry  : close of the bounce candle (after pullback to EMA21 on 15m)
SL     : EMA21 × (1 - 0.002) for LONG, × (1 + 0.002) for SHORT
TP     : entry ± (entry - SL) × 1.5
Macro  : 4H EMA21 vs EMA50 determines LONG / SHORT bias
"""
import logging
import os
import yaml

log = logging.getLogger(__name__)

_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "config.yaml")
with open(_CONFIG_PATH) as _f:
    _cfg = yaml.safe_load(_f)

_EP = _cfg.get("ema_pullback", {})

_RR              = float(_EP.get("rr_ratio",           1.5))
_PULLBACK_PCT    = float(_EP.get("pullback_touch_pct", 0.002))
_MIN_BOUNCE_PCT  = float(_EP.get("min_bounce_body_pct",0.002))
_VOL_QUIET       = float(_EP.get("vol_quiet_mult",     1.2))
_COOLDOWN_BARS   = int(_EP.get("cooldown_mins",        45) // 15)  # 45 min → 3 × 15m bars
_MAX_HOLD        = int(_EP.get("max_hold_bars",         8))
_FIRE_THRESHOLD  = float(_EP.get("fire_threshold",     0.75))

_EMA_FAST        = 21
_EMA_SLOW        = 50
_RSI_PERIOD      = 14
_RSI_LONG_MIN    = 35
_RSI_LONG_MAX    = 60
_RSI_SHORT_MIN   = 40
_RSI_SHORT_MAX   = 65
_SL_BUFFER       = 0.002   # 0.2% below/above EMA21


def _ts_str(ts_ms: int) -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


def _ema(closes: list[float], period: int) -> float:
    if len(closes) < period:
        return 0.0
    k   = 2.0 / (period + 1)
    ema = sum(closes[:period]) / period
    for c in closes[period:]:
        ema = c * k + ema * (1 - k)
    return ema


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
    all_closed: list[dict] = []
    warmup_15m = _EMA_SLOW + 10
    warmup_4h  = _EMA_SLOW + 5

    for sym in symbols:
        bars_15m = ohlcv.get(f"{sym}:15m", [])
        bars_4h  = ohlcv.get(f"{sym}:4h",  [])

        if len(bars_15m) < warmup_15m + 10:
            log.warning("EMA Pullback: insufficient 15m data for %s (%d bars)", sym, len(bars_15m))
            continue
        if len(bars_4h) < warmup_4h:
            log.warning("EMA Pullback: insufficient 4h data for %s (%d bars)", sym, len(bars_4h))
            continue

        equity     = starting_capital
        eval_bars  = bars_15m[warmup_15m:]
        log.info("EMA Pullback backtest %s: %d × 15m bars (%s → %s)",
                 sym, len(eval_bars), _ts_str(eval_bars[0]["ts"]), _ts_str(eval_bars[-1]["ts"]))

        open_trade:     dict | None = None
        cooldown_until: int         = 0

        for bar_idx, bar in enumerate(eval_bars):
            global_idx = warmup_15m + bar_idx

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

            # ── 4H macro bias ──────────────────────────────────────────────────
            # Find 4H bars up to current 15m timestamp
            bar_ts = bar["ts"]
            bars_4h_current = [b for b in bars_4h if b["ts"] <= bar_ts]
            if len(bars_4h_current) < warmup_4h:
                continue

            closes_4h = [b["c"] for b in bars_4h_current]
            ema21_4h  = _ema(closes_4h, _EMA_FAST)
            ema50_4h  = _ema(closes_4h, _EMA_SLOW)

            htf_long  = closes_4h[-1] > ema50_4h or ema21_4h > ema50_4h
            htf_short = closes_4h[-1] < ema50_4h and ema21_4h < ema50_4h

            if not (htf_long or htf_short):
                continue   # choppy 4H — no clear bias

            # ── 15m indicators ─────────────────────────────────────────────────
            bars_slice = bars_15m[max(0, global_idx - _EMA_SLOW - 2): global_idx + 1]
            if len(bars_slice) < _EMA_SLOW + 2:
                continue

            closes_15m = [b["c"] for b in bars_slice]
            ema21_15m  = _ema(closes_15m, _EMA_FAST)
            ema50_15m  = _ema(closes_15m, _EMA_SLOW)

            if ema21_15m <= 0 or ema50_15m <= 0:
                continue

            price     = bar["c"]
            prev_bar  = bars_slice[-2]
            prev_low  = prev_bar["l"]
            prev_high = prev_bar["h"]

            rsi = _rsi(closes_15m)

            # Volume: pullback bar (prev) must be quiet
            vol_slice = bars_slice[-21:]
            avg_vol   = sum(b["v"] for b in vol_slice[:-1]) / max(len(vol_slice) - 1, 1)
            quiet_pull = avg_vol == 0 or prev_bar["v"] <= avg_vol * _VOL_QUIET

            direction = None
            sl = tp = 0.0

            # Volume gate: bounce bar must have more volume than pullback bar
            vol_confirm = bar["v"] > prev_bar["v"]

            # ── LONG: 4H bullish, 15m EMA21 > EMA50, price bounced off EMA21 ──
            if htf_long and ema21_15m > ema50_15m:
                touch = (abs(prev_low  - ema21_15m) / ema21_15m <= _PULLBACK_PCT or
                         abs(closes_15m[-2] - ema21_15m) / ema21_15m <= _PULLBACK_PCT)
                # Close must be ≥ 0.2% above EMA21 (not a marginal cross)
                ema_dist_long = (price - ema21_15m) / ema21_15m if ema21_15m > 0 else 0
                if (touch and ema_dist_long >= _MIN_BOUNCE_PCT
                        and quiet_pull and vol_confirm):
                    if _RSI_LONG_MIN <= rsi <= _RSI_LONG_MAX:
                        direction = "LONG"
                        sl = ema21_15m * (1 - _SL_BUFFER)
                        dist = price - sl
                        if dist > 0:
                            tp = price + dist * _RR

            # ── SHORT: 4H bearish, 15m EMA21 < EMA50, price rejected at EMA21 ─
            if direction is None and htf_short and ema21_15m < ema50_15m:
                touch = (abs(prev_high - ema21_15m) / ema21_15m <= _PULLBACK_PCT or
                         abs(closes_15m[-2] - ema21_15m) / ema21_15m <= _PULLBACK_PCT)
                # Close must be ≥ 0.2% below EMA21 (not a marginal cross)
                ema_dist_short = (ema21_15m - price) / ema21_15m if ema21_15m > 0 else 0
                if (touch and ema_dist_short >= _MIN_BOUNCE_PCT
                        and quiet_pull and vol_confirm):
                    if _RSI_SHORT_MIN <= rsi <= _RSI_SHORT_MAX:
                        direction = "SHORT"
                        sl = ema21_15m * (1 + _SL_BUFFER)
                        dist = sl - price
                        if dist > 0:
                            tp = price - dist * _RR

            if direction is None or sl == 0.0 or tp == 0.0:
                continue

            risk_amount = round(equity * risk_pct, 4)
            open_trade = {
                "symbol":      sym,
                "regime":      "EMA_PULLBACK",
                "direction":   direction,
                "entry":       price,
                "sl":          sl,
                "tp":          tp,
                "rr":          _RR,
                "entry_ts":    bar["ts"],
                "bar_idx":     bar_idx,
                "risk_amount": risk_amount,
                "score":       _FIRE_THRESHOLD,
            }

        if open_trade is not None:
            last_bar = eval_bars[-1]
            result = _force_close(open_trade, last_bar)
            equity += result["pnl"]
            result["equity_after"] = round(equity, 4)
            all_closed.append(result)

    total_pnl = sum(t["pnl"] for t in all_closed)
    avg_sym_ret = (total_pnl / starting_capital / max(len(symbols), 1)) * 100
    log.info("EMA Pullback backtest complete: %d trades  total_pnl=$%.2f  avg_return=%.1f%%",
             len(all_closed), total_pnl, avg_sym_ret)
    return all_closed
