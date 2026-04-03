"""
signals/trend/oi_divergence.py
Open Interest divergence signals.

When price and OI move in opposite directions, the move is
structurally weak and likely to reverse.

Price up + OI down → longs are closing, not new money entering.
  Fake breakout. Expect reversal SHORT.

Price down + OI up → shorts piling in aggressively.
  Short squeeze potential. Expect reversal LONG.
"""


def _oi_change_pct(symbol: str, cache, hours: int = 4) -> float | None:
    """Return OI % change over the last N hours. None if data unavailable."""
    oi_now  = cache.get_oi(symbol, offset_hours=0,     exchange="binance")
    oi_prev = cache.get_oi(symbol, offset_hours=hours, exchange="binance")
    if oi_now is None or oi_prev is None or oi_prev == 0:
        return None
    return (oi_now - oi_prev) / oi_prev


def _price_change_pct(symbol: str, cache, bars: int = 4) -> float:
    """Return price % change over last N 1H bars."""
    closes = cache.get_closes(symbol, window=bars + 1, tf="1h")
    if len(closes) < 2:
        return 0.0
    old = closes[0]
    return (closes[-1] - old) / old if old > 0 else 0.0


def check_oi_divergence_short(symbol: str, cache) -> bool:
    """True when price is rising but OI is falling — fake breakout.

    Price up ≥ 1.5% over 4H but OI down ≥ 2% over same period.
    Longs are taking profit / closing, not new money entering.
    Expect reversal or at minimum loss of momentum.
    """
    oi_chg    = _oi_change_pct(symbol, cache, hours=4)
    price_chg = _price_change_pct(symbol, cache, bars=4)
    if oi_chg is None:
        return False
    return price_chg >= 0.015 and oi_chg <= -0.02


def check_oi_divergence_long(symbol: str, cache) -> bool:
    """True when price is falling but OI is rising — short squeeze setup.

    Price down ≥ 1.5% over 4H but OI up ≥ 3% over same period.
    New shorts piling in aggressively = fuel for a squeeze.
    """
    oi_chg    = _oi_change_pct(symbol, cache, hours=4)
    price_chg = _price_change_pct(symbol, cache, bars=4)
    if oi_chg is None:
        return False
    return price_chg <= -0.015 and oi_chg >= 0.03
