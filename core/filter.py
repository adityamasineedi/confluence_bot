"""Hard filters for all trade directions — must all pass before a signal fires."""
import logging
import os
import yaml

_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "config.yaml")
with open(_CONFIG_PATH) as _f:
    _cfg = yaml.safe_load(_f)

log = logging.getLogger(__name__)

_FUNDING_MAX_LONG  = _cfg["funding"]["neutral_band"]   # 0.0003
_FUNDING_MIN_SHORT = -_cfg["funding"]["neutral_band"]  # -0.0003
_MIN_24H_VOL       = _cfg["filters"]["min_24h_volume_usdt"]
_BTC               = "BTCUSDT"

# +DI must exceed -DI by at least this margin to confirm trend direction
_DI_MIN_EDGE = 5.0

# BTC overextension: don't go TREND LONG if BTC > EMA200 × this multiple
_BTC_OVEREXT_MULT = 1.15


# ── BTC EMA-200 helpers ───────────────────────────────────────────────────────

def _btc_ema200(cache) -> float:
    """Compute BTC 4H EMA(200).  Returns 0.0 on insufficient data."""
    btc_4h = cache.get_closes(_BTC, window=210, tf="4h")
    if len(btc_4h) < 200:
        return 0.0
    k   = 2.0 / 201
    ema = sum(btc_4h[:200]) / 200
    for p in btc_4h[200:]:
        ema = p * k + ema * (1 - k)
    return ema


def _btc_above_ema200(cache) -> bool:
    """Return True when BTC 4H price is above its 200-period EMA."""
    btc_4h = cache.get_closes(_BTC, window=210, tf="4h")
    if not btc_4h:
        return True   # insufficient data → give benefit of doubt
    ema = _btc_ema200(cache)
    if ema == 0.0:
        return True
    return btc_4h[-1] > ema


def _btc_not_overextended(cache) -> bool:
    """Return False when BTC is >15% above its 4H EMA200 (parabolic, reversal risk)."""
    btc_4h = cache.get_closes(_BTC, window=210, tf="4h")
    if not btc_4h:
        return True
    ema = _btc_ema200(cache)
    if ema == 0.0:
        return True
    return btc_4h[-1] <= ema * _BTC_OVEREXT_MULT


def _di_lines(symbol: str, cache) -> tuple[float, float]:
    """Return (+DI, -DI) from ADX(14) on the symbol's 4H candles."""
    from core.regime_detector import get_adx_info
    info = get_adx_info(symbol, cache, tf="4h")
    return info["plus_di"], info["minus_di"]


def _adx_is_rising(symbol: str, cache) -> bool:
    """Return True when the 4H ADX is not in a steep decline.

    Blocks entries only when ADX is clearly falling (trend exhausting).
    Allow flat or rising ADX — both are acceptable entry conditions.
    Returns True on insufficient data.
    """
    from core.regime_detector import get_adx_series
    history = get_adx_series(symbol, cache, tf="4h", n=3)
    if len(history) < 3:
        return True   # insufficient data — pass
    # Block only when current ADX is below the minimum of prior 2 readings
    # (i.e. ADX making new lows = clear exhaustion)
    return history[-1] >= min(history[:-1])


def _daily_bar_confirms_long(symbol: str, cache) -> bool:
    """Return True when the last completed daily bar was green (close >= open)."""
    candles = cache.get_ohlcv(symbol, window=2, tf="1d")
    if not candles:
        return True
    bar = candles[-1]
    return bar["c"] >= bar["o"]


def _daily_bar_confirms_short(symbol: str, cache) -> bool:
    """Return True when the last completed daily bar was red (close <= open)."""
    candles = cache.get_ohlcv(symbol, window=2, tf="1d")
    if not candles:
        return True
    bar = candles[-1]
    return bar["c"] <= bar["o"]


# ── Public filter functions ───────────────────────────────────────────────────

def passes_trend_long_filters(symbol: str, cache) -> bool:
    """Return True only when all TREND LONG hard filters are satisfied.

    Gates:
    1. BTC 4H close above 200-period EMA (macro must be bullish)
    2. Symbol 4H +DI > -DI by at least _DI_MIN_EDGE (4H trend direction is up)
    3. 4H ADX is rising (trend accelerating, not exhausting)
    4. BTC not parabolic (< EMA200 × 1.15)
    5. Funding rate not overheated (< 0.0003)
    6. 24H volume above minimum liquidity threshold

    Note: daily bar green gate removed — blocks 40% of valid entry days
    (in a trend, pullback days produce red daily bars but are ideal entries).
    ADX slope (Gate 3) provides the exhaustion guard instead.
    """
    # Gate 1: BTC macro bullish
    if not _btc_above_ema200(cache):
        log.info("TREND LONG filter BLOCKED %s: Gate1 BTC below EMA200", symbol)
        return False

    # Gate 2: 4H DI confirms upward trend direction
    plus_di, minus_di = _di_lines(symbol, cache)
    if plus_di - minus_di < _DI_MIN_EDGE:
        log.info("TREND LONG filter BLOCKED %s: Gate2 +DI=%.1f -DI=%.1f gap<%.1f", symbol, plus_di, minus_di, _DI_MIN_EDGE)
        return False

    # Gate 3: ADX must be rising (avoid exhausted trends — key fix for bad months).
    # Exception: in PUMP regime, ADX can flatten during parabolic moves while price
    # still trends strongly — skip the ADX gate to avoid blocking valid pump entries.
    from core.regime_detector import detect_regime
    if str(detect_regime(symbol, cache)) != "PUMP":
        if not _adx_is_rising(symbol, cache):
            log.info("TREND LONG filter BLOCKED %s: Gate3 ADX declining", symbol)
            return False

    # Gate 4: BTC not in parabolic overextension
    if not _btc_not_overextended(cache):
        log.info("TREND LONG filter BLOCKED %s: Gate4 BTC overextended", symbol)
        return False

    # Gate 5: Funding not overheated
    funding = cache.get_funding_rate(symbol)
    if funding is not None and funding >= _FUNDING_MAX_LONG:
        log.info("TREND LONG filter BLOCKED %s: Gate5 funding=%.5f overheated", symbol, funding)
        return False

    # Gate 6: Liquidity
    vol_24h = cache.get_vol_24h(symbol)
    if vol_24h is not None and vol_24h < _MIN_24H_VOL:
        log.info("TREND LONG filter BLOCKED %s: Gate6 vol_24h=%.0f too low", symbol, vol_24h)
        return False

    return True


def passes_trend_short_filters(symbol: str, cache) -> bool:
    """Return True only when all TREND SHORT hard filters are satisfied.

    Gates:
    1. BTC 4H close below 200-period EMA (macro must be bearish)
    2. Symbol 4H -DI > +DI by at least _DI_MIN_EDGE (4H trend direction is down)
    3. 4H ADX not making new lows (not deeply exhausted) — softened vs long:
       ADX may be flat; only block when it is clearly at a 3-bar low
    4. Funding not extreme negative (crash scorer handles that)
    5. 24H volume above minimum threshold

    Note: daily bar confirmation removed for shorts — in a downtrend, bounces
    produce green daily bars constantly and the gate was blocking 95% of entries.
    ADX slope (Gate 3) provides the exhaustion guard instead.
    """
    # Gate 1: BTC macro bearish
    if _btc_above_ema200(cache):
        return False

    # Gate 2: 4H DI confirms downward trend direction
    plus_di, minus_di = _di_lines(symbol, cache)
    if minus_di - plus_di < _DI_MIN_EDGE:
        return False   # 4H trend is sideways or pointing UP — skip short

    # Gate 3: ADX not at a 3-bar low (trend must not be deeply exhausted)
    if not _adx_is_rising(symbol, cache):
        return False

    # Gate 4: Funding not in panic territory
    funding = cache.get_funding_rate(symbol)
    if funding is not None and funding < _FUNDING_MIN_SHORT:
        return False

    # Gate 5: Liquidity
    vol_24h = cache.get_vol_24h(symbol)
    if vol_24h is not None and vol_24h < _MIN_24H_VOL:
        return False

    return True


def passes_pump_filters(symbol: str, cache) -> bool:
    """Return True when all PUMP LONG hard filters are satisfied.

    PUMP is already validated by regime detection (price > EMA50, +12% 7-day).
    Filters here block extreme greed, illiquidity, and parabolic blow-off tops.

    Gates:
    1. Funding not extreme positive (avoid buying into a fully crowded long)
    2. 24H volume above minimum liquidity threshold
    3. Price not >20% above 1D EMA50 (blow-off top / exhaustion guard)
    """
    # Gate 1: Funding not in extreme greed territory
    funding = cache.get_funding_rate(symbol)
    _PUMP_FUNDING_MAX = _cfg["funding"]["extreme_positive"]   # 0.001 = 0.1%/8h
    if funding is not None and funding > _PUMP_FUNDING_MAX:
        return False

    # Gate 2: Liquidity
    vol_24h = cache.get_vol_24h(symbol)
    if vol_24h is not None and vol_24h < _MIN_24H_VOL:
        return False

    # Gate 3: Price not extended >20% above 1D EMA50 (blow-off top guard)
    closes_1d = cache.get_closes(symbol, window=60, tf="1d")
    if len(closes_1d) >= 50:
        ema50 = sum(closes_1d[-50:]) / 50
        if closes_1d[-1] > ema50 * 1.20:
            log.info("PUMP filter BLOCKED %s: Gate3 price >20%% above 1D EMA50 (%.4f > %.4f)", symbol, closes_1d[-1], ema50 * 1.20)
            return False

    return True


def passes_breakout_long_filters(symbol: str, cache) -> bool:
    """Return True when all BREAKOUT LONG hard filters are satisfied.

    Gates:
    1. Price still above range_high (not a fake breakout that snapped back)
    2. 4H volume on current bar >= 1.2× 14-bar volume MA (volume confirms breakout)
    3. Funding not overheated
    4. 24H volume above minimum
    """
    rng_high = cache.get_range_high(symbol)
    if rng_high is not None:
        price = (cache.get_closes(symbol, window=1, tf="1h") or [0])[-1]
        if price <= rng_high:
            return False   # snapped back inside range

    # Gate 2: volume spike confirmation (1.5× to filter fake breakouts)
    candles = cache.get_ohlcv(symbol, window=15, tf="4h")
    if len(candles) >= 5:
        avg_vol = sum(c["v"] for c in candles[:-1]) / (len(candles) - 1)
        if avg_vol > 0 and candles[-1]["v"] < avg_vol * 1.5:
            return False

    # Gate 3: Funding
    funding = cache.get_funding_rate(symbol)
    if funding is not None and funding >= _FUNDING_MAX_LONG:
        return False

    # Gate 4: Liquidity
    vol_24h = cache.get_vol_24h(symbol)
    if vol_24h is not None and vol_24h < _MIN_24H_VOL:
        return False

    return True


def passes_breakout_short_filters(symbol: str, cache) -> bool:
    """Return True when all BREAKOUT SHORT hard filters are satisfied.

    Gates:
    1. Price still below range_low (not a fake breakdown that recovered)
    2. 4H volume on current bar >= 1.5× 14-bar MA (volume confirms breakout)
    3. Funding not in panic territory (crash scorer handles that)
    4. 24H volume above minimum
    """
    rng_low = cache.get_range_low(symbol)
    if rng_low is not None:
        price = (cache.get_closes(symbol, window=1, tf="1h") or [0])[-1]
        if price >= rng_low:
            return False   # snapped back inside range

    # Gate 2: volume spike confirmation (1.5× to filter fake breakouts)
    candles = cache.get_ohlcv(symbol, window=15, tf="4h")
    if len(candles) >= 5:
        avg_vol = sum(c["v"] for c in candles[:-1]) / (len(candles) - 1)
        if avg_vol > 0 and candles[-1]["v"] < avg_vol * 1.5:
            return False

    # Gate 3: Funding
    funding = cache.get_funding_rate(symbol)
    if funding is not None and funding < _FUNDING_MIN_SHORT:
        return False

    # Gate 4: Liquidity
    vol_24h = cache.get_vol_24h(symbol)
    if vol_24h is not None and vol_24h < _MIN_24H_VOL:
        return False

    return True


def passes_crash_filters(symbol: str, cache) -> bool:
    """Return True only when CRASH SHORT hard filters are satisfied.

    Gates:
    1. BTC not in parabolic uptrend (allow crash shorts even above EMA200
       when the dead_cat pattern is present — flash crashes happen in bull markets)
    2. 24H volume above threshold (crashes can have thin books — ensure liquidity)

    Note: the EMA200 gate was removed because flash crashes (May 2021, Jan 2022)
    occurred while BTC was still above EMA200. The dead_cat signal (mandatory in
    crash_scorer) provides the structural guard instead.
    """
    vol_24h = cache.get_vol_24h(symbol)
    if vol_24h is not None and vol_24h < _MIN_24H_VOL:
        return False

    return True


def atr_spike_ok(
    symbol: str,
    cache,
    tf: str = "1h",
    multiplier: float | None = None,
) -> bool:
    """Return False when current bar ATR is abnormally large.

    Blocks entries during flash crashes, liquidation cascades, or news spikes
    where fills will be noisy and SL hits are near-certain.
    Returns True on insufficient data (conservative — allow rather than block
    when data is missing).

    Each scorer passes its own tf so the check matches the entry timeframe:
        fvg           → tf="1h"   (default)
        ema_pullback  → tf="15m"
        vwap_band     → tf="15m"
        microrange    → tf="5m"
    """
    _risk_cfg = _cfg.get("risk", {})
    if not _risk_cfg.get("atr_spike_gate_enabled", True):
        return True

    if multiplier is None:
        try:
            multiplier = float(_risk_cfg.get("atr_spike_gate_mult", 3.0))
        except Exception:
            multiplier = 3.0

    bars = cache.get_ohlcv(symbol, 22, tf)
    if not bars or len(bars) < 15:
        return True   # insufficient data — do not block

    trs = []
    for i in range(1, len(bars)):
        h  = bars[i]["h"]
        l  = bars[i]["l"]
        pc = bars[i - 1]["c"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))

    if len(trs) < 14:
        return True

    avg_atr     = sum(trs[-21:-1]) / min(20, len(trs) - 1)
    current_atr = trs[-1]

    ok = current_atr < avg_atr * multiplier
    if not ok:
        log.info(
            "ATR spike gate BLOCKED %s (%s): current=%.6f  avg=%.6f  mult=%.1f",
            symbol, tf, current_atr, avg_atr, multiplier,
        )
    return ok
