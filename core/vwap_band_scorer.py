"""VWAP Band Reversion scorer — mean-reversion entries at ±2σ VWAP bands on 15m.

Strategy logic
--------------
1. Price touches the ±2σ band and closes back inside on 15m (band rejection).
2. Score three equally-weighted signals (0.33 each, max 1.0):
       band_touch      — price touched ±2σ and closed back inside (HARD gate)
       rsi_confirm     — RSI(14) ≤ 35 (LONG) or ≥ 65 (SHORT) on 15m
       regime_aligned  — regime is RANGE or TREND (mean reversion works in both)
3. Hard gates (block fire, not scored):
       band_touch  — must be True (checked via check_vwap_long/short)
       regime_ok   — CRASH blocks LONG, PUMP blocks SHORT
       not_trending — 4H ADX < adx_max (blocks entries in strong trends where
                      bands keep expanding and reversion fails systematically)
4. Fire when score ≥ 0.67 (2 of 3), RR ≥ 1.5× (VWAP must be ≥ 1.5× SL dist), cooldown clear.

SL/TP (VWAP-anchored):
    LONG:  SL = lower_2_band × (1 − sl_buffer_pct)
           TP = vwap_mid   (the mean-reversion target)
    SHORT: SL = upper_2_band × (1 + sl_buffer_pct)
           TP = vwap_mid
    Min RR: |vwap - entry| / |entry - SL| ≥ 1.5 — if VWAP is too close, skip.

Output dict keys: symbol, regime, direction, score, signals, fire, vb_stop, vb_tp
"""
import logging
import os
import yaml
from datetime import datetime, timezone

from core.cooldown_store import CooldownStore
from core.filter import atr_spike_ok

log = logging.getLogger(__name__)

_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "config.yaml")
with open(_CONFIG_PATH) as _f:
    _cfg = yaml.safe_load(_f)

_VB_CFG = _cfg.get("vwap_band", {})

_LONG_ONLY     = bool(_VB_CFG.get("long_only", True))
_COOLDOWN_SECS = float(_VB_CFG.get("cooldown_mins",  30)) * 60.0
_THRESHOLD     = 0.67   # 2 of 3 scored signals required
_SL_BUFFER_PCT = float(_VB_CFG.get("sl_buffer_pct",  0.002))
_MIN_RR        = 1.5    # VWAP must be at least 1.5× SL distance away
_ADX_MAX       = float(_VB_CFG.get("adx_max",        30.0))
_VB_ATR_MULT   = _VB_CFG.get("sl_atr_mult", {"tier1": 1.2, "tier2": 1.5, "tier3": 2.0, "base": 1.5})
_VB_MIN_SL_PCT = float(_VB_CFG.get("min_sl_pct", 0.003))

_SF_CFG          = _cfg.get("session_filter", {})
_SESSION_ENABLED = bool(_SF_CFG.get("enabled", True))
_BLOCK_SATURDAY  = bool(_SF_CFG.get("block_saturday", True))

_BTC_GATE_CFG      = _cfg.get("btc_direction_gate", {})
_BTC_GATE_ENABLED  = bool(_BTC_GATE_CFG.get("enabled", True))
_BTC_FALL_THRESHOLD = float(_BTC_GATE_CFG.get("fall_threshold", -0.0003))
_BTC_RISE_THRESHOLD = float(_BTC_GATE_CFG.get("rise_threshold",  0.0003))

_cd = CooldownStore("VWAPBAND")

# Regimes where mean reversion at VWAP bands works reliably
_GOOD_REGIMES = frozenset({"RANGE", "TREND"})


# ── Cooldown helpers ───────────────────────────────────────────────────────────

def is_on_cooldown(symbol: str) -> bool:
    return _cd.is_active(symbol)


def set_cooldown(symbol: str) -> None:
    _cd.set(symbol, _COOLDOWN_SECS)
    log.debug("VWAP Band cooldown set for %s (%.0f min)", symbol, _COOLDOWN_SECS / 60)


def cooldown_remaining(symbol: str) -> float:
    return _cd.remaining(symbol)


def clear_cooldown(symbol: str) -> None:
    """Clear scorer cooldown for symbol — used by backtest engines."""
    _cd.clear(symbol)


# ── Gate helpers ───────────────────────────────────────────────────────────────

def _regime_str(symbol: str, cache) -> str:
    try:
        from core.regime_detector import detect_regime
        return str(detect_regime(symbol, cache))
    except Exception:
        return ""


def _regime_ok(regime: str, direction: str) -> bool:
    """False when macro regime fundamentally opposes the trade direction."""
    if regime == "CRASH" and direction == "LONG":
        return False
    if regime == "PUMP" and direction == "SHORT":
        return False
    return True


def _adx_below_max(symbol: str, cache) -> bool:
    """True when 4H ADX < adx_max — confirms we are NOT in a strong trend.

    In strong trends (ADX > 30), the VWAP bands keep expanding outward and price
    rarely reverts — fading band extensions loses money systematically.
    Falls back to True (allow trade) when ADX data is unavailable.
    """
    try:
        from core.regime_detector import get_adx_info
        info = get_adx_info(symbol, cache, tf="4h")
        adx  = info.get("adx", 0.0)
        if adx == 0.0:
            return True   # no data — conservative: allow trade
        return adx < _ADX_MAX
    except Exception:
        return True


# ── Session / BTC gate helpers ────────────────────────────────────────────────

def _session_ok(ts_ms: int) -> bool:
    """Block only truly dead trading windows: Saturday + 22:00–00:00 UTC."""
    if not _SESSION_ENABLED:
        return True
    dt      = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
    hour    = dt.hour
    weekday = dt.weekday()
    if _BLOCK_SATURDAY and weekday == 5:
        return False
    if 22 <= hour < 24:
        return False
    return True


def _vwap_btc_direction_ok(direction: str, cache) -> bool:
    """Veto gate: False only when BTC is actively moving against the trade direction."""
    if not _BTC_GATE_ENABLED:
        return True
    try:
        from signals.trend.ema_pullback_15m import _ema
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
    except Exception:
        return True


# ── ATR-based SL helper ───────────────────────────────────────────────────────

def _vwap_band_sl(entry: float, direction: str, bars_15m: list[dict],
                  tier: str = "tier2") -> float:
    """ATR-based SL for VWAP band entries.

    VWAP Band SL must be outside ATR noise.  The old band-distance SL
    was 0.32% on XRP when ATR was 0.57% — guaranteed noise stop.

    New formula:
      SL = entry ± max(ATR × mult, entry × min_sl_pct)
    """
    atr_mult = float(_VB_ATR_MULT.get(tier, _VB_ATR_MULT.get("base", 1.5)))

    if len(bars_15m) >= 15:
        trs = []
        for i in range(1, len(bars_15m)):
            h, l, pc = bars_15m[i]["h"], bars_15m[i]["l"], bars_15m[i - 1]["c"]
            trs.append(max(h - l, abs(h - pc), abs(l - pc)))
        atr = sum(trs[-14:]) / 14 if len(trs) >= 14 else sum(trs) / len(trs) if trs else 0.0
    else:
        atr = 0.0

    stop_dist = max(atr * atr_mult, entry * _VB_MIN_SL_PCT)

    if direction == "LONG":
        return round(entry - stop_dist, 8)
    else:
        return round(entry + stop_dist, 8)


# ── Scorer ────────────────────────────────────────────────────────────────────

async def score(symbol: str, cache) -> list[dict]:
    """Score *symbol* for VWAP band reversion setups on 15m.

    Returns a list of score dicts (up to one LONG + one SHORT candidate).
    Standard keys: symbol, regime, direction, score, signals, fire
    Strategy keys: vb_stop, vb_tp  (executor reads via VWAPBAND preset-levels)
    """
    from signals.range.vwap_bands import (
        check_vwap_long,
        check_vwap_short,
        get_vwap_levels,
    )
    from signals.volume_momentum import VolumeContext, get_volume_params, extreme_volatility

    # Extreme volatility gate — flat during flash crashes
    _ev_cfg = _cfg.get("risk", {}).get("extreme_vol_gate", {})
    if _ev_cfg.get("enabled", True):
        bars_4h_ev = cache.get_ohlcv(symbol, window=55, tf="4h")
        if extreme_volatility(bars_4h_ev,
                              lookback=int(_ev_cfg.get("lookback_bars", 50)),
                              spike_mult=float(_ev_cfg.get("spike_multiplier", 3.0))):
            log.info("VWAP Band blocked — extreme volatility on %s", symbol)
            return []

    cool_ok = not is_on_cooldown(symbol)
    regime  = _regime_str(symbol, cache)
    adx_ok  = _adx_below_max(symbol, cache)

    # Dynamic volume params — VWAP Band uses 15m bars; regime-aware thresholds
    vol_ctx    = VolumeContext(symbol=symbol, regime=regime or "RANGE", timeframe="15m", cache=cache)
    vol_params = get_volume_params(vol_ctx)
    bars_15m_dyn = cache.get_ohlcv(symbol, window=25, tf="15m") or []
    vol_spike_ok = vol_params.spike(bars_15m_dyn)
    rvol_ok      = vol_params.rvol_ok(bars_15m_dyn)
    momentum_ok  = not vol_params.increasing(bars_15m_dyn)

    # Current price for RR calculation
    closes_1m = cache.get_closes(symbol, window=1, tf="1m")
    price = closes_1m[-1] if closes_1m else 0.0
    if price == 0.0:
        bars_15m = cache.get_ohlcv(symbol, window=1, tf="15m")
        price = bars_15m[-1]["c"] if bars_15m else 0.0
    if price == 0.0:
        return []

    results: list[dict] = []

    # ── LONG: lower_2 band touch + rejection ─────────────────────────────────
    band_long = check_vwap_long(symbol, cache)
    if band_long:
        levels = get_vwap_levels(symbol, cache, "LONG")
        if levels is not None:
            vwap_mid, lower_2, std_dev = levels

            regime_gate  = _regime_ok(regime, "LONG")
            regime_align = regime in _GOOD_REGIMES

            # RSI already baked into check_vwap_long, surface it as named signal
            # Re-derive RSI value for the signals dict
            bars_15m  = cache.get_ohlcv(symbol, window=35, tf="15m")
            closes_15m = [b["c"] for b in bars_15m] if bars_15m else []
            rsi_val   = _rsi_from_closes(closes_15m)
            rsi_ok    = rsi_val <= float(_VB_CFG.get("rsi_long_max", 35.0))

            signals = {
                "band_touch":     True,         # always True here (hard gate confirmed)
                "rsi_confirm":    rsi_ok,
                "regime_aligned": regime_align,
                "vol_spike_ok":   vol_spike_ok,
                "rvol_ok":        rvol_ok,
                "no_momentum":    momentum_ok,
            }
            score_val = round(sum(1.0 / 3.0 for v in (
                signals["band_touch"],
                signals["rsi_confirm"],
                signals["regime_aligned"],
            ) if v), 4)

            from core.symbol_config import get_symbol_tier
            bars_15m_sl = cache.get_ohlcv(symbol, window=20, tf="15m") or []
            sl   = _vwap_band_sl(price, "LONG", bars_15m_sl, tier=get_symbol_tier(symbol))
            tp   = vwap_mid
            dist = abs(price - sl)
            rr   = abs(tp - price) / dist if dist > 0 else 0.0

            # Hard gate: RVOL below minimum = time-of-day noise
            # Hard gate: increasing volume at band = breakout, not reversion
            bars_15m_ts = cache.get_ohlcv(symbol, window=1, tf="15m") or []
            current_ts  = bars_15m_ts[-1]["ts"] if bars_15m_ts else 0
            session_gate = _session_ok(current_ts) if current_ts > 0 else True
            btc_gate     = _vwap_btc_direction_ok("LONG", cache) if symbol != "BTCUSDT" else True
            from core.weekly_trend_gate import weekly_allows_long
            spike_ok  = atr_spike_ok(symbol, cache, tf="15m")
            weekly_ok = weekly_allows_long("vwap_band", cache)
            signals["atr_spike_ok"]   = spike_ok
            signals["weekly_gate_ok"] = weekly_ok
            fire = (
                score_val >= _THRESHOLD
                and regime_gate
                and adx_ok
                and spike_ok
                and rr >= _MIN_RR
                and rvol_ok
                and momentum_ok
                and cool_ok
                and session_gate
                and btc_gate
                and weekly_ok
            )

            results.append({
                "symbol":    symbol,
                "regime":    "VWAPBAND",
                "direction": "LONG",
                "score":     score_val,
                "signals":   {**signals,
                              "regime_ok":    regime_gate,
                              "not_trending": adx_ok},
                "fire":      fire,
                "vb_stop":   round(sl, 8),
                "vb_tp":     round(tp, 8),
            })

    # ── SHORT: upper_2 band touch + rejection ────────────────────────────────
    # ── LONG-only gate ────────────────────────────────────────────────────────
    # Backtest confirmed: VWAP SHORT WR=0.5%, PF=0.01 across 400+ trades.
    # Upper band touch in trending or ranging market = price continues up, not mean-reverts.
    # Only allow SHORT when config explicitly sets long_only: false.
    if _LONG_ONLY:
        return results

    band_short = check_vwap_short(symbol, cache)
    if band_short:
        levels = get_vwap_levels(symbol, cache, "SHORT")
        if levels is not None:
            vwap_mid, upper_2, std_dev = levels

            regime_gate  = _regime_ok(regime, "SHORT")
            regime_align = regime in _GOOD_REGIMES

            bars_15m   = cache.get_ohlcv(symbol, window=35, tf="15m")
            closes_15m = [b["c"] for b in bars_15m] if bars_15m else []
            rsi_val    = _rsi_from_closes(closes_15m)
            rsi_ok     = rsi_val >= float(_VB_CFG.get("rsi_short_min", 65.0))

            signals = {
                "band_touch":     True,
                "rsi_confirm":    rsi_ok,
                "regime_aligned": regime_align,
                "vol_spike_ok":   vol_spike_ok,
                "rvol_ok":        rvol_ok,
                "no_momentum":    momentum_ok,
            }
            score_val = round(sum(1.0 / 3.0 for v in (
                signals["band_touch"],
                signals["rsi_confirm"],
                signals["regime_aligned"],
            ) if v), 4)

            from core.symbol_config import get_symbol_tier
            bars_15m_sl = cache.get_ohlcv(symbol, window=20, tf="15m") or []
            sl   = _vwap_band_sl(price, "SHORT", bars_15m_sl, tier=get_symbol_tier(symbol))
            tp   = vwap_mid
            dist = abs(sl - price)
            rr   = abs(price - tp) / dist if dist > 0 else 0.0

            bars_15m_ts2 = cache.get_ohlcv(symbol, window=1, tf="15m") or []
            current_ts2  = bars_15m_ts2[-1]["ts"] if bars_15m_ts2 else 0
            session_gate2 = _session_ok(current_ts2) if current_ts2 > 0 else True
            btc_gate2     = _vwap_btc_direction_ok("SHORT", cache) if symbol != "BTCUSDT" else True
            fire = (
                score_val >= _THRESHOLD
                and regime_gate
                and adx_ok
                and rr >= _MIN_RR
                and rvol_ok
                and momentum_ok
                and cool_ok
                and session_gate2
                and btc_gate2
            )

            results.append({
                "symbol":    symbol,
                "regime":    "VWAPBAND",
                "direction": "SHORT",
                "score":     score_val,
                "signals":   {**signals,
                              "regime_ok":    regime_gate,
                              "not_trending": adx_ok},
                "fire":      fire,
                "vb_stop":   round(sl, 8),
                "vb_tp":     round(tp, 8),
            })

    return results


# ── RSI helper (scorer-local, avoids re-importing signal module) ──────────────

def _rsi_from_closes(closes: list[float], period: int = 14) -> float:
    """Wilder RSI from closes list.  Returns 50.0 on insufficient data."""
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
