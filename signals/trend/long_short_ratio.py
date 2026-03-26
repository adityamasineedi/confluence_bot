"""Long/Short ratio signal — contrarian entries from Coinglass crowd positioning.

When the global long/short account ratio is extremely skewed, mean reversion is
likely: a crowded long is fuel for a long squeeze; a crowded short is fuel for a
short squeeze.

Thresholds (configurable):
  ls_crowded_long  (default 1.8): ratio > this → everyone is long → contrarian BEAR
  ls_crowded_short (default 0.6): ratio < this → everyone is short → contrarian BULL

Both functions return False when the ratio is unavailable (no Coinglass API key).
"""
import logging
import os
import yaml

log = logging.getLogger(__name__)

_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "config.yaml")
with open(_CONFIG_PATH) as _f:
    _cfg = yaml.safe_load(_f)

_ls_cfg = _cfg.get("long_short_ratio", {})
_CROWDED_LONG  = float(_ls_cfg.get("crowded_long_threshold",  1.8))
_CROWDED_SHORT = float(_ls_cfg.get("crowded_short_threshold", 0.6))


def check_ls_crowded_long(symbol: str, cache) -> bool:
    """True when the crowd is excessively long → contrarian bearish pressure.

    Fires when global L/S ratio > crowded_long_threshold (default 1.8).
    Used as a supplementary BEAR / TREND SHORT signal.
    """
    ratio = cache.get_long_short_ratio(symbol)
    if ratio is None:
        return False
    result = ratio > _CROWDED_LONG
    if result:
        log.debug("L/S crowded long %s: ratio=%.3f > %.3f", symbol, ratio, _CROWDED_LONG)
    return result


def check_ls_crowded_short(symbol: str, cache) -> bool:
    """True when the crowd is excessively short → contrarian bullish pressure.

    Fires when global L/S ratio < crowded_short_threshold (default 0.6).
    Used as a supplementary TREND LONG signal.
    """
    ratio = cache.get_long_short_ratio(symbol)
    if ratio is None:
        return False
    result = ratio < _CROWDED_SHORT
    if result:
        log.debug("L/S crowded short %s: ratio=%.3f < %.3f", symbol, ratio, _CROWDED_SHORT)
    return result
