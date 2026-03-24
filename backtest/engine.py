"""Backtest engine — replays history bar-by-bar through the live scoring pipeline.

Capital compounds: each trade risks `risk_pct` of the current equity.
At 2.5 RR a win returns risk × 2.5; a loss costs exactly risk.
ATR-based stops from rr_calculator determine entry/SL/TP price levels;
the dollar risk is always the percentage of current equity, not a fixed amount.
"""
import asyncio
import logging

log = logging.getLogger(__name__)

_RR_RATIO           = 2.5    # must match config.yaml risk.rr_ratio
_PUMP_RR_MAX        = 5.0    # PUMP regime: let winners run to 5× before hard cap
_MAX_HOLD_BARS      = 48     # force-close after 48 × 1h bars (2 days)
_WARMUP_BARS        = 210    # bars to skip for indicator warmup
_TRAIL_ACTIVATE_RR  = 1.0    # trailing stop activates once 1R in profit (locks in 0R min)
_TRAIL_DIST_MULT    = 1.0    # trailing distance = 1× stop dist


def _update_trailing(trade: dict, bar: dict) -> None:
    """Update trailing stop in-place for PUMP trades.

    Activates once the trade is _TRAIL_ACTIVATE_RR × risk in profit.
    Tracks the extreme price (high for LONG, low for SHORT) and ratchets
    the stop up / down — never in the adverse direction.
    """
    if not trade.get("trailing"):
        return

    stop_dist = trade["trail_dist"]

    if trade["direction"] == "LONG":
        trade["trail_extreme"] = max(trade.get("trail_extreme", trade["entry"]), bar["h"])
        # Only activate once price has moved TRAIL_ACTIVATE_RR × stop_dist above entry
        if trade["trail_extreme"] >= trade["entry"] + stop_dist * _TRAIL_ACTIVATE_RR:
            new_sl = trade["trail_extreme"] - stop_dist * _TRAIL_DIST_MULT
            if new_sl > trade["sl"]:
                trade["sl"] = round(new_sl, 8)
    else:
        trade["trail_extreme"] = min(trade.get("trail_extreme", trade["entry"]), bar["l"])
        if trade["trail_extreme"] <= trade["entry"] - stop_dist * _TRAIL_ACTIVATE_RR:
            new_sl = trade["trail_extreme"] + stop_dist * _TRAIL_DIST_MULT
            if new_sl < trade["sl"]:
                trade["sl"] = round(new_sl, 8)


def _check_exit(trade: dict, bar: dict) -> dict | None:
    """Check SL/TP hit using bar high/low. SL wins on simultaneous hit."""
    h, l   = bar["h"], bar["l"]
    risk   = trade["risk_amount"]
    rr     = _PUMP_RR_MAX if trade.get("trailing") else _RR_RATIO

    if trade["direction"] == "LONG":
        sl_hit = l <= trade["sl"]
        tp_hit = h >= trade["tp"]
    else:
        sl_hit = h >= trade["sl"]
        tp_hit = l <= trade["tp"]

    if sl_hit and tp_hit:
        sl_hit, tp_hit = True, False   # worst-case: SL first

    if sl_hit:
        # For trailing trades, calculate actual PnL from current SL (not entry)
        if trade.get("trailing") and trade["sl"] > trade["entry"] and trade["direction"] == "LONG":
            stop_dist = trade.get("trail_dist") or abs(trade["entry"] - trade["sl"])
            actual_pnl = (trade["sl"] - trade["entry"]) / stop_dist * risk if stop_dist > 0 else -risk
            actual_pnl = max(actual_pnl, -risk)
            return {**trade, "exit_ts": bar["ts"], "outcome": "WIN" if actual_pnl > 0 else "LOSS",
                    "pnl": round(actual_pnl, 2)}
        return {**trade, "exit_ts": bar["ts"], "outcome": "LOSS",
                "pnl": round(-risk, 2)}
    if tp_hit:
        return {**trade, "exit_ts": bar["ts"], "outcome": "WIN",
                "pnl": round(risk * rr, 2)}
    return None


def _force_close(trade: dict, bar: dict) -> dict:
    """Close at current bar close (max-hold timeout). PnL scales with risk_amount."""
    entry     = trade["entry"]
    close     = bar["c"]
    risk      = trade["risk_amount"]

    if trade["direction"] == "LONG":
        pct = (close - entry) / entry
    else:
        pct = (entry - close) / entry

    stop_dist = abs(entry - trade["sl"])
    pnl = (pct * entry / stop_dist * risk) if stop_dist > 0 else 0.0
    # Cap loss at risk_amount (stop was never hit, but price may have gapped through)
    pnl = max(pnl, -risk)

    return {**trade, "exit_ts": bar["ts"], "outcome": "TIMEOUT",
            "pnl": round(pnl, 2)}


async def run(
    symbols:          list[str],
    ohlcv:            dict[str, list[dict]],
    oi:               dict[str, list[dict]],
    funding:          dict[str, list[dict]],
    warmup_bars:      int   = _WARMUP_BARS,
    starting_capital: float = 1_000.0,
    risk_pct:         float = 0.02,          # 2 % of equity per trade
) -> list[dict]:
    """
    Run the full backtest.  Returns a list of closed trade dicts.

    Each trade dict contains:
        symbol, regime, direction, entry, sl, tp,
        entry_ts, exit_ts, outcome (WIN/LOSS/TIMEOUT),
        pnl, risk_amount, equity_after,
        score, signals
    """
    from backtest.cache import BacktestCache
    from core.regime_detector import RegimeDetector
    import core.rr_calculator as rr_calc
    from backtest.scorer import (
        score_trend_long, score_trend_short,
        score_range_long, score_range_short,
        score_crash,
        score_pump,
        score_breakout_long, score_breakout_short,
    )

    cache    = BacktestCache(ohlcv, oi, funding)
    detector = RegimeDetector()
    equity   = starting_capital

    anchor_key = f"{symbols[0]}:1h"
    all_1h     = ohlcv.get(anchor_key, [])
    if len(all_1h) < warmup_bars + 10:
        log.error("Insufficient 1h data (need >%d bars, got %d)", warmup_bars, len(all_1h))
        return []

    eval_bars = all_1h[warmup_bars:]
    log.info("Backtesting %d bars (%s -> %s) for %s  |  capital=$%.2f  risk=%.0f%%",
             len(eval_bars),
             _ts_str(eval_bars[0]["ts"]),
             _ts_str(eval_bars[-1]["ts"]),
             symbols, starting_capital, risk_pct * 100)

    open_trades:   list[dict] = []
    closed_trades: list[dict] = []
    bar_count = 0

    # Consecutive-loss cooldown: after 2 back-to-back losses on the same symbol,
    # pause new entries for 48 bars (2 days) to avoid chasing bad conditions.
    _COOLDOWN_BARS   = 48
    consec_losses:   dict[str, int] = {s: 0 for s in symbols}
    cooldown_until:  dict[str, int] = {s: 0 for s in symbols}   # bar_ts epoch ms

    for bar in eval_bars:
        bar_ts = bar["ts"]
        cache.advance(bar_ts)
        bar_count += 1

        if bar_count % 1000 == 0:
            log.info("  progress: %d / %d bars  open=%d  closed=%d  equity=$%.2f",
                     bar_count, len(eval_bars),
                     len(open_trades), len(closed_trades), equity)

        # ── Resolve open trades ───────────────────────────────────────────────
        still_open: list[dict] = []
        for trade in open_trades:
            sym      = trade["symbol"]
            sym_bar  = cache.get_ohlcv(sym, window=1, tf="1h")
            if not sym_bar:
                still_open.append(trade)
                continue

            current_bar = sym_bar[-1]
            _update_trailing(trade, current_bar)   # ratchet SL for PUMP trades
            result      = _check_exit(trade, current_bar)

            if result is not None:
                equity += result["pnl"]
                result["equity_after"] = round(equity, 2)
                closed_trades.append(result)
                # Track consecutive losses for cooldown
                if result["outcome"] == "LOSS":
                    consec_losses[sym] = consec_losses.get(sym, 0) + 1
                    if consec_losses[sym] >= 2:
                        cooldown_until[sym] = bar_ts + _COOLDOWN_BARS * 3_600_000
                        log.debug("Cooldown on %s until %s (2 consecutive losses)",
                                  sym, _ts_str(cooldown_until[sym]))
                else:
                    consec_losses[sym] = 0   # any win/timeout resets streak
            elif bar_ts - trade["entry_ts"] >= _MAX_HOLD_BARS * 3_600_000:
                closed = _force_close(trade, current_bar)
                equity += closed["pnl"]
                closed["equity_after"] = round(equity, 2)
                closed_trades.append(closed)
                consec_losses[sym] = 0   # timeout resets streak
            else:
                still_open.append(trade)

        open_trades = still_open

        # ── Score each symbol ─────────────────────────────────────────────────
        for symbol in symbols:
            if any(t["symbol"] == symbol for t in open_trades):
                continue

            # Skip if symbol is in consecutive-loss cooldown
            if cooldown_until.get(symbol, 0) > bar_ts:
                continue

            try:
                regime = detector.detect(symbol, cache)

                if regime == "TREND":
                    candidates = [
                        await score_trend_long(symbol, cache),
                        await score_trend_short(symbol, cache),
                    ]
                elif regime == "RANGE":
                    candidates = [
                        await score_range_long(symbol, cache),
                        await score_range_short(symbol, cache),
                    ]
                elif regime == "CRASH":
                    candidates = [await score_crash(symbol, cache)]
                elif regime == "PUMP":
                    candidates = [await score_pump(symbol, cache)]
                elif regime == "BREAKOUT":
                    bdir = detector.get_breakout_direction(symbol)
                    if bdir == "LONG":
                        candidates = [await score_breakout_long(symbol, cache)]
                    elif bdir == "SHORT":
                        candidates = [await score_breakout_short(symbol, cache)]
                    else:
                        continue
                else:
                    continue

            except Exception as exc:
                log.debug("Pipeline error %s @ %s: %s", symbol, _ts_str(bar_ts), exc)
                continue

            for score_dict in candidates:
                if not score_dict.get("fire"):
                    continue

                direction = score_dict["direction"]
                try:
                    entry, sl, tp = rr_calc.compute(symbol, direction, cache)
                except Exception:
                    continue

                if entry == 0.0 or sl == 0.0 or tp == 0.0:
                    continue
                if abs(entry - sl) == 0.0:
                    continue

                risk_amount = round(equity * risk_pct, 4)
                regime_str  = score_dict["regime"]

                # PUMP and BREAKOUT trades use trailing stop + extended TP cap
                use_trailing = regime_str in ("PUMP", "BREAKOUT")
                stop_dist    = abs(entry - sl)

                trade_rec = {
                    "symbol":      symbol,
                    "regime":      regime_str,
                    "direction":   direction,
                    "entry":       entry,
                    "sl":          sl,
                    "tp":          entry + stop_dist * _PUMP_RR_MAX if use_trailing and direction == "LONG"
                                   else entry - stop_dist * _PUMP_RR_MAX if use_trailing
                                   else tp,
                    "entry_ts":    bar_ts,
                    "score":       score_dict["score"],
                    "signals":     score_dict.get("signals", {}),
                    "risk_amount": risk_amount,
                    "trailing":    use_trailing,
                    "trail_dist":  stop_dist,
                    "trail_extreme": entry,
                }
                open_trades.append(trade_rec)
                break   # one trade per symbol per bar

    # Force-close remaining open trades at the final bar
    for trade in open_trades:
        sym_bar = cache.get_ohlcv(trade["symbol"], window=1, tf="1h")
        if sym_bar:
            closed = _force_close(trade, sym_bar[-1])
            equity += closed["pnl"]
            closed["equity_after"] = round(equity, 2)
            closed_trades.append(closed)

    log.info("Backtest complete: %d trades  final equity=$%.2f  return=%.1f%%",
             len(closed_trades), equity,
             (equity - starting_capital) / starting_capital * 100)
    return closed_trades


def _ts_str(ts_ms: int) -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
