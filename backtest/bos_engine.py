"""BOS/CHoCH backtest engine — bar-by-bar replay on 1H OHLCV.

Strategy
--------
- Detect confirmed swing highs/lows (pivot_n bars each side).
- BOS LONG: close > last swing_high AND prior_close < swing_high + volume spike.
- BOS SHORT: close < last swing_low AND prior_close > swing_low + volume spike.
- 4H HTF structure alignment (HH+HL for LONG, LH+LL for SHORT).
- Block entry if price already extended > 2% beyond break level.

Entry  : close of the break bar
SL     : prior swing point ± sl_buffer  (0.1% beyond the anchor)
TP     : entry + dist × rr_ratio  (2.5)
"""
import logging
import os
import yaml

log = logging.getLogger(__name__)

_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "config.yaml")
with open(_CONFIG_PATH) as _f:
    _cfg = yaml.safe_load(_f)

_BOS_CFG     = _cfg.get("bos", {})
_PIVOT_N     = int(_BOS_CFG.get("pivot_n",            3))
_LOOKBACK    = int(_BOS_CFG.get("lookback_bars",       50))
_VOL_MULT    = float(_BOS_CFG.get("vol_confirm_mult",  1.3))
_MAX_EXT_PCT = float(_BOS_CFG.get("max_extension_pct", 0.02))
_RR          = float(_BOS_CFG.get("rr_ratio",          2.5))
_SL_BUFFER   = float(_BOS_CFG.get("sl_buffer_pct",     0.001))
_COOLDOWN_BARS = int(_BOS_CFG.get("cooldown_mins",     60))   # 1 bar = 1 hour
_MAX_HOLD      = 48   # 2 days


def _ts_str(ts_ms: int) -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


def _detect_pivots(bars: list[dict], n: int) -> dict:
    """Return {"highs": [...], "lows": [...]} pivot points from bars slice."""
    highs, lows = [], []
    end = len(bars) - n
    for i in range(n, end):
        h = bars[i]["h"]; l = bars[i]["l"]
        if (all(h >= bars[i - j]["h"] for j in range(1, n + 1)) and
                all(h >= bars[i + j]["h"] for j in range(1, n + 1))):
            highs.append(h)
        if (all(l <= bars[i - j]["l"] for j in range(1, n + 1)) and
                all(l <= bars[i + j]["l"] for j in range(1, n + 1))):
            lows.append(l)
    return {"highs": highs, "lows": lows}


def _htf_bullish(bars_4h: list[dict], bar_ts: int, n: int = 2) -> bool:
    """4H structure bullish: last two pivot_highs ascending AND pivot_lows ascending."""
    b4 = [b for b in bars_4h if b["ts"] <= bar_ts]
    if len(b4) < 20:
        return False
    p = _detect_pivots(b4[-40:], n)
    ph, pl = p["highs"], p["lows"]
    return len(ph) >= 2 and len(pl) >= 2 and ph[-1] > ph[-2] and pl[-1] > pl[-2]


def _htf_bearish(bars_4h: list[dict], bar_ts: int, n: int = 2) -> bool:
    b4 = [b for b in bars_4h if b["ts"] <= bar_ts]
    if len(b4) < 20:
        return False
    p = _detect_pivots(b4[-40:], n)
    ph, pl = p["highs"], p["lows"]
    return len(ph) >= 2 and len(pl) >= 2 and ph[-1] < ph[-2] and pl[-1] < pl[-2]


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
    """Run BOS/CHoCH backtest on 1H bars. Returns list of closed trades."""
    all_closed: list[dict] = []
    warmup = _LOOKBACK + _PIVOT_N + 5

    for sym in symbols:
        bars_1h = ohlcv.get(f"{sym}:1h", [])
        bars_4h = ohlcv.get(f"{sym}:4h", [])
        if len(bars_1h) < warmup + 10:
            log.warning("BOS: insufficient 1h data for %s (%d bars)", sym, len(bars_1h))
            continue

        equity         = starting_capital
        eval_bars      = bars_1h[warmup:]
        open_trade: dict | None = None
        cooldown_until = 0

        log.info("BOS backtest %s: %d bars (%s → %s)",
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

            # ── Detect BOS from lookback slice ────────────────────────────────
            lookback_slice = bars_1h[max(0, global_idx - _LOOKBACK - _PIVOT_N): global_idx]
            if len(lookback_slice) < _PIVOT_N * 2 + 5:
                continue

            pivots   = _detect_pivots(lookback_slice[:-1], _PIVOT_N)   # exclude current bar
            ph, pl   = pivots["highs"], pivots["lows"]
            close    = bar["c"]
            prev_close = bars_1h[global_idx - 1]["c"]

            # Volume check
            vol_slice = lookback_slice[-21:-1]
            avg_vol   = sum(b["v"] for b in vol_slice) / len(vol_slice) if vol_slice else 0
            vol_ok    = avg_vol > 0 and bar["v"] >= avg_vol * _VOL_MULT

            direction = None
            sl = tp = 0.0

            # ── BOS LONG: close > last swing high ─────────────────────────────
            if ph and vol_ok and _htf_bullish(bars_4h, bar_ts):
                swing_high = ph[-1]
                if prev_close < swing_high and close > swing_high:
                    extension = (close - swing_high) / swing_high
                    if extension <= _MAX_EXT_PCT:
                        sl_anchor = pl[-1] if pl else swing_high * 0.98
                        sl  = sl_anchor * (1.0 - _SL_BUFFER)
                        dist = abs(close - sl)
                        if dist > 0:
                            tp        = close + dist * _RR
                            direction = "LONG"

            # ── BOS SHORT: close < last swing low ─────────────────────────────
            if direction is None and pl and vol_ok and _htf_bearish(bars_4h, bar_ts):
                swing_low = pl[-1]
                if prev_close > swing_low and close < swing_low:
                    extension = (swing_low - close) / swing_low
                    if extension <= _MAX_EXT_PCT:
                        sl_anchor = ph[-1] if ph else swing_low * 1.02
                        sl  = sl_anchor * (1.0 + _SL_BUFFER)
                        dist = abs(sl - close)
                        if dist > 0:
                            tp        = close - dist * _RR
                            direction = "SHORT"

            if direction is None or sl == 0.0 or tp == 0.0:
                continue

            risk_amount = round(equity * risk_pct, 4)
            open_trade = {
                "symbol":      sym,
                "regime":      "BOS",
                "direction":   direction,
                "entry":       close,
                "sl":          sl,
                "tp":          tp,
                "entry_ts":    bar_ts,
                "bar_idx":     bar_idx,
                "risk_amount": risk_amount,
                "score":       0.75,
            }

        # Force-close remaining
        if open_trade is not None:
            result = _force_close(open_trade, eval_bars[-1])
            equity += result["pnl"]
            result["equity_after"] = round(equity, 4)
            all_closed.append(result)

    total_pnl = sum(t["pnl"] for t in all_closed)
    log.info("BOS backtest complete: %d trades  total_pnl=$%.2f", len(all_closed), total_pnl)
    return all_closed
