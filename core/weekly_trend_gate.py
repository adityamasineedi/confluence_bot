"""Weekly trend gate — macro BTC filter for long/short direction.

Reads 1W BTC klines from cache and computes the 10-week EMA.
Used by ema_pullback, fvg, and vwap_band scorers to block trades
that contradict the macro trend.
"""
import logging
import os
import yaml

log = logging.getLogger(__name__)

_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "config.yaml")
with open(_CONFIG_PATH) as _f:
    _cfg = yaml.safe_load(_f)

_WTG        = _cfg.get("weekly_trend_gate", {})
_ENABLED    = bool(_WTG.get("enabled", True))
_EMA_PERIOD = int(_WTG.get("ema_period", 10))
_APPLY_TO   = set(_WTG.get("apply_to", []))


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
    if not _ENABLED or strategy not in _APPLY_TO:
        return True
    bars = cache.get_ohlcv("BTCUSDT", _EMA_PERIOD + 5, "1w")
    if not bars or len(bars) < _EMA_PERIOD:
        return True
    closes = [b["c"] for b in bars]
    weekly_ema = _ema(closes, _EMA_PERIOD)
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
    if not _ENABLED or strategy not in _APPLY_TO:
        return True
    bars = cache.get_ohlcv("BTCUSDT", _EMA_PERIOD + 5, "1w")
    if not bars or len(bars) < _EMA_PERIOD:
        return True
    closes = [b["c"] for b in bars]
    weekly_ema = _ema(closes, _EMA_PERIOD)
    result = closes[-1] < weekly_ema
    if not result:
        log.info(
            "Weekly gate BLOCKED SHORT (%s): BTC weekly %.0f > 10W EMA %.0f",
            strategy, closes[-1], weekly_ema,
        )
    return result
