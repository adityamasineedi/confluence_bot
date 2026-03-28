"""OI Spike Fade backtest engine — bar-by-bar replay on 15m OHLCV.

Strategy
--------
- Detect sudden OI surges using the `oi` data dict when available.
- Fallback proxy when OI data absent: volume spike ≥ 2.5× 20-bar average
  (high-volume bars approximate liquidation cascades well in backtests).
- Confirm with wick rejection (lower wick for LONG, upper wick for SHORT).
- EMA21 and RSI gates filter noise.

Entry  : close of the rejection candle
SL     : wick extreme ± sl_buffer  (LONG: candle.low - buffer; SHORT: candle.high + buffer)
TP     : entry ± ATR(14, 15m) × atr_mult  (default 2.0)
"""
import logging
import os
import yaml

log = logging.getLogger(__name__)

_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "config.yaml")
with open(_CONFIG_PATH) as _f:
    _cfg = yaml.safe_load(_f)

_OS_CFG       = _cfg.get("oi_spike", {})
_SPIKE_PCT    = float(_OS_CFG.get("spike_pct",       0.15))
_LOOKBACK_HRS = float(_OS_CFG.get("lookback_hours",   2.0))
_WICK_PCT     = float(_OS_CFG.get("wick_pct",         0.005))
_SL_BUFFER    = float(_OS_CFG.get("sl_buffer",        0.002))
_ATR_MULT     = float(_OS_CFG.get("atr_mult",         2.0))
_ATR_WINDOW   = int(_OS_CFG.get("atr_window",         14))
_EMA_PERIOD   = int(_OS_CFG.get("ema_period",         21))
_RSI_WINDOW   = int(_OS_CFG.get("rsi_window",         14))
_VOL_MULT     = float(_OS_CFG.get("vol_mult",         1.5))
_RR           = float(_OS_CFG.get("rr_ratio",         2.0))

# When OI data unavailable, use volume proxy (higher threshold to reduce false positives)
_VOL_PROXY_MULT = 2.5

_COOLDOWN_BARS = int(_OS_CFG.get("cooldown_mins", 60) // 15)  # 60min → 4 × 15m bars
_MAX_HOLD      = 8    # 8 × 15m = 2h


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


def _atr(bars: list[dict], window: int = _ATR_WINDOW) -> float:
    if len(bars) < 2:
        return 0.0
    trs = []
    for i in range(1, len(bars)):
        h, l, pc = bars[i]["h"], bars[i]["l"], bars[i - 1]["c"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    recent = trs[-window:]
    return sum(recent) / len(recent) if recent else 0.0


def _oi_spike_from_data(
    sym: str,
    bar_ts: int,
    oi_data: dict[str, list[dict]],
    lookback_ms: int,
) -> float | None:
    """Return fractional OI change if OI history data is available.

    oi_data format: {"BTCUSDT:binance": [{"ts": ms, "oi": float}, ...], ...}
    Returns None when OI data absent.
    """
    key = f"{sym}:binance"
    rows = oi_data.get(key, [])
    if not rows:
        return None
    oi_now_rows  = [r for r in rows if r["ts"] <= bar_ts]
    oi_prev_rows = [r for r in rows if r["ts"] <= bar_ts - lookback_ms]
    if not oi_now_rows or not oi_prev_rows:
        return None
    oi_now  = oi_now_rows[-1]["oi"]
    oi_prev = oi_prev_rows[-1]["oi"]
    if oi_prev <= 0:
        return None
    return (oi_now - oi_prev) / oi_prev


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
    oi:               dict[str, list[dict]] | None = None,
    starting_capital: float = 100.0,
    risk_pct:         float = 0.01,
) -> list[dict]:
    """Run OI Spike Fade backtest on 15m bars.

    `oi` is optional — if absent, a volume-spike proxy is used instead.
    Returns list of closed trades.
    """
    all_closed: list[dict] = []
    oi_data    = oi or {}
    warmup     = _ATR_WINDOW + _EMA_PERIOD + 5
    lookback_ms = int(_LOOKBACK_HRS * 3_600_000)

    for sym in symbols:
        bars_15m = ohlcv.get(f"{sym}:15m", [])
        if len(bars_15m) < warmup + 10:
            log.warning("OISpike: insufficient 15m data for %s (%d bars)", sym, len(bars_15m))
            continue

        equity         = starting_capital
        eval_bars      = bars_15m[warmup:]
        open_trade: dict | None  = None
        cooldown_until = 0
        has_oi_data    = bool(oi_data.get(f"{sym}:binance"))

        if not has_oi_data:
            log.info("OISpike %s: no OI history — using volume proxy (%.1f× avg)",
                     sym, _VOL_PROXY_MULT)

        log.info("OISpike backtest %s: %d bars (%s → %s)",
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
                continue

            if bar_idx < cooldown_until:
                continue

            slice_bars = bars_15m[max(0, global_idx - _ATR_WINDOW - 5): global_idx + 1]
            closes     = [b["c"] for b in slice_bars]
            if len(closes) < _RSI_WINDOW + 2:
                continue

            # ── OI spike check ─────────────────────────────────────────────────
            if has_oi_data:
                spike = _oi_spike_from_data(sym, bar_ts, oi_data, lookback_ms)
                oi_ok = spike is not None and spike >= _SPIKE_PCT
            else:
                # Volume proxy: very high volume suggests liquidation cascade
                vol_hist  = bars_15m[max(0, global_idx - 21): global_idx]
                avg_vol   = sum(b["v"] for b in vol_hist) / len(vol_hist) if vol_hist else 0
                oi_ok     = avg_vol > 0 and bar["v"] >= avg_vol * _VOL_PROXY_MULT
                spike     = (bar["v"] / avg_vol - 1.0) if avg_vol > 0 else 0.0

            if not oi_ok:
                continue

            atr     = _atr(slice_bars, _ATR_WINDOW)
            rsi     = _rsi(closes)
            ema_val = _ema(closes[:-1], _EMA_PERIOD)
            close   = bar["c"]; o = bar["o"]; h = bar["h"]; lo = bar["l"]
            body_size = abs(close - o)

            # Volume confirmation (even with OI data, need volume spike too)
            vol_hist = bars_15m[max(0, global_idx - 21): global_idx]
            avg_vol  = sum(b["v"] for b in vol_hist) / len(vol_hist) if vol_hist else 0
            vol_ok   = avg_vol <= 0 or bar["v"] >= avg_vol * _VOL_MULT

            direction = None
            sl = tp   = 0.0

            # ── LONG: lower wick rejection + above EMA + RSI 35-55 ────────────
            lower_wick = min(o, close) - lo
            lw_pct     = lower_wick / lo if lo > 0 else 0.0
            if (lw_pct >= _WICK_PCT and lower_wick >= body_size * 0.5
                    and close > ema_val and 35 <= rsi <= 55 and vol_ok):
                sl   = lo * (1.0 - _SL_BUFFER)
                tp   = close + atr * _ATR_MULT
                dist = abs(close - sl)
                if dist > 0 and (tp - close) / dist >= 1.5:
                    direction = "LONG"

            # ── SHORT: upper wick rejection + below EMA + RSI 45-65 ──────────
            if direction is None:
                upper_wick = h - max(o, close)
                uw_pct     = upper_wick / h if h > 0 else 0.0
                if (uw_pct >= _WICK_PCT and upper_wick >= body_size * 0.5
                        and close < ema_val and 45 <= rsi <= 65 and vol_ok):
                    sl   = h * (1.0 + _SL_BUFFER)
                    tp   = close - atr * _ATR_MULT
                    dist = abs(sl - close)
                    if dist > 0 and (close - tp) / dist >= 1.5:
                        direction = "SHORT"

            if direction is None or sl == 0.0 or tp == 0.0 or atr == 0.0:
                continue

            risk_amount = round(equity * risk_pct, 4)
            open_trade = {
                "symbol":      sym,
                "regime":      "OISPIKE",
                "direction":   direction,
                "entry":       close,
                "sl":          sl,
                "tp":          tp,
                "entry_ts":    bar_ts,
                "bar_idx":     bar_idx,
                "risk_amount": risk_amount,
                "score":       0.75,
                "spike_pct":   round(spike, 4),
            }

        # Force-close remaining
        if open_trade is not None:
            result = _force_close(open_trade, eval_bars[-1])
            equity += result["pnl"]
            result["equity_after"] = round(equity, 4)
            all_closed.append(result)

    total_pnl = sum(t["pnl"] for t in all_closed)
    log.info("OISpike backtest complete: %d trades  total_pnl=$%.2f", len(all_closed), total_pnl)
    return all_closed
