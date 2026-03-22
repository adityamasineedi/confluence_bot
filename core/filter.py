"""Hard filters for all trade directions — must all pass before a signal fires."""
import os
import yaml

_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "config.yaml")
with open(_CONFIG_PATH) as _f:
    _cfg = yaml.safe_load(_f)

_FUNDING_MAX_LONG  = _cfg["funding"]["neutral_band"]   # 0.0003
_FUNDING_MIN_SHORT = -_cfg["funding"]["neutral_band"]  # -0.0003
_MIN_24H_VOL       = _cfg["filters"]["min_24h_volume_usdt"]
_BTC               = "BTCUSDT"


# ── BTC EMA-200 check ─────────────────────────────────────────────────────────

def _btc_above_ema200(cache) -> bool:
    """Return True when BTC 4H price is above its 200-period EMA."""
    btc_4h = cache.get_closes(_BTC, window=210, tf="4h")
    if len(btc_4h) < 200:
        return True   # insufficient data → give benefit of doubt
    k   = 2.0 / 201
    ema = sum(btc_4h[:200]) / 200
    for p in btc_4h[200:]:
        ema = p * k + ema * (1 - k)
    return btc_4h[-1] > ema


# ── Public filter functions ───────────────────────────────────────────────────

def passes_trend_long_filters(symbol: str, cache) -> bool:
    """Return True only when all TREND LONG hard filters are satisfied.

    Gates:
    1. BTC 4H close above 200-period EMA (macro must be bullish)
    2. Funding rate not overheated (< 0.0003 — longs not overextended)
    3. 24H volume above minimum liquidity threshold
    """
    # Gate 1: BTC macro
    if not _btc_above_ema200(cache):
        return False

    # Gate 2: Funding not overheated
    funding = cache.get_funding_rate(symbol)
    if funding is not None and funding >= _FUNDING_MAX_LONG:
        return False

    # Gate 3: Liquidity
    vol_24h = cache.get_vol_24h(symbol)
    if vol_24h is not None and vol_24h < _MIN_24H_VOL:
        return False

    return True


def passes_trend_short_filters(symbol: str, cache) -> bool:
    """Return True only when all TREND SHORT hard filters are satisfied.

    Gates:
    1. BTC 4H close below 200-period EMA (macro must be bearish)
    2. Funding not extreme negative (< -0.0003 would mean crash, handled by crash scorer)
    3. 24H volume above minimum threshold
    """
    # Gate 1: BTC macro bearish
    if _btc_above_ema200(cache):
        return False

    # Gate 2: Funding not in panic territory (negative but not extreme)
    funding = cache.get_funding_rate(symbol)
    if funding is not None and funding < _FUNDING_MIN_SHORT:
        return False  # extreme negative → crash scorer handles this

    # Gate 3: Liquidity
    vol_24h = cache.get_vol_24h(symbol)
    if vol_24h is not None and vol_24h < _MIN_24H_VOL:
        return False

    return True


def passes_crash_filters(symbol: str, cache) -> bool:
    """Return True only when CRASH SHORT hard filters are satisfied.

    Gates:
    1. BTC in downtrend (below EMA200)
    2. 24H volume above threshold (crashes can have thin books — ensure liquidity)
    """
    if _btc_above_ema200(cache):
        return False

    vol_24h = cache.get_vol_24h(symbol)
    if vol_24h is not None and vol_24h < _MIN_24H_VOL:
        return False

    return True
