"""Funding rate harvest backtest engine — replays 8h funding windows on 5m history.

For each symbol
---------------
1. Iterate funding rate entries (8h resolution).
2. At each funding settlement, check if |rate| >= min_rate.
3. If yes, find the 5m bar that is `entry_mins_before` minutes before settlement.
4. Enter there; exit at TP / SL or `exit_mins_after` minutes after settlement.
5. Per-symbol cooldown: one trade per settlement window (8h).

Entry/exit timing
-----------------
    entry_bar  : 5m bar with ts = settlement_ts - entry_mins × 60 × 1000
    latest_exit: 5m bar with ts = settlement_ts + exit_mins  × 60 × 1000
    SL/TP      : fixed percentage, checked on every 5m bar between entry and exit

Position sizing
---------------
    risk_amount = equity × risk_pct
    pnl_win     = risk_amount × (tp_pct / sl_pct)
    pnl_loss    = -risk_amount
    pnl_exit    = scaled to actual move vs stop distance
"""
import logging
import os
import yaml
from datetime import datetime, timezone

log = logging.getLogger(__name__)

_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "config.yaml")
with open(_CONFIG_PATH) as _f:
    _cfg = yaml.safe_load(_f)

_FH = _cfg.get("funding_harvest", {})

# Patchable by tuner
_MIN_RATE    = float(_FH.get("min_rate_pct",       0.0005))
_ENTRY_MINS  = int(_FH.get("entry_mins_before",     30))
_EXIT_MINS   = int(_FH.get("exit_mins_after",       15))
_SL_PCT      = float(_FH.get("sl_pct",             0.005))
_TP_PCT      = float(_FH.get("tp_pct",             0.008))
_RR          = _TP_PCT / _SL_PCT


def _ts_str(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


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


def _forced_exit_at(trade: dict, bar: dict) -> dict:
    """Exit at bar close (post-settlement timeout)."""
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
    funding:          dict[str, list[dict]],
    starting_capital: float = 100.0,
    risk_pct:         float = 0.005,   # smaller risk: income strategy
) -> list[dict]:
    """Run the funding harvest backtest.  Returns closed trade list.

    Parameters
    ----------
    funding : dict "SYMBOL" → list[{ts, rate}]  (8h funding history from fetcher)
    """
    from signals.funding_harvest.detector import funding_direction, compute_levels

    _ENTRY_MS   = _ENTRY_MINS * 60 * 1000
    _EXIT_MS    = _EXIT_MINS  * 60 * 1000
    _COOLDOWN_MS = 8 * 3600 * 1000   # one trade per 8h window per symbol

    all_closed: list[dict] = []
    equity = starting_capital

    for sym in symbols:
        fund_history = funding.get(sym, [])
        bars_5m      = ohlcv.get(f"{sym}:5m", [])

        if len(fund_history) < 5 or len(bars_5m) < 20:
            log.warning("FundingHarvest: insufficient data for %s", sym)
            continue

        # Build O(1) timestamp → bar index for 5m bars
        bar_index = {b["ts"]: i for i, b in enumerate(bars_5m)}

        last_trade_ts: int = 0   # ms of last trade's settlement

        log.info("FundingHarvest backtest %s: %d funding events", sym, len(fund_history))

        for fund_event in fund_history:
            settle_ts = int(fund_event["ts"])
            rate      = float(fund_event["rate"])

            # Respect cooldown
            if settle_ts - last_trade_ts < _COOLDOWN_MS:
                continue

            # Check funding direction
            direction = funding_direction(rate, _MIN_RATE)
            if direction is None:
                continue

            # Find entry bar (approximately entry_mins before settlement)
            entry_ts = settle_ts - _ENTRY_MS
            # Find nearest 5m bar >= entry_ts
            entry_bar_idx = next(
                (i for i, b in enumerate(bars_5m) if b["ts"] >= entry_ts),
                None,
            )
            if entry_bar_idx is None:
                continue

            # Compute SL/TP from entry bar close
            entry_bar = bars_5m[entry_bar_idx]
            entry     = entry_bar["c"]
            sl, tp    = compute_levels(direction, entry, _SL_PCT, _TP_PCT)

            risk_amount = round(equity * risk_pct, 4)
            trade = {
                "symbol":       sym,
                "regime":       "FUNDING",
                "direction":    direction,
                "entry":        entry,
                "sl":           sl,
                "tp":           tp,
                "entry_ts":     entry_bar["ts"],
                "settle_ts":    settle_ts,
                "risk_amount":  risk_amount,
                "funding_rate": round(rate * 100, 4),
                "score":        0.75,
                "signals": {
                    "rate_extreme": True,
                    "in_window":    True,
                    "cooldown_ok":  True,
                },
            }

            # Simulate bar-by-bar from entry to max exit time
            exit_ts = settle_ts + _EXIT_MS
            closed  = None

            for bar in bars_5m[entry_bar_idx + 1:]:
                if bar["ts"] > exit_ts:
                    closed = _forced_exit_at(trade, bars_5m[
                        max(0, bar_index.get(exit_ts, entry_bar_idx + 1) - 1)
                    ])
                    break
                result = _check_exit(trade, bar)
                if result is not None:
                    closed = result
                    break

            if closed is None:
                # Hit end of data
                last_bar_in_range = next(
                    (b for b in reversed(bars_5m) if b["ts"] <= exit_ts), None
                )
                if last_bar_in_range:
                    closed = _forced_exit_at(trade, last_bar_in_range)

            if closed:
                equity += closed["pnl"]
                closed["equity_after"] = round(equity, 4)
                all_closed.append(closed)
                last_trade_ts = settle_ts

    log.info(
        "FundingHarvest backtest complete: %d trades  final=$%.2f  return=%.1f%%",
        len(all_closed), equity,
        (equity - starting_capital) / starting_capital * 100,
    )
    return all_closed
