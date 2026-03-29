"""15m EMA Pullback backtest engine — trend-continuation entries at EMA21.

Entry  : close of the bounce candle (after pullback to EMA21 on 15m)
SL     : EMA21 × (1 - 0.002) for LONG, × (1 + 0.002) for SHORT
TP     : entry ± (entry - SL) × 1.5
Macro  : 4H EMA21 vs EMA50 determines LONG / SHORT bias
"""
import bisect
import logging
import os
import yaml

from backtest.cost_model import apply_costs
from backtest.regime_classifier import classify_regime
from signals.volume_momentum import get_volume_params_static

_BAR_MINUTES = 15

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
_MAX_HOLD        = int(_EP.get("max_hold_bars",         4))
_FIRE_THRESHOLD  = float(_EP.get("fire_threshold",     0.75))

_EMA_FAST        = 21
_EMA_SLOW        = 50
_RSI_PERIOD      = 14
_RSI_LONG_MIN    = float(_EP.get("rsi_long_min",  30.0))
_RSI_LONG_MAX    = float(_EP.get("rsi_long_max",  50.0))
_RSI_SHORT_MIN   = float(_EP.get("rsi_short_min", 50.0))
_RSI_SHORT_MAX   = float(_EP.get("rsi_short_max", 70.0))
_SL_ATR_MULT = _EP.get("sl_atr_mult", {"tier1": 1.5, "tier2": 2.0, "tier3": 2.5, "base": 2.0})
_MIN_SL_PCT  = float(_EP.get("min_sl_pct", 0.003))


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
        bars_1d  = ohlcv.get(f"{sym}:1d",  [])
        _ts_1d   = [b["ts"] for b in bars_1d]

        if len(bars_15m) < warmup_15m + 10:
            log.warning("EMA Pullback: insufficient 15m data for %s (%d bars)", sym, len(bars_15m))
            continue
        if len(bars_4h) < warmup_4h:
            log.warning("EMA Pullback: insufficient 4h data for %s (%d bars)", sym, len(bars_4h))
            continue

        from core.symbol_config import get_symbol_tier
        _tier     = get_symbol_tier(sym)
        _atr_mult = float(_SL_ATR_MULT.get(_tier, _SL_ATR_MULT.get("base", 2.0)))

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

            # ── Regime classification ─────────────────────────────────────────
            _c4h = bars_4h_current[-30:]
            _b1d_i = bisect.bisect_right(_ts_1d, bar_ts) - 1
            _c1d = bars_1d[max(0, _b1d_i - 59): _b1d_i + 1] if _b1d_i >= 0 else []
            regime = classify_regime(
                closes_4h=[b["c"] for b in _c4h],
                highs_4h=[b["h"] for b in _c4h],
                lows_4h=[b["l"] for b in _c4h],
                closes_1d=[b["c"] for b in _c1d],
            )
            # EMA Pullback only fires in trend conditions
            if regime not in ("TREND", "BREAKOUT"):
                continue

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

            # RVOL gate — skip low time-of-day volume entries
            _vol_params = get_volume_params_static(sym, regime, "15m")
            _rvol_bars  = bars_slice[-25:]
            if not _vol_params.rvol_ok(_rvol_bars):
                continue

            # 4H macro bias — compute once for both directions
            closes_4h = [b["c"] for b in bars_4h]
            if len(closes_4h) >= 50:
                ema21_4h = sum(closes_4h[-21:]) / 21
                ema50_4h = sum(closes_4h[-50:]) / 50
                htf_bull = closes_4h[-1] > ema50_4h or ema21_4h > ema50_4h
                htf_bear = closes_4h[-1] < ema50_4h and ema21_4h < ema50_4h
            else:
                htf_bull = True  # insufficient data — allow
                htf_bear = True

            # ── ATR for SL sizing ──────────────────────────────────────────────
            if len(bars_slice) >= 2:
                _trs = []
                for _j in range(1, len(bars_slice)):
                    _h, _l, _pc = bars_slice[_j]["h"], bars_slice[_j]["l"], bars_slice[_j - 1]["c"]
                    _trs.append(max(_h - _l, abs(_h - _pc), abs(_l - _pc)))
                _atr_val = sum(_trs[-14:]) / min(14, len(_trs)) if _trs else price * 0.005
            else:
                _atr_val = price * 0.005
            _ema_dist_raw = abs(price - ema21_15m)
            _stop_dist = max(_atr_val * _atr_mult, _ema_dist_raw * 1.5, price * _MIN_SL_PCT)

            # ── LONG: 4H bullish, 15m EMA21 > EMA50, price bounced off EMA21 ──
            if htf_bull and htf_long and ema21_15m > ema50_15m:
                touch = (abs(prev_low  - ema21_15m) / ema21_15m <= _PULLBACK_PCT or
                         abs(closes_15m[-2] - ema21_15m) / ema21_15m <= _PULLBACK_PCT)
                # Close must be ≥ 0.2% above EMA21 (not a marginal cross)
                ema_dist_long = (price - ema21_15m) / ema21_15m if ema21_15m > 0 else 0
                if (touch and ema_dist_long >= _MIN_BOUNCE_PCT
                        and quiet_pull and vol_confirm):
                    if _RSI_LONG_MIN <= rsi <= _RSI_LONG_MAX:
                        direction = "LONG"
                        sl = price - _stop_dist
                        tp = price + _stop_dist * _RR

            # ── SHORT: 4H bearish, 15m EMA21 < EMA50, price rejected at EMA21 ─
            if htf_bear and direction is None and htf_short and ema21_15m < ema50_15m:
                touch = (abs(prev_high - ema21_15m) / ema21_15m <= _PULLBACK_PCT or
                         abs(closes_15m[-2] - ema21_15m) / ema21_15m <= _PULLBACK_PCT)
                # Close must be ≥ 0.2% below EMA21 (not a marginal cross)
                ema_dist_short = (ema21_15m - price) / ema21_15m if ema21_15m > 0 else 0
                if (touch and ema_dist_short >= _MIN_BOUNCE_PCT
                        and quiet_pull and vol_confirm):
                    if _RSI_SHORT_MIN <= rsi <= _RSI_SHORT_MAX:
                        direction = "SHORT"
                        sl = price + _stop_dist
                        tp = price - _stop_dist * _RR

            if direction is None or sl == 0.0 or tp == 0.0:
                continue

            risk_amount = round(equity * risk_pct, 4)
            sl_dist     = abs(price - sl)
            qty         = round(risk_amount / sl_dist, 8) if sl_dist > 0.0 else 0.0
            open_trade = {
                "symbol":      sym,
                "regime":      regime,
                "direction":   direction,
                "entry":       price,
                "sl":          sl,
                "tp":          tp,
                "rr":          _RR,
                "entry_ts":    bar["ts"],
                "bar_idx":     bar_idx,
                "risk_amount": risk_amount,
                "qty":         qty,
                "score":       _FIRE_THRESHOLD,
            }

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
