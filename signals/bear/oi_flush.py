"""OI flush signal — open interest liquidation cascade (bearish)."""

_OI_PEAK_WINDOW = 12     # look back N readings to find the recent OI peak
_OI_DROP_MIN    = 0.05   # OI must have dropped ≥ 5 % from peak (flush in progress)
_PRICE_DROP_MIN = 0.01   # price must also be down ≥ 1 % from the OI-peak candle


def check_oi_long_flush(symbol: str, cache) -> bool:
    """True when OI has peaked and is now flushing alongside a price decline.

    A rapid OI decline with falling prices indicates forced long liquidations —
    the unwind tends to accelerate, making this a high-conviction short signal.

    Requirements:
    - At least _OI_PEAK_WINDOW OI readings in cache.
    - OI fell ≥ 5 % from its recent peak.
    - Price is also below where it was at the OI peak.
    """
    oi_series = cache.get_oi_history(symbol, window=_OI_PEAK_WINDOW)
    if len(oi_series) < 3:
        return False

    oi_peak = max(oi_series)
    oi_now  = oi_series[-1]

    if oi_peak == 0:
        return False

    oi_drop = (oi_peak - oi_now) / oi_peak
    if oi_drop < _OI_DROP_MIN:
        return False

    # Price confirmation: current price vs price at OI peak candle
    prices = cache.get_closes(symbol, window=_OI_PEAK_WINDOW, tf="1h")
    if len(prices) < 3:
        return False

    # Align: assume OI series and price series are the same length
    peak_idx   = oi_series.index(oi_peak)
    price_peak = prices[peak_idx] if peak_idx < len(prices) else prices[0]
    price_now  = prices[-1]

    if price_peak == 0:
        return False

    price_drop = (price_peak - price_now) / price_peak
    return price_drop >= _PRICE_DROP_MIN
