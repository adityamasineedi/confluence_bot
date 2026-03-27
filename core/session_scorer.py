"""Session open trap scorer — live scoring with per-session cooldown.

Score components (equal-weighted, 0.25 each):
    fake_move_ok    — |fake_move| >= min_move_pct (sufficient sweep distance)
    move_strong     — |fake_move| >= strong_move_pct (bonus signal quality tier)
    spread_tight    — session range (high-low) / open is not too wide (no gap open)
    cooldown_ok     — no recent trade on this symbol in this session window

The scorer is called *once per session*, ~15 min after the session opens.
After the 15-min window the entry opportunity is gone (session direction clarifies).
"""
import logging
import os
import time
import yaml

log = logging.getLogger(__name__)

_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "config.yaml")
with open(_CONFIG_PATH) as _f:
    _cfg = yaml.safe_load(_f)

_SS = _cfg.get("session_trap", {})

_MIN_MOVE_PCT       = float(_SS.get("min_move_pct",       0.006))   # 0.6% fake move to qualify
_STRONG_MOVE_PCT    = float(_SS.get("strong_move_pct",   0.006))   # 0.6% = strong bonus
_MAX_RANGE_PCT      = float(_SS.get("max_range_pct",     0.015))   # hard gate: reject gap opens >1.5%
_RANGE_COMPACT_MAX  = float(_SS.get("range_compact_max", 0.007))   # range_compact signal: ≤ 0.7%
_SL_BUFFER_PCT      = float(_SS.get("sl_buffer_pct",     0.002))   # 0.2% beyond session extreme
_RR_RATIO           = float(_SS.get("rr_ratio",           1.5))
_THRESHOLD          = float(_SS.get("fire_threshold",     0.75))   # 3 of 4 signals
_COOLDOWN_SECS      = float(_SS.get("cooldown_mins",       60)) * 60.0

# symbol+session → monotonic timestamp when cooldown expires
_cooldown_until: dict[str, float] = {}


def is_on_cooldown(symbol: str, session_hour: int) -> bool:
    key = f"{symbol}:{session_hour}"
    return time.monotonic() < _cooldown_until.get(key, 0.0)


def set_cooldown(symbol: str, session_hour: int) -> None:
    key = f"{symbol}:{session_hour}"
    _cooldown_until[key] = time.monotonic() + _COOLDOWN_SECS
    log.debug("SessionTrap cooldown set for %s session=%d", symbol, session_hour)


async def score(symbol: str, cache, session_hour: int) -> dict | None:
    """Score *symbol* for a session trap setup at the given session_hour.

    Returns a score dict (standard format) or None if the 15-min window has
    not completed yet for this session.
    """
    from signals.session.detector import measure_fake_move, compute_levels

    # Fetch the last 5 × 5m bars; we need exactly 3 for the session window
    bars = cache.get_ohlcv(symbol, window=6, tf="5m")
    if len(bars) < 3:
        return None

    # The most recent 3 bars should be the session opening window
    window = bars[-3:]
    setup  = measure_fake_move(window)
    if setup is None:
        return None

    fake_abs   = abs(setup["fake_move"])
    range_pct  = (setup["session_high"] - setup["session_low"]) / setup["open_price"]
    cool_ok    = not is_on_cooldown(symbol, session_hour)

    # Score signals — all vary independently, no overlap with hard gates
    reversal_bar  = window[-1]["c"] < window[-2]["c"] if setup["direction"] == "LONG" else window[-1]["c"] > window[-2]["c"]
    signals = {
        "fake_move_ok":   fake_abs >= _MIN_MOVE_PCT,   # sufficient sweep to fade
        "move_strong":    fake_abs >= _STRONG_MOVE_PCT, # strong sweep = better edge
        "reversal_bar":   reversal_bar,                 # bar-3 already reversing direction
        "range_compact":  range_pct <= _RANGE_COMPACT_MAX,    # very tight session range (≤ 0.7%)
    }

    score_val = sum(0.25 for v in signals.values() if v)

    # Hard gates (not counted in score)
    setup["close_entry"] = window[-1]["c"]
    sl, tp = compute_levels(setup, _SL_BUFFER_PCT, _RR_RATIO)

    fire = (
        score_val >= _THRESHOLD
        and fake_abs >= _MIN_MOVE_PCT   # hard gate: must have a real fake move
        and range_pct <= _MAX_RANGE_PCT  # hard gate: reject gap opens
        and cool_ok
    )

    return {
        "symbol":       symbol,
        "regime":       "SESSION",
        "direction":    setup["direction"],
        "score":        round(score_val, 4),
        "signals":      signals,
        "fire":         fire,
        "ss_stop":      sl,
        "ss_tp":        tp,
        "fake_move":    round(setup["fake_move"] * 100, 3),
        "session_hour": session_hour,
    }
