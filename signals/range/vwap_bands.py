"""Rolling VWAP ±σ bands on 15m candles — mean-reversion from outer bands to VWAP.

Strategy concept
----------------
Price extending 2 standard deviations from its volume-weighted average price is a
statistically extreme state (~95th percentile assuming normal distribution).
Institutional algorithms routinely fade 2σ extensions back toward the VWAP midline,
making these zones high-probability mean-reversion entry points.

VWAP formula (rolling window, not anchored)
-------------------------------------------
1. typical_price (tp)  = (high + low + close) / 3
2. VWAP                = Σ(tp × volume) / Σ(volume)   over last `window` bars
3. variance            = Σ(volume × (tp − VWAP)²) / Σ(volume)  (volume-weighted)
4. std_dev             = √variance
5. upper_2 / lower_2   = VWAP ± 2 × std_dev

Entry conditions
----------------
LONG  (lower_2 touch):
  · Last candle low ≤ lower_2  (touched the band)
  · Current close > lower_2    (closed back above — rejection, not breakdown)
  · RSI(14) on 15m ≤ 35        (oversold confirmation)
  · Volume ≤ 1.5× 20-bar avg   (quiet bar = mean reversion, not trending breakdown)

SHORT (upper_2 touch): mirror of above.

All config values from vwap_band: section of config.yaml.
"""
import os
import yaml

_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "config.yaml")
with open(_CONFIG_PATH) as _f:
    _cfg = yaml.safe_load(_f)

_VB_CFG       = _cfg.get("vwap_band", {})
_WINDOW       = int(_VB_CFG.get("window_bars",   20))
_RSI_LONG_MAX = float(_VB_CFG.get("rsi_long_max", 35.0))
_RSI_SHORT_MIN = float(_VB_CFG.get("rsi_short_min", 65.0))
_VOL_MAX_MULT = float(_VB_CFG.get("vol_max_mult",  1.5))
_RSI_PERIOD   = 14
_VOL_AVG_BARS = 20


# ── Math helpers ──────────────────────────────────────────────────────────────

def _rsi(closes: list[float], period: int = _RSI_PERIOD) -> float:
    """Wilder RSI.  Returns 50.0 when insufficient data."""
    if len(closes) < period + 1:
        return 50.0
    gains, losses = [], []
    for i in range(1, len(closes)):
        chg = closes[i] - closes[i - 1]
        gains.append(max(chg, 0.0))
        losses.append(max(-chg, 0.0))
    avg_g = sum(gains[:period]) / period
    avg_l = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_g = (avg_g * (period - 1) + gains[i]) / period
        avg_l = (avg_l * (period - 1) + losses[i]) / period
    if avg_l == 0.0:
        return 100.0
    return 100.0 - (100.0 / (1.0 + avg_g / avg_l))


def _vol_ok(bars: list[dict]) -> bool:
    """True when the last bar is NOT a high-volume breakdown/breakout bar."""
    if len(bars) < _VOL_AVG_BARS + 1:
        return True   # insufficient data — assume ok (conservative: don't block)
    avg = sum(b["v"] for b in bars[-_VOL_AVG_BARS - 1:-1]) / _VOL_AVG_BARS
    if avg == 0.0:
        return True
    return bars[-1]["v"] <= _VOL_MAX_MULT * avg


# ── Core computation ──────────────────────────────────────────────────────────

def compute_vwap_bands(candles: list[dict], window: int = 20) -> dict:
    """Compute rolling VWAP and ±1σ / ±2σ bands over the last `window` candles.

    Uses volume-weighted variance so wider-spread bars carry proportionally more
    weight in the standard deviation calculation.

    Args:
        candles : OHLCV bar dicts with keys h, l, c, v.
        window  : number of most-recent bars used for the rolling calculation.

    Returns:
        dict with keys: vwap, upper_1, upper_2, lower_1, lower_2, std_dev
        Empty dict when volume is zero or candles list is empty.
    """
    bars = candles[-window:] if len(candles) >= window else candles
    if not bars:
        return {}

    cum_pv = 0.0
    cum_v  = 0.0
    for b in bars:
        tp      = (b["h"] + b["l"] + b["c"]) / 3.0
        cum_pv += tp * b["v"]
        cum_v  += b["v"]

    if cum_v == 0.0:
        return {}

    vwap = cum_pv / cum_v

    # Volume-weighted variance: Σ(vol × (tp − vwap)²) / Σ(vol)
    weighted_sq = 0.0
    for b in bars:
        tp           = (b["h"] + b["l"] + b["c"]) / 3.0
        weighted_sq += b["v"] * (tp - vwap) ** 2
    variance = max(weighted_sq / cum_v, 0.0)
    std_dev  = variance ** 0.5

    return {
        "vwap":    vwap,
        "upper_1": vwap + std_dev,
        "upper_2": vwap + 2.0 * std_dev,
        "lower_1": vwap - std_dev,
        "lower_2": vwap - 2.0 * std_dev,
        "std_dev": std_dev,
    }


# ── Public signal functions ───────────────────────────────────────────────────

def check_vwap_long(symbol: str, cache) -> bool:
    """True when price touches the lower_2 band and closes back above it on 15m.

    All four conditions must pass:
      1. Last candle low ≤ lower_2  — touched the band (wicked into oversold zone)
      2. Current close > lower_2    — closed back inside (rejection, not breakdown)
      3. RSI(14) ≤ rsi_long_max     — oversold confirmation
      4. Volume ≤ vol_max_mult × avg — quiet mean-reversion bar, not a dump

    Returns False (never raises) on missing or insufficient cache data.
    """
    bars = cache.get_ohlcv(symbol, window=_WINDOW + _RSI_PERIOD + 5, tf="15m")
    if not bars or len(bars) < _WINDOW + 2:
        return False

    bands = compute_vwap_bands(bars, _WINDOW)
    if not bands:
        return False

    lower_2 = bands["lower_2"]
    current = bars[-1]

    if not (current["l"] <= lower_2):
        return False   # did not touch the band

    if not (current["c"] > lower_2):
        return False   # closed below the band — breakdown, not rejection

    closes = [b["c"] for b in bars]
    if _rsi(closes) > _RSI_LONG_MAX:
        return False

    if not _vol_ok(bars):
        return False

    return True


def check_vwap_short(symbol: str, cache) -> bool:
    """True when price touches the upper_2 band and closes back below it on 15m.

    Mirror of check_vwap_long for the upside:
      1. Last candle high ≥ upper_2  — touched the band
      2. Current close < upper_2     — closed back inside (rejection)
      3. RSI(14) ≥ rsi_short_min     — overbought confirmation
      4. Volume ≤ vol_max_mult × avg — quiet mean-reversion bar, not a breakout

    Returns False (never raises) on missing or insufficient cache data.
    """
    bars = cache.get_ohlcv(symbol, window=_WINDOW + _RSI_PERIOD + 5, tf="15m")
    if not bars or len(bars) < _WINDOW + 2:
        return False

    bands = compute_vwap_bands(bars, _WINDOW)
    if not bands:
        return False

    upper_2 = bands["upper_2"]
    current = bars[-1]

    if not (current["h"] >= upper_2):
        return False   # did not touch the band

    if not (current["c"] < upper_2):
        return False   # closed above the band — breakout, not rejection

    closes = [b["c"] for b in bars]
    if _rsi(closes) < _RSI_SHORT_MIN:
        return False

    if not _vol_ok(bars):
        return False

    return True


def get_vwap_levels(
    symbol: str,
    cache,
    direction: str,
) -> tuple[float, float, float] | None:
    """Return (vwap_mid, band_level, std_dev) for the given direction.

    LONG:  band_level = lower_2  (SL anchor)
    SHORT: band_level = upper_2  (SL anchor)

    Used by vwap_band_scorer to compute strategy-specific SL and TP.
    TP = vwap_mid in both cases (mean-reversion target).
    Returns None when bands cannot be computed.
    """
    bars = cache.get_ohlcv(symbol, window=_WINDOW + 5, tf="15m")
    if not bars or len(bars) < _WINDOW:
        return None

    bands = compute_vwap_bands(bars, _WINDOW)
    if not bands:
        return None

    vwap    = bands["vwap"]
    std_dev = bands["std_dev"]

    if direction == "LONG":
        return vwap, bands["lower_2"], std_dev
    if direction == "SHORT":
        return vwap, bands["upper_2"], std_dev
    return None
