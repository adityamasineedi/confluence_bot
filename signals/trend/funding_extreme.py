"""
signals/trend/funding_extreme.py
Funding rate contrarian signals.

Extreme positive funding (longs pay shorts > 0.12%/8H):
  Crowd is maximally long → contrarian SHORT signal.

Extreme negative funding (shorts pay longs > 0.12%/8H):
  Crowd is maximally short → contrarian LONG signal.

This is NOT income collection — it is fading overleveraged crowds.
Only fires once per 8H window per symbol.
"""
import os, yaml

_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "config.yaml")
with open(_CONFIG_PATH) as _f:
    _cfg = yaml.safe_load(_f)

_FC = _cfg.get("funding_contrarian", {})
_EXTREME_POS = float(_FC.get("extreme_positive_pct", 0.0012))  # 0.12%/8H
_EXTREME_NEG = float(_FC.get("extreme_negative_pct", -0.0012)) # -0.12%/8H


def check_funding_extreme_short(symbol: str, cache) -> bool:
    """True when funding is extremely positive — crowd is max long.
    Contrarian SHORT signal.
    """
    rate = cache.get_funding_rate(symbol)
    if rate is None:
        return False
    return rate >= _EXTREME_POS


def check_funding_extreme_long(symbol: str, cache) -> bool:
    """True when funding is extremely negative — crowd is max short.
    Contrarian LONG signal.
    """
    rate = cache.get_funding_rate(symbol)
    if rate is None:
        return False
    return rate <= _EXTREME_NEG
