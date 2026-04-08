"""
core/liq_sweep_scorer.py
Liquidity sweep reversal scorer — equal highs/lows stop hunt.

Backtest confirmed:
  LONG:  BTC PF 2.46, BNB PF 2.81, LINK PF 3.74, DOGE PF 7.49
  SHORT: BNB PF 2.22, XRP PF 2.22, DOGE PF 2.56, SUI PF 2.44

SL = wick low/high × (1 ± 0.001) — natural invalidation level.
TP = entry ± |entry - SL| × 2.5
"""
import logging
import os
import yaml

from core.cooldown_store import CooldownStore
from core.filter import atr_spike_ok
from core.vol_ratio import compute_vol_ratio
from core.weekly_trend_gate import weekly_allows_long, weekly_allows_short

log = logging.getLogger(__name__)

_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "config.yaml")
with open(_CONFIG_PATH) as _f:
    _cfg = yaml.safe_load(_f)

_LIQ_CFG       = _cfg.get("liq_sweep", {})
_COOLDOWN_SECS = float(_LIQ_CFG.get("cooldown_mins", 60)) * 60.0
_RR_RATIO      = float(_LIQ_CFG.get("rr_ratio", 2.5))
_SL_BUFFER     = float(_LIQ_CFG.get("sl_buffer_pct", 0.001))
_LOOKBACK      = int(_LIQ_CFG.get("lookback_bars", 30))
_EQ_TOL        = float(_LIQ_CFG.get("eq_level_tol", 0.002))
_MIN_SEP       = int(_LIQ_CFG.get("min_separation_bars", 10))
_VOL_MULT      = float(_LIQ_CFG.get("vol_spike_mult", 1.5))
_THRESHOLD     = float(_LIQ_CFG.get("fire_threshold", 0.75))
_MC_VOL_MAX    = float(_LIQ_CFG.get("mc_vol_ratio_max", 0.0))
_MAX_ENTRY_DRIFT_PCT = float(_LIQ_CFG.get("max_entry_drift_pct", 0.003))

_cd_long  = CooldownStore("LIQ_SWEEP_LONG")
_cd_short = CooldownStore("LIQ_SWEEP_SHORT")


# ── Signal detection ──────────────────────────────────────────────────────────

def _vol_ma(bars: list[dict], period: int = 20) -> float:
    vols = [b["v"] for b in bars[-period:] if b.get("v", 0) > 0]
    return sum(vols) / len(vols) if vols else 0.0


def _swing_lows(lows: list[float]) -> list[tuple[int, float]]:
    """Return (index, value) pairs for local swing lows."""
    result = []
    for i in range(2, len(lows) - 2):
        window = lows[max(0, i-2): i+3]
        if lows[i] == min(window):
            result.append((i, lows[i]))
    return result


def _swing_highs(highs: list[float]) -> list[tuple[int, float]]:
    """Return (index, value) pairs for local swing highs."""
    result = []
    for i in range(2, len(highs) - 2):
        window = highs[max(0, i-2): i+3]
        if highs[i] == max(window):
            result.append((i, highs[i]))
    return result


def _find_equal_lows(bars: list[dict],
                     lookback: int,
                     eq_tol: float,
                     min_sep: int) -> float | None:
    """Return the equal-lows level if found, else None."""
    window = bars[-lookback - 1: -1]
    lows   = [b["l"] for b in window]
    swings = _swing_lows(lows)

    for a in range(len(swings)):
        for b in range(a + 1, len(swings)):
            idx_a, val_a = swings[a]
            idx_b, val_b = swings[b]
            if abs(idx_b - idx_a) < min_sep:
                continue
            if val_a > 0 and abs(val_a - val_b) / val_a <= eq_tol:
                return min(val_a, val_b)
    return None


def _find_equal_highs(bars: list[dict],
                      lookback: int,
                      eq_tol: float,
                      min_sep: int) -> float | None:
    """Return the equal-highs level if found, else None."""
    window = bars[-lookback - 1: -1]
    highs  = [b["h"] for b in window]
    swings = _swing_highs(highs)

    for a in range(len(swings)):
        for b in range(a + 1, len(swings)):
            idx_a, val_a = swings[a]
            idx_b, val_b = swings[b]
            if abs(idx_b - idx_a) < min_sep:
                continue
            if val_a > 0 and abs(val_a - val_b) / val_a <= eq_tol:
                return max(val_a, val_b)
    return None


def check_liq_sweep_long(symbol: str, cache) -> tuple[bool, float]:
    """Returns (sweep_detected, eq_low_level)."""
    bars = cache.get_ohlcv(symbol, window=_LOOKBACK + 5, tf="1h")
    if not bars or len(bars) < _LOOKBACK + 3:
        return False, 0.0

    eq_low = _find_equal_lows(bars, _LOOKBACK, _EQ_TOL, _MIN_SEP)
    if eq_low is None:
        return False, 0.0

    sweep_bar = bars[-1]

    # Must wick below and close above
    if sweep_bar["l"] >= eq_low:
        return False, 0.0
    if sweep_bar["c"] <= eq_low:
        return False, 0.0

    # Volume confirmation
    vm = _vol_ma(bars[:-1], 20)
    if vm > 0 and sweep_bar["v"] < vm * _VOL_MULT:
        return False, 0.0

    return True, eq_low


def check_liq_sweep_short(symbol: str, cache) -> tuple[bool, float]:
    """Returns (sweep_detected, eq_high_level)."""
    bars = cache.get_ohlcv(symbol, window=_LOOKBACK + 5, tf="1h")
    if not bars or len(bars) < _LOOKBACK + 3:
        return False, 0.0

    eq_high = _find_equal_highs(bars, _LOOKBACK, _EQ_TOL, _MIN_SEP)
    if eq_high is None:
        return False, 0.0

    sweep_bar = bars[-1]

    if sweep_bar["h"] <= eq_high:
        return False, 0.0
    if sweep_bar["c"] >= eq_high:
        return False, 0.0

    vm = _vol_ma(bars[:-1], 20)
    if vm > 0 and sweep_bar["v"] < vm * _VOL_MULT:
        return False, 0.0

    return True, eq_high


# ── HTF confirmation ──────────────────────────────────────────────────────────

def _htf_bullish(symbol: str, cache) -> bool:
    bars_4h = cache.get_ohlcv(symbol, window=25, tf="4h")
    if not bars_4h or len(bars_4h) < 22:
        return False
    _live = cache.get_closes(symbol, window=1, tf="1m")
    if _live:
        bars_4h[-1] = {**bars_4h[-1], "c": _live[-1]}
    closes = [b["c"] for b in bars_4h]
    k = 2.0 / (21 + 1)
    ema = sum(closes[:21]) / 21
    for c in closes[21:]:
        ema = c * k + ema * (1 - k)
    return closes[-1] > ema


def _htf_bearish(symbol: str, cache) -> bool:
    bars_4h = cache.get_ohlcv(symbol, window=25, tf="4h")
    if not bars_4h or len(bars_4h) < 22:
        return False
    _live = cache.get_closes(symbol, window=1, tf="1m")
    if _live:
        bars_4h[-1] = {**bars_4h[-1], "c": _live[-1]}
    closes = [b["c"] for b in bars_4h]
    k = 2.0 / (21 + 1)
    ema = sum(closes[:21]) / 21
    for c in closes[21:]:
        ema = c * k + ema * (1 - k)
    return closes[-1] < ema


# ── RSI ───────────────────────────────────────────────────────────────────────

def _rsi_1h(symbol: str, cache) -> float:
    closes = cache.get_closes(symbol, window=16, tf="1h")
    if not closes or len(closes) < 15:
        return 50.0
    gains = losses = 0.0
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        if d > 0:
            gains += d
        else:
            losses -= d
    if losses == 0:
        return 100.0
    return round(100 - 100 / (1 + gains / losses), 1)


# ── Main scorer ───────────────────────────────────────────────────────────────

async def score(symbol: str, cache) -> list[dict]:
    """Score symbol for liquidity sweep setups on 1H.

    Returns up to two dicts — one LONG candidate and one SHORT candidate.
    Standard keys: symbol, regime, direction, score, signals, fire
    Strategy keys: ls_stop, ls_tp
    """
    results = []

    if not atr_spike_ok(symbol, cache, tf="1h"):
        return []

    # Vol ratio gate — block during vol spikes beyond threshold
    if _MC_VOL_MAX > 0:
        bars_vr = cache.get_ohlcv(symbol, window=60, tf="1h")
        if bars_vr and len(bars_vr) >= 54:
            vr = compute_vol_ratio(bars_vr)
            if vr > _MC_VOL_MAX:
                log.debug("Liq sweep blocked — vol ratio %.2f > %.1f on %s",
                          vr, _MC_VOL_MAX, symbol)
                return []

    rsi_val = _rsi_1h(symbol, cache)

    # ── LONG sweep ────────────────────────────────────────────────────────────
    sweep_long, eq_low = check_liq_sweep_long(symbol, cache)

    if sweep_long:
        htf_ok    = _htf_bullish(symbol, cache)
        weekly_ok = weekly_allows_long("liq_sweep", cache)
        rsi_ok    = rsi_val < 55
        cool_ok   = not _cd_long.is_active(symbol)

        score_val = (0.40
                     + (0.35 if htf_ok else 0.0)
                     + (0.25 if rsi_ok else 0.0))

        bars = cache.get_ohlcv(symbol, window=2, tf="1h")
        if bars:
            sweep_bar = bars[-1]
            entry     = sweep_bar["c"]

            # Gate: reject if price has already moved away from the sweep close
            current_price = cache.get_last_price(symbol)
            if current_price > 0 and abs(current_price - entry) / entry > _MAX_ENTRY_DRIFT_PCT:
                log.debug("Liq sweep LONG entry drift too large on %s: swept=%.6f current=%.6f drift=%.3f%%",
                          symbol, entry, current_price, abs(current_price - entry) / entry * 100)
            else:
                sl_dist   = max(entry - sweep_bar["l"] * (1.0 - _SL_BUFFER),
                                entry * 0.002)
                stop = entry - sl_dist
                tp   = entry + sl_dist * _RR_RATIO

                fire = (score_val >= _THRESHOLD
                        and htf_ok and weekly_ok and rsi_ok and cool_ok
                        and stop > 0 and tp > entry)

                if fire:
                    _cd_long.set(symbol, _COOLDOWN_SECS)

                results.append({
                    "symbol":    symbol,
                    "regime":    "liq_sweep",
                    "direction": "LONG",
                    "score":     round(score_val, 4),
                    "signals":   {
                        "sweep_detected": True,
                        "eq_low":         round(eq_low, 6),
                        "htf_bullish":    htf_ok,
                        "weekly_ok":      weekly_ok,
                        "rsi_ok":         rsi_ok,
                        "rsi_val":        rsi_val,
                        "cooldown_ok":    cool_ok,
                    },
                    "fire":    fire,
                    "ls_stop": round(stop, 8),
                    "ls_tp":   round(tp,   8),
                })

    # ── SHORT sweep ───────────────────────────────────────────────────────────
    sweep_short, eq_high = check_liq_sweep_short(symbol, cache)

    if sweep_short:
        htf_ok    = _htf_bearish(symbol, cache)
        weekly_ok = weekly_allows_short("liq_sweep", cache)
        rsi_ok    = rsi_val > 45
        cool_ok   = not _cd_short.is_active(symbol)

        score_val = (0.40
                     + (0.35 if htf_ok else 0.0)
                     + (0.25 if rsi_ok else 0.0))

        bars = cache.get_ohlcv(symbol, window=2, tf="1h")
        if bars:
            sweep_bar = bars[-1]
            entry     = sweep_bar["c"]

            # Gate: reject if price has already moved away from the sweep close
            current_price = cache.get_last_price(symbol)
            if current_price > 0 and abs(current_price - entry) / entry > _MAX_ENTRY_DRIFT_PCT:
                log.debug("Liq sweep SHORT entry drift too large on %s: swept=%.6f current=%.6f drift=%.3f%%",
                          symbol, entry, current_price, abs(current_price - entry) / entry * 100)
            else:
                sl_dist   = max(sweep_bar["h"] * (1.0 + _SL_BUFFER) - entry,
                                entry * 0.002)
                stop = entry + sl_dist
                tp   = entry - sl_dist * _RR_RATIO

                fire = (score_val >= _THRESHOLD
                        and htf_ok and weekly_ok and rsi_ok and cool_ok
                        and stop > 0 and tp < entry)

                if fire:
                    _cd_short.set(symbol, _COOLDOWN_SECS)

                results.append({
                    "symbol":    symbol,
                    "regime":    "liq_sweep",
                    "direction": "SHORT",
                    "score":     round(score_val, 4),
                    "signals":   {
                        "sweep_detected": True,
                        "eq_high":        round(eq_high, 6),
                        "htf_bearish":    htf_ok,
                        "weekly_ok":      weekly_ok,
                        "rsi_ok":         rsi_ok,
                        "rsi_val":        rsi_val,
                        "cooldown_ok":    cool_ok,
                    },
                    "fire":    fire,
                    "ls_stop": round(stop, 8),
                    "ls_tp":   round(tp,   8),
                })

    return results
