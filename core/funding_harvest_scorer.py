"""Funding rate harvest scorer — systematic income from extreme funding windows.

Score components (equal-weighted, 0.25 each):
    rate_extreme    — |funding_rate| >= min_rate_pct (e.g. 0.05%)
    rate_very_high  — |funding_rate| >= strong_rate_pct (e.g. 0.10%) — bonus
    in_window       — we are within entry_mins_before of next settlement
    cooldown_ok     — no recent harvest on this symbol

Income math at 0.1% funding with 0.5% SL / 0.8% TP:
    Expected value = WR × 0.8 − (1−WR) × 0.5
    Break-even WR  = 38.5%
    Actual WR usually 55–65% (funding reverts, price stabilises post-settlement)
"""
import logging
import os
import time
import yaml

log = logging.getLogger(__name__)

_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "config.yaml")
with open(_CONFIG_PATH) as _f:
    _cfg = yaml.safe_load(_f)

_FH = _cfg.get("funding_harvest", {})

_MIN_RATE_PCT    = float(_FH.get("min_rate_pct",       0.0005))  # 0.05% per 8h
_STRONG_RATE_PCT = float(_FH.get("strong_rate_pct",    0.0010))  # 0.10% per 8h
_ENTRY_MINS      = int(_FH.get("entry_mins_before",     30))
_EXIT_MINS       = int(_FH.get("exit_mins_after",       15))
_SL_PCT          = float(_FH.get("sl_pct",             0.005))
_TP_PCT          = float(_FH.get("tp_pct",             0.008))
_THRESHOLD       = float(_FH.get("fire_threshold",     0.50))
_COOLDOWN_SECS   = float(_FH.get("cooldown_hours",      8)) * 3600.0

_cooldown_until: dict[str, float] = {}


def is_on_cooldown(symbol: str) -> bool:
    return time.monotonic() < _cooldown_until.get(symbol, 0.0)


def set_cooldown(symbol: str) -> None:
    _cooldown_until[symbol] = time.monotonic() + _COOLDOWN_SECS
    log.debug("FundingHarvest cooldown set for %s (%.1fh)", symbol, _COOLDOWN_SECS / 3600)


async def score(symbol: str, cache) -> dict | None:
    """Score *symbol* for a funding harvest setup.

    Returns a score dict or None if we're not in a funding window.
    """
    import time as _time
    from signals.funding_harvest.detector import (
        funding_direction,
        settlement_in_window,
        compute_levels,
    )

    rate = cache.get_funding_rate(symbol)
    if rate is None:
        return None

    ts_ms    = int(_time.time() * 1000)
    in_win   = settlement_in_window(ts_ms, _ENTRY_MINS, _EXIT_MINS)
    cool_ok  = not is_on_cooldown(symbol)

    direction = funding_direction(rate, _MIN_RATE_PCT)
    if direction is None:
        return None   # funding not extreme enough — don't even log this

    rate_strong = abs(rate) >= _STRONG_RATE_PCT

    signals = {
        "rate_extreme":  True,           # already confirmed above
        "rate_very_high": rate_strong,
        "in_window":     in_win,
        "cooldown_ok":   cool_ok,
    }

    score_val = sum(0.25 for v in signals.values() if v)

    entry = cache.get_last_price(symbol)
    if entry <= 0.0:
        return None

    sl, tp = compute_levels(direction, entry, _SL_PCT, _TP_PCT)

    fire = (
        score_val >= _THRESHOLD
        and in_win        # hard gate: must be in the settlement window
        and cool_ok
    )

    rr = _TP_PCT / _SL_PCT

    return {
        "symbol":        symbol,
        "regime":        "FUNDING",
        "direction":     direction,
        "score":         round(score_val, 4),
        "signals":       signals,
        "fire":          fire,
        "fh_stop":       sl,
        "fh_tp":         tp,
        "funding_rate":  round(rate * 100, 4),   # in percent
        "rr":            round(rr, 2),
    }
