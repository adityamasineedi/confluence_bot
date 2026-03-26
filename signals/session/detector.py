"""Session open trap — signal detection.

The first 15 minutes of each major session (Asia, London, NY) shows a
predictable pattern: price fakes one direction, sweeps nearby stops, then
reverses.  We detect the fake and enter the fade.

Session opens (UTC):
    Asia:   01:00
    London: 08:00
    NY:     13:00

Detection logic
---------------
1. Identify the 5m bar that opens at the session hour.
2. Accumulate the first 3 × 5m bars (= 15 min window).
3. Compute the "fake move":
       fake_move = (close_bar3 - open_bar1) / open_bar1
4. If |fake_move| >= min_move_pct → fake direction confirmed.
5. Return the fade direction + SL level (session extreme + buffer).

All functions are pure (no cache, no I/O).  The scorer calls them with
pre-sliced bar lists.
"""
from datetime import datetime, timezone

# Session opens (UTC hour, minute)
SESSION_OPENS: list[tuple[int, int]] = [
    (1,  0),   # Asia
    (8,  0),   # London
    (13, 0),   # NY
]

_5M_MS = 5 * 60 * 1000


def bar_session_slot(ts_ms: int) -> tuple[int, int] | None:
    """Return (hour, minute) if ts_ms is a session open bar, else None."""
    dt = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
    slot = (dt.hour, dt.minute)
    return slot if slot in SESSION_OPENS else None


def session_name(hour: int) -> str:
    return {1: "Asia", 8: "London", 13: "NY"}.get(hour, f"{hour:02d}:00")


def measure_fake_move(window_bars: list[dict]) -> dict | None:
    """Analyse the 15-min opening window and return a setup dict, or None.

    Parameters
    ----------
    window_bars : exactly 3 consecutive 5m bars starting at the session open

    Returns
    -------
    dict with keys:
        direction   : "LONG" (fade the dump) or "SHORT" (fade the pump)
        fake_move   : (close_bar3 - open_bar1) / open_bar1  (signed)
        session_high: max high over 3 bars
        session_low : min low over 3 bars
        open_price  : first bar's open
    None if window is incomplete or |fake_move| is below threshold (caller decides threshold).
    """
    if len(window_bars) < 3:
        return None

    open_price   = window_bars[0]["o"]
    close_price  = window_bars[-1]["c"]
    session_high = max(b["h"] for b in window_bars)
    session_low  = min(b["l"] for b in window_bars)

    if open_price <= 0.0:
        return None

    fake_move  = (close_price - open_price) / open_price
    direction  = "LONG" if fake_move < 0 else "SHORT"   # fade the move

    return {
        "direction":    direction,
        "fake_move":    fake_move,
        "session_high": session_high,
        "session_low":  session_low,
        "open_price":   open_price,
    }


def compute_levels(
    setup: dict,
    sl_buffer_pct: float,
    rr_ratio:      float,
) -> tuple[float, float]:
    """Return (stop_loss, take_profit) for a session trap entry at current price.

    SL is anchored to the session extreme (the level that stops would cluster at).
    TP is the entry price ± (sl_distance × rr_ratio).

    Parameters
    ----------
    setup         : dict from measure_fake_move()
    sl_buffer_pct : extra buffer beyond the session extreme (e.g. 0.002 = 0.2%)
    rr_ratio      : TP / SL distance multiplier (e.g. 1.5)
    """
    direction = setup["direction"]
    entry     = setup["close_entry"]   # caller sets this after the 15-min window

    if direction == "LONG":
        sl      = setup["session_low"]  * (1.0 - sl_buffer_pct)
        sl_dist = abs(entry - sl)
        tp      = entry + sl_dist * rr_ratio
    else:
        sl      = setup["session_high"] * (1.0 + sl_buffer_pct)
        sl_dist = abs(sl - entry)
        tp      = entry - sl_dist * rr_ratio

    return round(sl, 8), round(tp, 8)
