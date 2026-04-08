"""Weekly trend gate — macro BTC filter for long/short direction.

Reads 1W BTC klines from cache and computes the 10-week EMA.
Used by scorers to block trades that contradict the macro trend.

Config is reloaded on every call so changes to apply_to take
effect without a bot restart.
"""
import logging
import os
import time
import yaml

log = logging.getLogger(__name__)

_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "config.yaml")

# Config cache — reload every 30s instead of reading YAML on every call
_cfg_cache: dict = {}
_cfg_ts: float = 0.0
_CFG_TTL = 30.0


def _get_wtg() -> dict:
    """Return weekly_trend_gate config, refreshed every 30s."""
    global _cfg_cache, _cfg_ts
    now = time.monotonic()
    if now - _cfg_ts < _CFG_TTL and _cfg_cache:
        return _cfg_cache
    try:
        with open(_CONFIG_PATH) as f:
            cfg = yaml.safe_load(f)
        _cfg_cache = cfg.get("weekly_trend_gate", {})
        _cfg_ts = now
    except Exception:
        pass  # keep stale cache on read error
    return _cfg_cache


def _ema(closes: list[float], period: int) -> float:
    if len(closes) < period:
        return closes[-1] if closes else 0.0
    k = 2.0 / (period + 1)
    val = sum(closes[:period]) / period
    for c in closes[period:]:
        val = c * k + val * (1.0 - k)
    return val


def weekly_allows_long(strategy: str, cache) -> bool:
    """True when BTC weekly close is above 10W EMA (macro bull).
    Returns True on insufficient data — never blocks on missing data.
    """
    wtg = _get_wtg()
    if not wtg.get("enabled", True):
        return True
    if strategy not in set(wtg.get("apply_to", [])):
        return True
    ema_period = int(wtg.get("ema_period", 10))
    bars = cache.get_ohlcv("BTCUSDT", ema_period + 5, "1w")
    if not bars or len(bars) < ema_period:
        return True
    closes = [b["c"] for b in bars]
    weekly_ema = _ema(closes, ema_period)
    result = closes[-1] > weekly_ema
    if not result:
        log.info(
            "Weekly gate BLOCKED LONG (%s): BTC weekly %.0f < 10W EMA %.0f",
            strategy, closes[-1], weekly_ema,
        )
    return result


def weekly_allows_short(strategy: str, cache) -> bool:
    """True when BTC weekly close is below 10W EMA (macro bear).
    Returns True on insufficient data.
    """
    wtg = _get_wtg()
    if not wtg.get("enabled", True):
        return True
    if strategy not in set(wtg.get("apply_to", [])):
        return True
    ema_period = int(wtg.get("ema_period", 10))
    bars = cache.get_ohlcv("BTCUSDT", ema_period + 5, "1w")
    if not bars or len(bars) < ema_period:
        return True
    closes = [b["c"] for b in bars]
    weekly_ema = _ema(closes, ema_period)
    result = closes[-1] < weekly_ema
    if not result:
        log.info(
            "Weekly gate BLOCKED SHORT (%s): BTC weekly %.0f > 10W EMA %.0f",
            strategy, closes[-1], weekly_ema,
        )
    return result
