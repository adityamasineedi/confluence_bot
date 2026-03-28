"""OI Spike Fade scorer — wraps signals/trend/oi_spike.py into a score_dict for the executor.

Signal weights
--------------
  oi_spike        0.40  (HARD gate — must fire)
  price_rejection 0.35  (HARD gate — wick must confirm)
  ema_aligned     0.15
  rsi_zone        0.10
  ─────────────────────
  threshold       0.75  (both HARD gates alone = 0.75 → fires; bonus signals push higher)

SL/TP keys injected: os_stop, os_tp
"""
import logging
import os
import yaml

from signals.trend.oi_spike import (
    check_oi_spike_long,
    check_oi_spike_short,
    get_oi_spike_levels,
    _ema,
    _rsi,
    _EMA_PERIOD,
    _RSI_WINDOW,
)

log = logging.getLogger(__name__)

_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "config.yaml")
with open(_CONFIG_PATH) as _f:
    _cfg = yaml.safe_load(_f)

_OS_CFG    = _cfg.get("oi_spike", {})
_THRESHOLD = float(_OS_CFG.get("threshold",    0.75))
_COOLDOWN  = float(_OS_CFG.get("cooldown_mins", 60)) * 60.0

# Weights
_W_OI_SPIKE    = 0.40
_W_REJECTION   = 0.35
_W_EMA         = 0.15
_W_RSI         = 0.10

from core.cooldown_store import CooldownStore as _CooldownStore
_cooldown = _CooldownStore("OISPIKE")


def set_cooldown(symbol: str) -> None:
    _cooldown.set(symbol, _COOLDOWN)


async def score(symbol: str, cache) -> list[dict]:
    """Return a list of score_dicts (LONG and/or SHORT) for OI spike fade setups."""
    results: list[dict] = []

    for direction in ("LONG", "SHORT"):
        check_fn = check_oi_spike_long if direction == "LONG" else check_oi_spike_short

        # Cooldown gate
        if _cooldown.is_active(symbol):
            log.debug("OI Spike %s %s — cooldown active", direction, symbol)
            continue

        spike_result = check_fn(symbol, cache)
        if spike_result is None:
            continue

        fired_oi       = spike_result.get("fired", False)
        spike_pct      = spike_result.get("spike_pct", 0.0)
        wick_pct       = spike_result.get("wick_pct", 0.0)

        # Hard gates: both oi_spike AND price_rejection must fire
        if not fired_oi:
            results.append(_build(symbol, direction, 0.0,
                                  {"oi_spike": False, "price_rejection": bool(wick_pct > 0),
                                   "ema_aligned": False, "rsi_zone": False},
                                  fire=False, stop=0.0, tp=0.0))
            continue

        # Price rejection is already gated inside check_fn (wick_pct check), but we
        # record it explicitly for the signals dict.
        rejection_ok = fired_oi  # check_fn already validated wick; True here means passed

        # EMA alignment bonus
        candles = cache.get_ohlcv(symbol, _EMA_PERIOD + 5, "15m")
        ema_aligned = False
        if candles:
            closes  = [c["c"] for c in candles]
            price   = closes[-1]
            ema_val = _ema(closes[:-1], _EMA_PERIOD)
            ema_aligned = (price > ema_val) if direction == "LONG" else (price < ema_val)

        # RSI zone bonus
        rsi_zone = False
        if candles:
            closes = [c["c"] for c in candles]
            rsi    = _rsi(closes, _RSI_WINDOW)
            rsi_zone = (35 <= rsi <= 55) if direction == "LONG" else (45 <= rsi <= 65)

        score_val = (
            _W_OI_SPIKE  * 1.0
            + _W_REJECTION * float(rejection_ok)
            + _W_EMA       * float(ema_aligned)
            + _W_RSI       * float(rsi_zone)
        )

        # Compute SL/TP
        levels = get_oi_spike_levels(symbol, cache, direction)
        if levels is None:
            continue
        sl, tp = levels

        fire = score_val >= _THRESHOLD and sl > 0 and tp > 0

        results.append(_build(
            symbol, direction, score_val,
            {"oi_spike": True, "price_rejection": rejection_ok,
             "ema_aligned": ema_aligned, "rsi_zone": rsi_zone},
            fire=fire, stop=sl, tp=tp,
        ))
        log.debug(
            "OI Spike %s %s  score=%.2f  spike=%.1f%%  wick=%.2f%%  fire=%s",
            direction, symbol, score_val, spike_pct * 100, wick_pct * 100, fire,
        )

    return results


def _build(
    symbol: str,
    direction: str,
    score_val: float,
    signals: dict,
    *,
    fire: bool,
    stop: float,
    tp: float,
) -> dict:
    return {
        "symbol":    symbol,
        "regime":    "OISPIKE",
        "direction": direction,
        "score":     round(score_val, 4),
        "signals":   signals,
        "fire":      fire,
        "os_stop":   round(stop, 8),
        "os_tp":     round(tp, 8),
    }
