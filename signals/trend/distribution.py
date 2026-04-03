"""Distribution detection — block late-trend LONG entries.

Dow Theory principle 3: in the distribution phase institutions sell into
retail FOMO.  The signature is price making a higher high while the
Cumulative Volume Delta (net buying pressure) fails to confirm — CVD is
making a lower high at the same time price is making a higher high.

Uses 4H bars so each half-window represents roughly 24 hours of context,
which is long enough to distinguish a real divergence from a single noisy
candle but short enough to stay relevant to the current trade setup.
"""


def is_distributing(symbol: str, cache) -> bool:
    """True when market shows distribution signs — price HH but CVD LH.

    Splits the last 12 × 4H bars into two halves and compares the highest
    price-wick and the highest CVD value in each half.  Returns False on
    insufficient data so entries are never blocked by missing history.
    """
    bars = cache.get_ohlcv(symbol, window=12, tf="4h")
    cvd  = cache.get_cvd(symbol,    window=12, tf="4h")

    if len(bars) < 8 or len(cvd) < 8:
        return False   # insufficient data — do not block

    mid = len(bars) // 2

    recent_price_high  = max(b["h"] for b in bars[mid:])
    earlier_price_high = max(b["h"] for b in bars[:mid])

    recent_cvd_high  = max(cvd[mid:])
    earlier_cvd_high = max(cvd[:mid])

    price_new_high  = recent_price_high > earlier_price_high * 1.005   # 0.5% buffer
    cvd_no_new_high = recent_cvd_high < earlier_cvd_high

    return price_new_high and cvd_no_new_high
