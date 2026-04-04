"""core/vol_ratio.py — Shared volatility ratio gate for live scorers.

Compares recent 6H volatility to 48H baseline using 1H log returns.
Returns ratio > 1.0 when market is more volatile than normal.
Used by scorers to skip entries during vol spikes that the ATR gate misses.
"""
import math


def compute_vol_ratio(bars_1h: list[dict],
                      short_window: int = 6,
                      long_window: int  = 48) -> float:
    """Recent 6H vol / baseline 48H vol ratio.

    Returns 1.0 if insufficient data.
    """
    closes = [b["c"] for b in bars_1h if b.get("c", 0) > 0]
    if len(closes) < long_window + short_window:
        return 1.0

    def _log_std(series: list[float]) -> float:
        rets = [math.log(series[i] / series[i - 1])
                for i in range(1, len(series))
                if series[i - 1] > 0 and series[i] > 0]
        if len(rets) < 2:
            return 0.0
        mean = sum(rets) / len(rets)
        var  = sum((r - mean) ** 2 for r in rets) / len(rets)
        return math.sqrt(var)

    recent_vol   = _log_std(closes[-short_window:])
    baseline_vol = _log_std(closes[-(long_window + short_window):-short_window])

    if baseline_vol < 1e-10:
        return 1.0
    return round(recent_vol / baseline_vol, 3)
