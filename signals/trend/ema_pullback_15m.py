"""15m EMA Pullback signals — high-frequency trend-continuation entries.

Strategy logic
--------------
Uses the 4H trend direction as the macro filter, then enters on 15m pullbacks
to EMA21. Much higher frequency than the 1H version used in MAIN strategy.

Long setup:
  1. 4H: price above EMA50 (macro uptrend) OR 4H EMA21 > EMA50 (short-term momentum up)
  2. 15m: EMA21 > EMA50 (trend intact on entry timeframe)
  3. 15m: price pulled back to EMA21 (within 0.4%) then bounced
  4. 15m: close is now ABOVE EMA21 (confirmed bounce)
  5. RSI 15m in healthy pullback zone 35-60 (not overbought on entry)
  6. Volume on pullback bar ≤ 1.2× average (quiet retreat = weak sellers)

Short setup: mirror image.
"""

import os, yaml
from datetime import datetime, timezone
_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "config.yaml")
with open(_CONFIG_PATH) as _f:
    _cfg = yaml.safe_load(_f)
_EP = _cfg.get("ema_pullback", {})

_EMA_FAST    = 21
_EMA_SLOW    = 50
_RSI_PERIOD  = 14
_TOUCH_PCT       = float(_EP.get("pullback_touch_pct",   0.002))
_MIN_BOUNCE_BODY = float(_EP.get("min_bounce_body_pct",  0.002))
_VOL_QUIET_MULT  = float(_EP.get("vol_quiet_mult",       0.8))
_RSI_LONG_MIN    = float(_EP.get("rsi_long_min",         30.0))
_RSI_LONG_MAX    = float(_EP.get("rsi_long_max",         50.0))
_RSI_SHORT_MIN   = float(_EP.get("rsi_short_min",        50.0))
_RSI_SHORT_MAX   = float(_EP.get("rsi_short_max",        70.0))

_SL_ATR_MULT = _EP.get("sl_atr_mult", {"tier1": 1.5, "tier2": 2.0, "tier3": 2.5, "base": 2.0})
_MIN_SL_PCT  = float(_EP.get("min_sl_pct", 0.003))

_SF_CFG          = _cfg.get("session_filter", {})
_SESSION_ENABLED = bool(_SF_CFG.get("enabled", True))
_BLOCK_SATURDAY  = bool(_SF_CFG.get("block_saturday", True))

_BTC_GATE_CFG        = _cfg.get("btc_direction_gate", {})
_BTC_GATE_ENABLED    = bool(_BTC_GATE_CFG.get("enabled", True))
_BTC_FALL_THRESHOLD  = float(_BTC_GATE_CFG.get("fall_threshold", -0.0003))
_BTC_RISE_THRESHOLD  = float(_BTC_GATE_CFG.get("rise_threshold",  0.0003))

_ADX_SHORT_MIN = float(_EP.get("adx_short_min", 30.0))
_ADX_LONG_MIN  = float(_EP.get("adx_long_min",  22.0))


def _session_ok(ts_ms: int) -> bool:
    """Block only truly dead trading windows.

    Blocked:
      - Saturday (confirmed worst day — low volume, wide spreads)
      - Dead zone: 22:00–00:00 UTC (2 hours only)
    """
    if not _SESSION_ENABLED:
        return True
    dt      = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
    hour    = dt.hour
    weekday = dt.weekday()   # 0=Mon, 5=Sat, 6=Sun
    if _BLOCK_SATURDAY and weekday == 5:
        return False
    if 22 <= hour < 24:
        return False
    return True


def _adx_strength_ok(symbol: str, cache, direction: str) -> bool:
    """For SHORT: require ADX > adx_short_min (strong confirmed downtrend).
    For LONG: require ADX > adx_long_min (bull markets more forgiving).

    Weak bears (ADX 22-30) are choppy — SHORT pullbacks fail there.
    Strong bears (ADX > 30) produce clean sustained downtrends.
    """
    bars_4h = cache.get_ohlcv(symbol, window=30, tf="4h")
    if len(bars_4h) < 14:
        return True   # insufficient data — don't block

    highs  = [b["h"] for b in bars_4h]
    lows   = [b["l"] for b in bars_4h]
    closes = [b["c"] for b in bars_4h]

    period = 14
    trs, plus_dm, minus_dm = [], [], []
    for i in range(1, len(bars_4h)):
        tr  = max(highs[i] - lows[i], abs(highs[i] - closes[i-1]), abs(lows[i] - closes[i-1]))
        pdm = max(highs[i] - highs[i-1], 0) if (highs[i] - highs[i-1]) > (lows[i-1] - lows[i]) else 0
        mdm = max(lows[i-1] - lows[i], 0)   if (lows[i-1] - lows[i]) > (highs[i] - highs[i-1]) else 0
        trs.append(tr); plus_dm.append(pdm); minus_dm.append(mdm)

    if len(trs) < period:
        return True

    atr_s = sum(trs[:period])
    pdm_s = sum(plus_dm[:period])
    mdm_s = sum(minus_dm[:period])
    for i in range(period, len(trs)):
        atr_s = atr_s - atr_s / period + trs[i]
        pdm_s = pdm_s - pdm_s / period + plus_dm[i]
        mdm_s = mdm_s - mdm_s / period + minus_dm[i]

    pdi = 100 * pdm_s / atr_s if atr_s > 0 else 0
    mdi = 100 * mdm_s / atr_s if atr_s > 0 else 0
    dx  = 100 * abs(pdi - mdi) / (pdi + mdi) if (pdi + mdi) > 0 else 0

    if direction == "SHORT":
        return dx > _ADX_SHORT_MIN
    return dx > _ADX_LONG_MIN


def _btc_direction_ok(direction: str, cache) -> bool:
    """Return False ONLY when BTC is actively moving against the trade direction.

    LONG:  block only if BTC is clearly falling (slope < fall_threshold)
    SHORT: block only if BTC is clearly rising (slope > rise_threshold)
    Flat BTC: always allow — flat BTC = alt season conditions.
    """
    if not _BTC_GATE_ENABLED:
        return True
    bars_1h = cache.get_ohlcv("BTCUSDT", window=25, tf="1h")
    if len(bars_1h) < 22:
        return True
    closes     = [b["c"] for b in bars_1h]
    ema20_now  = _ema(closes, 20)
    ema20_prev = _ema(closes[:-3], 20)
    slope = (ema20_now - ema20_prev) / ema20_prev if ema20_prev > 0 else 0
    if direction == "LONG"  and slope < _BTC_FALL_THRESHOLD:
        return False
    if direction == "SHORT" and slope > _BTC_RISE_THRESHOLD:
        return False
    return True


def _ema(closes: list[float], period: int) -> float:
    if len(closes) < period:
        return 0.0
    k   = 2.0 / (period + 1)
    ema = sum(closes[:period]) / period
    for c in closes[period:]:
        ema = c * k + ema * (1 - k)
    return ema


def _atr(bars: list[dict], period: int = 14) -> float:
    """Average True Range over last N bars."""
    if len(bars) < period + 1:
        return 0.0
    trs = []
    for i in range(1, len(bars)):
        high       = bars[i]["h"]
        low        = bars[i]["l"]
        prev_close = bars[i - 1]["c"]
        trs.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
    return sum(trs[-period:]) / period


def _rsi(closes: list[float], period: int = _RSI_PERIOD) -> float:
    if len(closes) < period + 1:
        return 50.0
    gains, losses = [], []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gains.append(max(diff, 0.0))
        losses.append(max(-diff, 0.0))
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0.0:
        return 100.0
    return 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)


def _htf_bullish(symbol: str, cache) -> bool:
    """4H macro bias is bullish: close above 4H EMA50 OR 4H EMA21 > EMA50."""
    bars_4h = cache.get_ohlcv(symbol, window=_EMA_SLOW + 5, tf="4h")
    if len(bars_4h) < _EMA_SLOW:
        return False
    closes_4h = [b["c"] for b in bars_4h]
    ema50_4h  = _ema(closes_4h, _EMA_SLOW)
    ema21_4h  = _ema(closes_4h, _EMA_FAST)
    return closes_4h[-1] > ema50_4h or ema21_4h > ema50_4h


def _htf_bearish(symbol: str, cache) -> bool:
    """4H macro bias is bearish: close below 4H EMA50 AND 4H EMA21 < EMA50."""
    bars_4h = cache.get_ohlcv(symbol, window=_EMA_SLOW + 5, tf="4h")
    if len(bars_4h) < _EMA_SLOW:
        return False
    closes_4h = [b["c"] for b in bars_4h]
    ema50_4h  = _ema(closes_4h, _EMA_SLOW)
    ema21_4h  = _ema(closes_4h, _EMA_FAST)
    return closes_4h[-1] < ema50_4h and ema21_4h < ema50_4h


def check_ema15m_pullback_long(symbol: str, cache) -> bool:
    """True when 4H is bullish and 15m price bounces off EMA21 pullback."""
    if not _htf_bullish(symbol, cache):
        return False

    bars = cache.get_ohlcv(symbol, window=_EMA_SLOW + 10, tf="15m")
    if len(bars) < _EMA_SLOW + 2:
        return False

    closes = [b["c"] for b in bars]
    ema21  = _ema(closes, _EMA_FAST)
    ema50  = _ema(closes, _EMA_SLOW)

    if ema21 <= 0 or ema50 <= 0:
        return False

    # 15m trend intact: EMA21 > EMA50
    if ema21 <= ema50:
        return False

    price = closes[-1]

    # Price recently touched EMA21 (within TOUCH_PCT) and is now bouncing above it
    # Check: previous bar low was near EMA21, current close is above EMA21
    prev_bar = bars[-2]
    prev_low = prev_bar["l"]
    touch    = abs(prev_low - ema21) / ema21 <= _TOUCH_PCT or \
               abs(closes[-2] - ema21) / ema21 <= _TOUCH_PCT

    if not touch:
        return False

    # Close must be meaningfully above EMA21 (≥ 0.2% — not a marginal cross)
    if (price - ema21) / ema21 < _MIN_BOUNCE_BODY:
        return False

    # ADX strength gate — require confirmed trend
    if not _adx_strength_ok(symbol, cache, "LONG"):
        return False

    # RSI in healthy pullback zone
    rsi = _rsi(closes)
    if not (_RSI_LONG_MIN <= rsi <= _RSI_LONG_MAX):
        return False

    # Volume checks (last 21 bars including current)
    vols = [b["v"] for b in bars[-21:]]
    if len(vols) >= 20:
        avg_vol    = sum(vols[:-1]) / len(vols[:-1])
        pullback_v = bars[-2]["v"]   # volume of the pullback/touch bar
        bounce_v   = bars[-1]["v"]   # volume of the current bounce bar
        # Pullback must be quiet (weak sellers)
        if avg_vol > 0 and pullback_v > avg_vol * _VOL_QUIET_MULT:
            return False
        # Bounce bar must have more volume than the pullback bar (buyers stepping in)
        if bounce_v <= pullback_v:
            return False

    # RVOL gate — skip low-activity entries
    from signals.volume_momentum import VolumeContext, get_volume_params
    vol_ctx    = VolumeContext(symbol=symbol, regime="TREND", timeframe="15m", cache=cache)
    vol_params = get_volume_params(vol_ctx)
    if not vol_params.rvol_ok(bars):
        return False

    # Session gate — skip off-hours and Saturday
    entry_ts = bars[-1].get("ts", 0)
    if entry_ts > 0 and not _session_ok(entry_ts):
        return False

    # BTC direction gate for alt coins
    if symbol != "BTCUSDT" and not _btc_direction_ok("LONG", cache):
        return False

    return True


def check_ema15m_pullback_short(symbol: str, cache) -> bool:
    """True when 4H is bearish and 15m price bounces down off EMA21 rally."""
    if not _htf_bearish(symbol, cache):
        return False

    bars = cache.get_ohlcv(symbol, window=_EMA_SLOW + 10, tf="15m")
    if len(bars) < _EMA_SLOW + 2:
        return False

    closes = [b["c"] for b in bars]
    ema21  = _ema(closes, _EMA_FAST)
    ema50  = _ema(closes, _EMA_SLOW)

    if ema21 <= 0 or ema50 <= 0:
        return False

    # 15m downtrend: EMA21 < EMA50
    if ema21 >= ema50:
        return False

    price = closes[-1]

    # Previous bar high was near EMA21, current close is below EMA21
    prev_bar  = bars[-2]
    prev_high = prev_bar["h"]
    touch     = abs(prev_high - ema21) / ema21 <= _TOUCH_PCT or \
                abs(closes[-2] - ema21) / ema21 <= _TOUCH_PCT

    if not touch:
        return False

    # Close must be meaningfully below EMA21 (≥ 0.2% — not a marginal cross)
    if (ema21 - price) / ema21 < _MIN_BOUNCE_BODY:
        return False

    # ADX strength gate — SHORT only in confirmed strong downtrend
    if not _adx_strength_ok(symbol, cache, "SHORT"):
        return False

    rsi = _rsi(closes)
    if not (_RSI_SHORT_MIN <= rsi <= _RSI_SHORT_MAX):
        return False

    vols = [b["v"] for b in bars[-21:]]
    if len(vols) >= 20:
        avg_vol    = sum(vols[:-1]) / len(vols[:-1])
        pullback_v = bars[-2]["v"]
        bounce_v   = bars[-1]["v"]
        # Pullback must be quiet (weak buyers)
        if avg_vol > 0 and pullback_v > avg_vol * _VOL_QUIET_MULT:
            return False
        # Bounce bar must have more volume than the pullback bar (sellers stepping in)
        if bounce_v <= pullback_v:
            return False

    # RVOL gate — skip low-activity entries
    from signals.volume_momentum import VolumeContext, get_volume_params
    vol_ctx    = VolumeContext(symbol=symbol, regime="TREND", timeframe="15m", cache=cache)
    vol_params = get_volume_params(vol_ctx)
    if not vol_params.rvol_ok(bars):
        return False

    # Session gate — skip off-hours and Saturday
    entry_ts = bars[-1].get("ts", 0)
    if entry_ts > 0 and not _session_ok(entry_ts):
        return False

    # BTC direction gate for alt coins
    if symbol != "BTCUSDT" and not _btc_direction_ok("SHORT", cache):
        return False

    return True


def get_ema15m_long_levels(symbol: str, cache, tier: str = "tier2") -> tuple[float, float, float]:
    """Return (entry, stop, tp) for long entry using ATR-based SL.

    SL = entry - max(ATR × atr_mult, ema21_dist × 1.5, entry × min_sl_pct)
    This ensures SL is always outside the noise floor regardless of EMA distance.

    ATR multipliers by tier (from config sl_atr_mult):
      tier1 (BTC):  1.5× ATR  — cleanest trends
      tier2:        2.0× ATR  — moderate noise
      tier3:        2.5× ATR  — high volatility alts
    """
    atr_mult = float(_SL_ATR_MULT.get(tier, _SL_ATR_MULT.get("base", 2.0)))

    bars = cache.get_ohlcv(symbol, window=_EMA_FAST + 20, tf="15m")
    if len(bars) < _EMA_FAST + 2:
        return 0.0, 0.0, 0.0

    closes   = [b["c"] for b in bars]
    ema21    = _ema(closes, _EMA_FAST)
    entry    = closes[-1]

    atr      = _atr(bars, period=14)
    ema_dist = abs(entry - ema21)
    stop_dist = max(atr * atr_mult, ema_dist * 1.5, entry * _MIN_SL_PCT)

    stop = round(entry - stop_dist, 8)
    if stop >= entry:
        return 0.0, 0.0, 0.0
    tp = round(entry + stop_dist * float(_EP.get("rr_ratio", 1.5)), 8)
    return entry, stop, tp


def get_ema15m_short_levels(symbol: str, cache, tier: str = "tier2") -> tuple[float, float, float]:
    """Return (entry, stop, tp) for short entry using ATR-based SL."""
    atr_mult = float(_SL_ATR_MULT.get(tier, _SL_ATR_MULT.get("base", 2.0)))

    bars = cache.get_ohlcv(symbol, window=_EMA_FAST + 20, tf="15m")
    if len(bars) < _EMA_FAST + 2:
        return 0.0, 0.0, 0.0

    closes   = [b["c"] for b in bars]
    ema21    = _ema(closes, _EMA_FAST)
    entry    = closes[-1]

    atr      = _atr(bars, period=14)
    ema_dist = abs(entry - ema21)
    stop_dist = max(atr * atr_mult, ema_dist * 1.5, entry * _MIN_SL_PCT)

    stop = round(entry + stop_dist, 8)
    if stop <= entry:
        return 0.0, 0.0, 0.0
    tp = round(entry - stop_dist * float(_EP.get("rr_ratio", 1.5)), 8)
    return entry, stop, tp
