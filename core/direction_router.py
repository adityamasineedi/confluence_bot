"""Direction router — decides trade direction before scorer selection."""
import asyncio
import logging
from typing import Literal

from .regime_detector import Regime, get_trend_bias

log = logging.getLogger(__name__)

# BTC is the macro anchor for TREND direction checks regardless of which
# altcoin symbol is being routed.
_BTC = "BTCUSDT"

# Funding threshold above which we suppress LONG in TREND (neutral band from config)
_FUNDING_LONG_MAX = 0.0003


class DirectionRouter:
    """Decides whether to go LONG, SHORT, or skip (NONE) for a symbol + regime.

    The method is synchronous and cheap — it reads only what's already in the
    cache, so it can be called at the top of every eval loop tick without I/O.

    Routing rules
    -------------
    CRASH  → always SHORT (no further checks needed)

    RANGE  → price position within the cached range bounds:
               pos ≤ 0.15  → LONG  (bottom 15 % — near support)
               pos ≥ 0.85  → SHORT (top 85 % — near resistance)
               otherwise   → NONE  (mid-range; wait for edge)

    TREND  → three-factor macro filter on BTC (all signals must align):
               btc_above_ema200  : BTC 4H close[-1] > 200-period EMA
               hh_intact         : last BTC weekly close > prior weekly close
               funding_ok_long   : funding rate is None OR < 0.0003
               LONG  when all three align to upside
               SHORT when btc below EMA200 AND weekly HH broken
               NONE  when signals are mixed
    """

    # ── Public API ────────────────────────────────────────────────────────────

    def get_direction(self, symbol: str, regime: Regime, cache) -> str:
        """Return "LONG", "SHORT", or "NONE"."""
        if regime == Regime.CRASH:
            return self._crash_direction()
        if regime == Regime.RANGE:
            return self._range_direction(symbol, cache)
        if regime == Regime.TREND:
            return self._trend_direction(symbol, cache)
        return "NONE"

    # ── CRASH ─────────────────────────────────────────────────────────────────

    def _crash_direction(self) -> str:
        return "SHORT"

    # ── RANGE ─────────────────────────────────────────────────────────────────

    def _range_direction(self, symbol: str, cache) -> str:
        """Position-in-range score → LONG near support, SHORT near resistance."""
        range_high = cache.get_range_high(symbol)
        range_low  = cache.get_range_low(symbol)

        if range_high is None or range_low is None:
            return "NONE"

        span = range_high - range_low
        if span <= 0.0:
            return "NONE"

        # Current price from the most recent 1m close
        closes_1m = cache.get_closes(symbol, window=1, tf="1m")
        if not closes_1m:
            return "NONE"
        price = closes_1m[-1]

        pos = (price - range_low) / span

        if pos <= 0.15:
            return "LONG"
        if pos >= 0.85:
            return "SHORT"
        return "NONE"

    # ── TREND ─────────────────────────────────────────────────────────────────

    def _trend_direction(self, symbol: str, cache) -> str:
        """Macro BTC filter: EMA200 + weekly HH + funding rate."""

        # ── Factor 1: BTC 4H close vs 200-period EMA ─────────────────────────
        btc_4h = cache.get_closes(_BTC, window=210, tf="4h")
        if len(btc_4h) < 210:
            # Not enough history yet — stay out
            return "NONE"
        ema200 = self._ema(btc_4h, period=200)
        btc_above_ema = btc_4h[-1] > ema200

        # ── Factor 2: Weekly higher-high intact ──────────────────────────────
        weekly = cache.get_ohlcv(_BTC, window=4, tf="1w")
        if len(weekly) < 2:
            # Insufficient weekly bars — give benefit of doubt based on EMA only
            hh_intact = btc_above_ema
        else:
            hh_intact = weekly[-1]["c"] > weekly[-2]["c"]

        # ── Factor 3: Funding rate not overheated on the long side ────────────
        funding = cache.get_funding_rate(symbol)
        funding_ok_long = funding is None or funding < _FUNDING_LONG_MAX

        # ── Decision ─────────────────────────────────────────────────────────
        if btc_above_ema and hh_intact and funding_ok_long:
            return "LONG"
        if not btc_above_ema and not hh_intact:
            return "SHORT"
        return "NONE"

    # ── EMA helper ────────────────────────────────────────────────────────────

    def _ema(self, data: list[float], period: int) -> float:
        """Return the final EMA value for *data* using multiplier 2/(period+1).

        Seeded with the SMA of the first `period` bars.
        Returns 0.0 when there are fewer bars than `period`.
        """
        if len(data) < period:
            return 0.0
        k   = 2.0 / (period + 1)
        ema = sum(data[:period]) / period
        for price in data[period:]:
            ema = price * k + ema * (1.0 - k)
        return ema


# ── Existing module-level functions (used by main.py / core/__init__.py) ─────
# These delegate to the scorer layer and remain unchanged.

async def route_direction(symbol: str, cache, regime: Regime) -> list[dict]:
    """Run the appropriate scorer(s) for the given regime concurrently.

    Returns ALL score dicts (fire=True and fire=False) so callers can log
    every evaluation for observability.  Callers check score_dict["fire"]
    to decide whether to execute.
    """
    coros = _build_coros(symbol, cache, regime)
    if not coros:
        return []

    raw = await asyncio.gather(*coros, return_exceptions=True)

    results: list[dict] = []
    for result in raw:
        if isinstance(result, Exception):
            log.error("Scorer raised for %s [%s]: %s", symbol, regime, result)
            continue
        results.append(result)

    return results


def _build_coros(symbol: str, cache, regime: Regime) -> list:
    r = str(regime)
    if r == "TREND":
        return _trend_coros(symbol, cache)
    if r == "RANGE":
        return _range_coros(symbol, cache)
    if r == "CRASH":
        return _crash_coros(symbol, cache)
    if r == "PUMP":
        return _pump_coros(symbol, cache)
    if r == "BREAKOUT":
        return _breakout_coros(symbol, cache, regime)
    return []


def _trend_coros(symbol: str, cache) -> list:
    from .scorer import score as trend_long
    from .bear_scorer import score as bear_short

    bias = get_trend_bias(symbol, cache)
    if bias == "LONG":
        return [trend_long(symbol, cache)]
    if bias == "SHORT":
        return [bear_short(symbol, cache)]
    # NEUTRAL — DI lines not aligned; no edge, no signal
    return []


def _range_coros(symbol: str, cache) -> list:
    from .range_scorer import score as range_long
    from .range_short_scorer import score as range_short

    return [range_long(symbol, cache), range_short(symbol, cache)]


def _crash_coros(symbol: str, cache) -> list:
    from .crash_scorer import score as crash_short

    return [crash_short(symbol, cache)]


def _pump_coros(symbol: str, cache) -> list:
    from .pump_scorer import score as pump_long

    return [pump_long(symbol, cache)]


def _breakout_coros(symbol: str, cache, regime: Regime) -> list:
    from .breakout_scorer import score_long, score_short
    from .regime_detector import _detector

    bdir = _detector.get_breakout_direction(symbol)
    if bdir == "LONG":
        return [score_long(symbol, cache)]
    if bdir == "SHORT":
        return [score_short(symbol, cache)]
    # Direction not yet determined — score both and let fire decide
    return [score_long(symbol, cache), score_short(symbol, cache)]
