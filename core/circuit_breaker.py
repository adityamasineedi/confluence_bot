"""Circuit breaker — halts all new entries when daily loss limits are breached.

Checks (any one trips the breaker):
  1. Daily PnL loss > max_daily_loss_pct  (% of opening balance)
  2. Daily PnL loss > max_daily_loss_usdt (hard USD cap)
  3. Consecutive losing trades >= max_consecutive_losses

State resets at UTC midnight automatically.
Called by executor.py before every order placement.
"""
import logging
import os
import sqlite3
import yaml
from datetime import datetime, timezone

log = logging.getLogger(__name__)

_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "config.yaml")
with open(_CONFIG_PATH) as _f:
    _cfg = yaml.safe_load(_f)

_RISK       = _cfg.get("risk", {})
_MAX_LOSS_PCT    = float(_RISK.get("max_daily_loss_pct",      5.0))
_MAX_LOSS_USDT   = float(_RISK.get("max_daily_loss_usdt",   250.0))
_MAX_CONSEC      = int(_RISK.get("max_consecutive_losses",     4))
_DB_PATH         = os.environ.get("DB_PATH", "confluence_bot.db")

# In-memory state (fast path — avoids DB hit on every signal)
_tripped:          bool  = False
_trip_reason:      str   = ""
_last_reset_date:  str   = ""   # "YYYY-MM-DD"


def _today_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _reset_if_new_day() -> None:
    global _tripped, _trip_reason, _last_reset_date
    today = _today_utc()
    if today != _last_reset_date:
        if _tripped:
            log.info("Circuit breaker reset at UTC midnight (%s)", today)
        _tripped         = False
        _trip_reason     = ""
        _last_reset_date = today


def _query_daily_stats() -> tuple[float, int]:
    """Return (daily_pnl, consecutive_losses) from DB for today UTC."""
    today = _today_utc()
    try:
        with sqlite3.connect(_DB_PATH) as conn:
            # Daily PnL: sum of all closed trades today
            row = conn.execute(
                "SELECT COALESCE(SUM(pnl_usdt), 0.0) FROM trades "
                "WHERE status='FILLED' AND DATE(closed_ts) = ?",
                (today,)
            ).fetchone()
            daily_pnl = float(row[0]) if row else 0.0

            # Consecutive losses: walk back from most recent closed trade
            rows = conn.execute(
                "SELECT pnl_usdt FROM trades WHERE status='FILLED' "
                "ORDER BY closed_ts DESC LIMIT 20"
            ).fetchall()
            consec = 0
            for r in rows:
                if float(r[0]) < 0:
                    consec += 1
                else:
                    break

        return daily_pnl, consec
    except Exception as exc:
        log.debug("circuit_breaker DB query failed: %s", exc)
        return 0.0, 0


def is_tripped() -> bool:
    """Return True if trading should be halted. Fast — checks in-memory flag first."""
    _reset_if_new_day()
    if _tripped:
        return True
    return _evaluate()


def _evaluate() -> bool:
    """Query DB stats and trip breaker if limits exceeded. Returns True if tripped."""
    global _tripped, _trip_reason

    daily_pnl, consec = _query_daily_stats()

    # Need balance for % check
    balance = _get_balance()

    if daily_pnl < 0:
        loss = abs(daily_pnl)
        if balance > 0 and loss / balance * 100 >= _MAX_LOSS_PCT:
            _trip_reason = (f"Daily loss ${loss:.2f} = "
                            f"{loss/balance*100:.1f}% >= {_MAX_LOSS_PCT}% limit")
            _tripped = True
        elif loss >= _MAX_LOSS_USDT:
            _trip_reason = f"Daily loss ${loss:.2f} >= ${_MAX_LOSS_USDT} hard cap"
            _tripped = True

    if consec >= _MAX_CONSEC:
        _trip_reason = f"{consec} consecutive losing trades >= {_MAX_CONSEC} limit"
        _tripped = True

    if _tripped:
        log.warning("CIRCUIT BREAKER TRIPPED: %s — all new entries halted until UTC midnight",
                    _trip_reason)
    return _tripped


def _get_balance() -> float:
    try:
        with sqlite3.connect(_DB_PATH) as conn:
            row = conn.execute(
                "SELECT value FROM bot_state WHERE key='account_balance' LIMIT 1"
            ).fetchone()
            return float(row[0]) if row else 0.0
    except Exception:
        return 0.0


def status() -> dict:
    """Return current circuit breaker state (for dashboard / health check)."""
    _reset_if_new_day()
    daily_pnl, consec = _query_daily_stats()
    balance = _get_balance()
    return {
        "tripped":       _tripped,
        "reason":        _trip_reason,
        "daily_pnl":     round(daily_pnl, 2),
        "daily_loss_pct": round(abs(daily_pnl) / balance * 100, 2) if balance > 0 and daily_pnl < 0 else 0.0,
        "consecutive_losses": consec,
        "limits": {
            "max_daily_loss_pct":  _MAX_LOSS_PCT,
            "max_daily_loss_usdt": _MAX_LOSS_USDT,
            "max_consecutive":     _MAX_CONSEC,
        },
    }
