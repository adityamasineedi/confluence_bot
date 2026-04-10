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
_RISK_PER_TRADE  = float(_RISK.get("risk_per_trade",         0.01))
# PnL must exceed 10% of the risked amount to count as a "real win".
# Breakeven / scratched trades (PnL near zero) count as losses for
# the consecutive-loss counter so the circuit breaker isn't fooled.
_MIN_WIN_RATIO   = 0.10
_DB_PATH         = os.environ.get("DB_PATH", "confluence_bot.db")

# In-memory state (fast path — avoids DB hit on every signal)
_tripped:          bool  = False
_trip_reason:      str   = ""
_last_reset_date:  str   = ""   # "YYYY-MM-DD"
_reset_override:   bool  = False  # manual reset suppresses re-evaluation
_consec_at_reset:  int   = 0     # consecutive losses when reset was pressed


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
    """Return (daily_pnl, consecutive_losses) from DB for today UTC.

    Consecutive-loss counting treats breakeven / scratched trades as
    losses.  A trade only counts as a "real win" (streak-breaker) when
    its PnL exceeds 10 % of the risked amount.  If risk_usdt is not
    recorded (legacy rows store 0), we fall back to
    balance × risk_per_trade × _MIN_WIN_RATIO as the threshold.
    """
    today = _today_utc()
    try:
        with sqlite3.connect(_DB_PATH) as conn:
            # Daily PnL: sum of all closed trades today
            row = conn.execute(
                "SELECT COALESCE(SUM(pnl_usdt), 0.0) FROM trades "
                "WHERE status IN ('FILLED','CLOSED') AND DATE(closed_ts) = ?",
                (today,)
            ).fetchone()
            daily_pnl = float(row[0]) if row else 0.0

            # Consecutive losses: walk back from most recent closed trade
            rows = conn.execute(
                "SELECT pnl_usdt, risk_usdt FROM trades "
                "WHERE status IN ('FILLED','CLOSED') "
                "ORDER BY closed_ts DESC LIMIT 20"
            ).fetchall()

            # Fallback min-win when risk_usdt is 0 / NULL on legacy rows.
            # Real win = PnL >= 10% of risked capital.  $1 absolute floor.
            balance = _get_balance()
            fallback_min_win = max(balance * _RISK_PER_TRADE * _MIN_WIN_RATIO, 1.0)

            consec = 0
            for pnl_val, risk_val in rows:
                pnl  = float(pnl_val) if pnl_val else 0.0
                risk = float(risk_val) if risk_val else 0.0
                # Threshold for "real" win/loss — scratch trades in between
                # do not affect the streak in either direction.
                threshold = (risk * _MIN_WIN_RATIO) if risk > 0 else fallback_min_win
                if pnl >= threshold:
                    break          # genuine win — streak ends
                if pnl > -threshold:
                    continue       # near-breakeven scratch (e.g. max_hold timeout)
                consec += 1        # genuine loss

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
    global _tripped, _trip_reason, _reset_override, _consec_at_reset

    daily_pnl, consec = _query_daily_stats()

    # If manually reset, only re-trip if NEW losses occurred since reset
    if _reset_override:
        if consec > _consec_at_reset:
            _reset_override = False  # new loss since reset — allow re-evaluation
            # But only trip on the NEW losses, not the old ones
            consec = consec - _consec_at_reset
        else:
            return False  # still within the reset grace period

    # Need balance for % check
    balance = _get_balance()

    if daily_pnl < 0:
        loss = abs(daily_pnl)
        if balance > 0 and loss / balance * 100 >= _MAX_LOSS_PCT:
            _trip_reason = (f"Daily loss ${loss:.2f} = "
                            f"{loss/balance*100:.1f}% >= {_MAX_LOSS_PCT}% limit")
            _tripped = True
        elif _MAX_LOSS_USDT > 0 and loss >= _MAX_LOSS_USDT:
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
    # 1. Try cache (fastest, always current)
    try:
        from data.cache import _global_cache
        if _global_cache is not None:
            bal = _global_cache.get_account_balance()
            if bal > 0:
                return bal
    except Exception:
        pass
    # 2. Fall back to DB (persisted across restarts)
    try:
        with sqlite3.connect(_DB_PATH) as conn:
            row = conn.execute(
                "SELECT value FROM bot_state WHERE key='account_balance' LIMIT 1"
            ).fetchone()
            if row and float(row[0]) > 0:
                return float(row[0])
    except Exception:
        pass
    return 0.0


def reset() -> dict:
    """Manually clear the circuit breaker. Stays cleared until next trip condition."""
    global _tripped, _trip_reason, _reset_override, _consec_at_reset
    _, consec = _query_daily_stats()
    _tripped          = False
    _trip_reason      = ""
    _reset_override   = True   # suppress re-evaluation until a new loss occurs
    _consec_at_reset  = consec  # remember current loss count
    log.warning("Circuit breaker manually reset via API (consec=%d)", consec)
    return status()


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
