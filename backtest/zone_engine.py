"""HTF Demand/Supply Zone backtest engine — 4H origin-of-move zone retests.

Entry  : close of the 1H confirmation candle inside the zone
SL     : zone_low × (1 - 0.002) for LONG, zone_high × (1 + 0.002) for SHORT
TP     : entry ± (entry - SL) × 2.5
Hold   : up to 12 × 1H bars (12h max)
"""
import logging
import os
import yaml

log = logging.getLogger(__name__)

_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "config.yaml")
with open(_CONFIG_PATH) as _f:
    _cfg = yaml.safe_load(_f)

_LOOKBACK_4H     = 100
_BASE_MIN_BARS   = 2
_BASE_RANGE_PCT  = 0.008    # max 0.8% range per bar in base
_IMPULSE_PCT     = 0.020    # ≥ 2% impulse after base
_IMPULSE_BARS    = 3
_ZONE_BUFFER_PCT = 0.005
_MIN_ZONE_AGE    = 3
_MAX_ZONE_AGE    = 80
_COOLDOWN_BARS   = 12       # 12 × 1H = 12h cooldown
_MAX_HOLD        = 12       # 12 × 1H bars = 12h max hold
_RR              = 2.5
_SL_BUFFER       = 0.002


def _ts_str(ts_ms: int) -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


def _find_zones(bars_4h: list[dict], bullish: bool) -> list[dict]:
    """Find demand (bullish=True) or supply (bullish=False) zones."""
    zones = []
    n = len(bars_4h)
    i = 0
    while i < n - _BASE_MIN_BARS - _IMPULSE_BARS:
        base_start = i
        base_end   = i
        while base_end < n - 1:
            bar = bars_4h[base_end]
            rng = (bar["h"] - bar["l"]) / bar["c"] if bar["c"] > 0 else 1.0
            if rng > _BASE_RANGE_PCT:
                break
            base_end += 1

        base_len = base_end - base_start
        if base_len < _BASE_MIN_BARS:
            i += 1
            continue

        base_low  = min(bars_4h[j]["l"] for j in range(base_start, base_end))
        base_high = max(bars_4h[j]["h"] for j in range(base_start, base_end))
        base_mid  = (base_low + base_high) / 2

        for k in range(base_end, min(base_end + _IMPULSE_BARS, n)):
            if bullish:
                move = (bars_4h[k]["h"] - base_mid) / base_mid
            else:
                move = (base_mid - bars_4h[k]["l"]) / base_mid

            if move >= _IMPULSE_PCT:
                age = n - 1 - k
                if _MIN_ZONE_AGE <= age <= _MAX_ZONE_AGE:
                    post_bars = bars_4h[k + 1:]
                    if bullish:
                        retested = any(b["c"] < base_low for b in post_bars)
                    else:
                        retested = any(b["c"] > base_high for b in post_bars)
                    if not retested:
                        zones.append({
                            "low":      base_low,
                            "high":     base_high,
                            "age_bars": age,
                        })
                break

        i = base_end + 1
    return zones


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
    """Run zone retest backtest on 1H bars (confirmed by 4H zone detection).
    Returns list of closed trades."""
    all_closed: list[dict] = []
    warmup_1h = 20
    warmup_4h = _LOOKBACK_4H

    for sym in symbols:
        bars_1h = ohlcv.get(f"{sym}:1h", [])
        bars_4h = ohlcv.get(f"{sym}:4h", [])

        if len(bars_1h) < warmup_1h + 10:
            log.warning("Zone: insufficient 1h data for %s (%d bars)", sym, len(bars_1h))
            continue
        if len(bars_4h) < warmup_4h:
            log.warning("Zone: insufficient 4h data for %s (%d bars)", sym, len(bars_4h))
            continue

        equity    = starting_capital
        eval_bars = bars_1h[warmup_1h:]
        log.info("Zone backtest %s: %d × 1H bars (%s → %s)",
                 sym, len(eval_bars), _ts_str(eval_bars[0]["ts"]), _ts_str(eval_bars[-1]["ts"]))

        open_trade:     dict | None = None
        cooldown_until: int         = 0

        for bar_idx, bar in enumerate(eval_bars):
            global_idx = warmup_1h + bar_idx

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

            # ── Get 4H bars available at this timestamp ────────────────────────
            bar_ts = bar["ts"]
            bars_4h_now = [b for b in bars_4h if b["ts"] <= bar_ts]
            if len(bars_4h_now) < warmup_4h:
                continue

            price = bar["c"]

            # ── Check demand zones (LONG) ──────────────────────────────────────
            direction = None
            sl = tp = 0.0

            demand_zones = _find_zones(bars_4h_now, bullish=True)
            for zone in demand_zones:
                in_zone = (zone["low"] * (1 - _ZONE_BUFFER_PCT) <= price
                           <= zone["high"] * (1 + _ZONE_BUFFER_PCT))
                if not in_zone:
                    continue
                # 1H confirmation: current bar is bullish
                if bar["c"] <= bar["o"]:
                    continue
                # Not too far above zone
                if bar["c"] > zone["high"] * (1 + _ZONE_BUFFER_PCT):
                    continue
                direction = "LONG"
                sl = zone["low"] * (1 - _SL_BUFFER)
                dist = price - sl
                if dist > 0:
                    tp = price + dist * _RR
                    break
                direction = None

            # ── Check supply zones (SHORT) ─────────────────────────────────────
            if direction is None:
                supply_zones = _find_zones(bars_4h_now, bullish=False)
                for zone in supply_zones:
                    in_zone = (zone["low"] * (1 - _ZONE_BUFFER_PCT) <= price
                               <= zone["high"] * (1 + _ZONE_BUFFER_PCT))
                    if not in_zone:
                        continue
                    if bar["c"] >= bar["o"]:
                        continue
                    if bar["c"] < zone["low"] * (1 - _ZONE_BUFFER_PCT):
                        continue
                    direction = "SHORT"
                    sl = zone["high"] * (1 + _SL_BUFFER)
                    dist = sl - price
                    if dist > 0:
                        tp = price - dist * _RR
                        break
                    direction = None

            if direction is None or sl == 0.0 or tp == 0.0:
                continue

            risk_amount = round(equity * risk_pct, 4)
            open_trade = {
                "symbol":      sym,
                "regime":      "ZONE",
                "direction":   direction,
                "entry":       price,
                "sl":          sl,
                "tp":          tp,
                "rr":          _RR,
                "entry_ts":    bar["ts"],
                "bar_idx":     bar_idx,
                "risk_amount": risk_amount,
                "score":       0.75,
            }

        if open_trade is not None:
            last_bar = eval_bars[-1]
            result = _force_close(open_trade, last_bar)
            equity += result["pnl"]
            result["equity_after"] = round(equity, 4)
            all_closed.append(result)

    total_pnl = sum(t["pnl"] for t in all_closed)
    avg_sym_ret = (total_pnl / starting_capital / max(len(symbols), 1)) * 100
    log.info("Zone backtest complete: %d trades  total_pnl=$%.2f  avg_return=%.1f%%",
             len(all_closed), total_pnl, avg_sym_ret)
    return all_closed
