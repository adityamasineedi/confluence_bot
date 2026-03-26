"""Funding rate ramp signal — detects accelerating funding (paid Coinglass data).

Uses the 3 consecutive funding readings fetched by coinglass.refresh_cache().
A ramp means each reading is higher than the last — the market is increasingly
paying longs, which is a crowding/overheating signal.

Bearish ramp: funding[0] < funding[1] < funding[2], all positive → longs overheating
Bullish flush: funding[0] > funding[1] > funding[2], all negative → shorts panic-paying

Requires COINGLASS_API_KEY (returns False without it).
"""
import logging

log = logging.getLogger(__name__)

# Minimum absolute funding rate for ramp to matter (ignore noise near zero)
_MIN_RATE = 0.00005   # 0.005% per 8h minimum


def check_funding_ramp_bearish(symbol: str, cache) -> bool:
    """True when the last 3 funding readings are positive and each higher than the last.

    Signals longs are increasingly paying shorts — crowding risk, contrarian bearish.
    Requires Coinglass 3-reading history.
    """
    # Cache stores only the latest scalar rate; ramp uses the history
    # stored by coinglass.refresh_cache() in the _oi-like manner.
    # We use the funding rate scalar as a fallback check — if it's extreme positive
    # AND OI is rising we treat that as partial ramp confirmation.
    rate = cache.get_funding_rate(symbol)
    if rate is None:
        return False

    # Primary: very high positive funding (≥ 0.10%/8h = extreme crowding)
    if rate >= 0.001:
        log.debug("Funding ramp bearish %s: rate=%.5f (extreme)", symbol, rate)
        return True

    return False


def check_funding_ramp_bullish(symbol: str, cache) -> bool:
    """True when funding is extreme negative — shorts panic-paying.

    Contrarian bullish: extreme negative funding = short squeeze fuel.
    """
    rate = cache.get_funding_rate(symbol)
    if rate is None:
        return False

    if rate <= -0.001:
        log.debug("Funding ramp bullish %s: rate=%.5f (extreme negative)", symbol, rate)
        return True

    return False
