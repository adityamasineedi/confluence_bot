"""Session open trap backtest engine — bar-by-bar replay on 5m OHLCV history.

For each symbol
---------------
1. Iterate 5m bars chronologically.
2. When a bar opens at a session hour (01:00, 08:00, or 13:00 UTC):
   accumulate the next 3 bars (15 min window).
3. At bar 3: call measure_fake_move().  If |fake_move| >= min_move_pct:
   enter the fade at bar-3 close.
4. Manage the open trade bar-by-bar: SL/TP hit or max_hold timeout.
5. Per-symbol × per-session cooldown: one trade per session window per symbol.

Position sizing
---------------
    risk_amount = equity × risk_pct
    pnl_win     = risk_amount × rr_ratio
    pnl_loss    = -risk_amount
    pnl_timeout = scaled to actual close vs SL distance
"""
import logging
import os
import yaml
from datetime import datetime, timezone

log = logging.getLogger(__name__)

_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "config.yaml")
with open(_CONFIG_PATH) as _f:
    _cfg = yaml.safe_load(_f)

_SS = _cfg.get("session_trap", {})

# Module-level constants (patchable by tuner)
_MIN_MOVE_PCT    = float(_SS.get("min_move_pct",    0.003))
_STRONG_MOVE_PCT = float(_SS.get("strong_move_pct", 0.006))
_MAX_RANGE_PCT   = float(_SS.get("max_range_pct",   0.015))
_SL_BUFFER_PCT   = float(_SS.get("sl_buffer_pct",   0.002))
_RR_RATIO        = float(_SS.get("rr_ratio",         1.5))
_MAX_HOLD        = int(_SS.get("max_hold_bars",       12))    # 12 × 5m = 1h
_COOLDOWN_BARS   = int(_SS.get("cooldown_mins",        60) // 5)
_SESSIONS        = list(_SS.get("sessions", [1, 8, 13]))


def _ts_str(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


def _bar_hour_minute(ts_ms: int) -> tuple[int, int]:
    dt = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
    return dt.hour, dt.minute


def _check_exit(trade: dict, bar: dict) -> dict | None:
    risk = trade["risk_amount"]
    if trade["direction"] == "LONG":
        if bar["l"] <= trade["sl"]:
            return {**trade, "exit_ts": bar["ts"], "outcome": "LOSS",
                    "pnl": round(-risk, 4)}
        if bar["h"] >= trade["tp"]:
            return {**trade, "exit_ts": bar["ts"], "outcome": "WIN",
                    "pnl": round(risk * _RR_RATIO, 4)}
    else:
        if bar["h"] >= trade["sl"]:
            return {**trade, "exit_ts": bar["ts"], "outcome": "LOSS",
                    "pnl": round(-risk, 4)}
        if bar["l"] <= trade["tp"]:
            return {**trade, "exit_ts": bar["ts"], "outcome": "WIN",
                    "pnl": round(risk * _RR_RATIO, 4)}
    return None


def _force_close(trade: dict, bar: dict) -> dict:
    entry   = trade["entry"]
    close   = bar["c"]
    risk    = trade["risk_amount"]
    sl_dist = abs(entry - trade["sl"])
    if sl_dist <= 0.0:
        return {**trade, "exit_ts": bar["ts"], "outcome": "TIMEOUT", "pnl": 0.0}
    if trade["direction"] == "LONG":
        pct_move = (close - entry) / entry
    else:
        pct_move = (entry - close) / entry
    pnl = pct_move / (sl_dist / entry) * risk
    pnl = max(pnl, -risk)
    return {**trade, "exit_ts": bar["ts"], "outcome": "TIMEOUT", "pnl": round(pnl, 4)}


def run(
    symbols:          list[str],
    ohlcv:            dict[str, list[dict]],
    starting_capital: float = 100.0,
    risk_pct:         float = 0.01,
) -> list[dict]:
    """Run the session trap backtest.  Returns closed trade list."""
    from signals.session.detector import measure_fake_move, compute_levels

    all_closed: list[dict] = []
    equity = starting_capital

    for sym in symbols:
        bars = ohlcv.get(f"{sym}:5m", [])
        if len(bars) < 20:
            log.warning("SessionTrap: insufficient 5m data for %s (%d bars)", sym, len(bars))
            continue

        log.info("SessionTrap backtest %s: %d bars (%s → %s)",
                 sym, len(bars), _ts_str(bars[0]["ts"]), _ts_str(bars[-1]["ts"]))

        open_trade:     dict | None     = None
        cooldown_until: dict[int, int]  = {h: 0 for h in _SESSIONS}  # session → bar_idx
        # Pending session window: {session_hour: [bar, ...]}
        session_window: dict[int, list] = {}

        for bar_idx, bar in enumerate(bars):
            # ── 1. Resolve open trade ─────────────────────────────────────────
            if open_trade is not None:
                result = _check_exit(open_trade, bar)
                if result is not None:
                    equity += result["pnl"]
                    result["equity_after"] = round(equity, 4)
                    all_closed.append(result)
                    cooldown_until[open_trade["session_hour"]] = bar_idx + _COOLDOWN_BARS
                    open_trade = None
                elif bar_idx - open_trade["bar_idx"] >= _MAX_HOLD:
                    result = _force_close(open_trade, bar)
                    equity += result["pnl"]
                    result["equity_after"] = round(equity, 4)
                    all_closed.append(result)
                    cooldown_until[open_trade["session_hour"]] = bar_idx + _COOLDOWN_BARS
                    open_trade = None
                else:
                    continue   # already in a trade, don't look for new entries

            # ── 2. Accumulate session windows ─────────────────────────────────
            h, m = _bar_hour_minute(bar["ts"])

            # Start a new window when we hit a session open bar
            if m == 0 and h in _SESSIONS:
                session_window[h] = [bar]
            elif m in (5, 10) and h in _SESSIONS:
                if h in session_window and len(session_window[h]) < 3:
                    session_window[h].append(bar)

            # ── 3. At the 15-min mark: evaluate and potentially enter ──────────
            if m == 15 and h in _SESSIONS and h in session_window:
                window = session_window.pop(h)
                if len(window) < 3:
                    continue
                if bar_idx < cooldown_until.get(h, 0):
                    continue

                setup = measure_fake_move(window)
                if setup is None:
                    continue

                fake_abs  = abs(setup["fake_move"])
                range_pct = (setup["session_high"] - setup["session_low"]) / setup["open_price"]

                if fake_abs < _MIN_MOVE_PCT or range_pct > _MAX_RANGE_PCT:
                    continue

                # Entry at close of bar-3 (the current 15-min bar)
                setup["close_entry"] = bar["c"]
                sl, tp = compute_levels(setup, _SL_BUFFER_PCT, _RR_RATIO)
                entry  = bar["c"]

                risk_amount = round(equity * risk_pct, 4)
                open_trade  = {
                    "symbol":       sym,
                    "regime":       "SESSION",
                    "direction":    setup["direction"],
                    "entry":        entry,
                    "sl":           sl,
                    "tp":           tp,
                    "entry_ts":     bar["ts"],
                    "bar_idx":      bar_idx,
                    "risk_amount":  risk_amount,
                    "session_hour": h,
                    "fake_move":    round(setup["fake_move"] * 100, 3),
                    "score":        0.75,
                    "signals": {
                        "fake_move_ok": True,
                        "spread_tight": True,
                        "cooldown_ok":  True,
                    },
                }

        # Force-close any open trade at end of data
        if open_trade is not None:
            result = _force_close(open_trade, bars[-1])
            equity += result["pnl"]
            result["equity_after"] = round(equity, 4)
            all_closed.append(result)

    log.info(
        "SessionTrap backtest complete: %d trades  final=$%.2f  return=%.1f%%",
        len(all_closed), equity,
        (equity - starting_capital) / starting_capital * 100,
    )
    return all_closed
