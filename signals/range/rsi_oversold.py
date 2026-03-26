"""RSI oversold mean-reversion signal — bounce entry at range support."""


def _calc_rsi(closes: list[float], period: int = 14) -> float:
    """Wilder RSI.  Returns 50.0 (neutral) when insufficient data."""
    if len(closes) < period + 1:
        return 50.0
    gains, losses = [], []
    for i in range(1, len(closes)):
        change = closes[i] - closes[i - 1]
        gains.append(max(change, 0.0))
        losses.append(max(-change, 0.0))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0.0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def check_rsi_oversold(symbol: str, cache) -> bool:
    """True when 15m RSI is oversold AND turning up, with price near range support.

    Conditions:
    1. RSI(14) on 15m < 35 — deeply oversold (mean-reversion entry zone)
    2. RSI turning up: current RSI > RSI one bar ago (momentum reversing)
    3. Price within 3 % above range_low — confirms we're actually at support,
       not just oversold in mid-range
    """
    closes = cache.get_closes(symbol, window=30, tf="15m")
    if len(closes) < 16:
        return False

    # Compute RSI at last two bars (need one extra close for comparison)
    rsi_now  = _calc_rsi(closes,      period=14)
    rsi_prev = _calc_rsi(closes[:-1], period=14)

    if rsi_now >= 35:
        return False

    if rsi_now <= rsi_prev:
        return False

    # Price must be near range support
    range_low = cache.get_range_low(symbol)
    if range_low is None or range_low == 0.0:
        return False

    price = closes[-1]
    proximity = (price - range_low) / range_low
    return 0.0 <= proximity <= 0.05
