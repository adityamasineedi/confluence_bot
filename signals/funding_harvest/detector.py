"""Funding rate harvest — signal detection.

Binance Futures funding settles every 8 hours: 00:00, 08:00, 16:00 UTC.

When funding is extreme:
- Positive rate (longs pay shorts) → enter SHORT before settlement to collect
- Negative rate (shorts pay longs) → enter LONG before settlement to collect

This is NOT a directional bet.  The income from the funding payment partially
or fully offsets an adverse price move.  We use a tight SL (0.5%) and a modest
TP (0.8%) to minimise directional exposure while capturing the income.

Key metrics
-----------
For a 0.1% funding rate and 0.5% SL:
    break-even: price must move < (funding/sl_pct) × sl% = 20% of the SL distance
    Expected EV per trade > 0 if WR > 1/(1+RR) = 40% (easily achievable at 0.1%+ rates)

All functions are pure.
"""
from datetime import datetime, timezone

# Funding settlement hours (UTC)
SETTLEMENT_HOURS: list[int] = [0, 8, 16]


def secs_to_next_settlement(ts_ms: int) -> float:
    """Seconds until the next funding settlement from ts_ms."""
    from datetime import timedelta
    dt = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
    for h in SETTLEMENT_HOURS:
        candidate = dt.replace(hour=h, minute=0, second=0, microsecond=0)
        if candidate > dt:
            return (candidate - dt).total_seconds()
    # Wrap to next day's first settlement
    tomorrow = dt.replace(hour=0, minute=0, second=0, microsecond=0)
    from datetime import timedelta
    tomorrow += timedelta(days=1)
    return (tomorrow - dt).total_seconds()


def settlement_in_window(ts_ms: int, entry_mins: int, exit_mins: int) -> bool:
    """True when we are within [entry_mins, 0] minutes before settlement
    OR within [0, exit_mins] minutes after settlement.
    """
    secs = secs_to_next_settlement(ts_ms)
    secs_entry = entry_mins * 60
    # Also check if we're within exit_mins *after* the last settlement
    secs_since = _secs_since_last_settlement(ts_ms)
    return secs <= secs_entry or secs_since <= exit_mins * 60


def _secs_since_last_settlement(ts_ms: int) -> float:
    """Seconds elapsed since the most recent funding settlement."""
    dt = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
    current_secs = dt.hour * 3600 + dt.minute * 60 + dt.second
    # Find how many seconds ago the most recent settlement was
    past_settlements = [h * 3600 for h in SETTLEMENT_HOURS if h * 3600 <= current_secs]
    if not past_settlements:
        return current_secs + (24 - SETTLEMENT_HOURS[-1]) * 3600
    last = max(past_settlements)
    return current_secs - last


def funding_direction(rate: float, min_rate: float) -> str | None:
    """Return trade direction based on funding rate, or None if not extreme enough.

    Positive funding (longs pay shorts) → SHORT to collect from longs.
    Negative funding (shorts pay longs) → LONG to collect from shorts.
    """
    if rate >= min_rate:
        return "SHORT"
    if rate <= -min_rate:
        return "LONG"
    return None


def compute_levels(
    direction: str,
    entry:     float,
    sl_pct:    float,
    tp_pct:    float,
) -> tuple[float, float]:
    """Return (stop_loss, take_profit) for a funding harvest entry."""
    if direction == "LONG":
        sl = round(entry * (1.0 - sl_pct), 8)
        tp = round(entry * (1.0 + tp_pct), 8)
    else:
        sl = round(entry * (1.0 + sl_pct), 8)
        tp = round(entry * (1.0 - tp_pct), 8)
    return sl, tp
