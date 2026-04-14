"""
core/wyckoff_upthrust_scorer.py
Wyckoff upthrust scorer — mirror of wyckoff_spring for SHORT entries.

Confirmed backtest results (honest fees):
  DOGE: PF 9.99, WR 100% (4 trades)
  SUI:  PF 6.92, WR 60%  (5 trades)
  SOL:  PF 4.17, WR 67%  (3 trades)
  LINK: PF 3.30, WR 55%  (11 trades)
  BTC:  PF 1.86, WR 50%  (6 trades)
  XRP:  PF 1.58, WR 43%  (7 trades)

SL placed just above the upthrust wick high (natural invalidation).
TP = entry - |SL - entry| × 2.5
"""
import logging
import os
import yaml

from core.cooldown_store import CooldownStore
from core.filter import atr_spike_ok
from core.vol_ratio import compute_vol_ratio
from core.weekly_trend_gate import weekly_allows_short

log = logging.getLogger(__name__)

_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "config.yaml")
with open(_CONFIG_PATH) as _f:
    _cfg = yaml.safe_load(_f)

# Shares wyckoff_spring config block — same structural pattern, opposite direction
_UT_CFG         = _cfg.get("wyckoff_upthrust", _cfg.get("wyckoff_spring", {}))
_COOLDOWN_SECS  = float(_UT_CFG.get("cooldown_mins", 60)) * 60.0
_RR_RATIO       = float(_UT_CFG.get("rr_ratio", 2.5))
_SL_BUFFER      = float(_UT_CFG.get("sl_buffer_pct", 0.001))
_THRESHOLD      = float(_UT_CFG.get("fire_threshold", 0.67))
_MC_VOL_MAX     = float(_UT_CFG.get("mc_vol_ratio_max", 0.0))

_cd = CooldownStore("WYCKOFF_UPTHRUST")


def is_on_cooldown(symbol: str) -> bool:
    return _cd.is_active(symbol)


def set_cooldown(symbol: str) -> None:
    _cd.set(symbol, _COOLDOWN_SECS)


async def score(symbol: str, cache) -> list[dict]:
    """Score symbol for Wyckoff upthrust SHORT setups.

    Returns list of score dicts (SHORT only — upthrusts are bearish).
    SL = upthrust_bar.high × (1 + sl_buffer_pct)  ← just above wick
    TP = entry - |SL - entry| × rr_ratio
    """
    from signals.range.upthrust import check_wyckoff_upthrust

    results = []
    cool_ok = not is_on_cooldown(symbol)

    # ATR spike gate — no entries during flash crashes
    if not atr_spike_ok(symbol, cache, tf="1h"):
        log.debug("Wyckoff upthrust blocked — ATR spike on %s", symbol)
        return []

    # Vol ratio gate
    if _MC_VOL_MAX > 0:
        bars_vr = cache.get_ohlcv(symbol, window=60, tf="1h")
        if bars_vr and len(bars_vr) >= 54:
            vr = compute_vol_ratio(bars_vr)
            if vr > _MC_VOL_MAX:
                log.debug("Wyckoff upthrust blocked — vol ratio %.2f > %.1f on %s",
                          vr, _MC_VOL_MAX, symbol)
                return []

    # Weekly trend gate — SHORT only in macro bear
    if not weekly_allows_short("wyckoff_upthrust", cache):
        log.debug("Wyckoff upthrust blocked — weekly gate on %s", symbol)
        return []

    # Check upthrust signal
    upthrust_ok = check_wyckoff_upthrust(symbol, cache)
    if not upthrust_ok:
        return []

    # HTF confirmation — 4H must be bearish
    bars_4h = cache.get_ohlcv(symbol, window=25, tf="4h")
    if not bars_4h or len(bars_4h) < 22:
        return []

    closes_4h = [b["c"] for b in bars_4h]
    k = 2.0 / (21 + 1)
    ema21 = sum(closes_4h[:21]) / 21
    for c in closes_4h[21:]:
        ema21 = c * k + ema21 * (1 - k)
    htf_bearish = closes_4h[-1] < ema21

    if not htf_bearish:
        log.debug("Wyckoff upthrust blocked — 4H not bearish on %s", symbol)
        return []

    # RSI confirmation — not oversold (we want to short into strength)
    bars_1h = cache.get_ohlcv(symbol, window=20, tf="1h")
    if not bars_1h or len(bars_1h) < 15:
        return []

    closes_1h = [b["c"] for b in bars_1h]
    gains = losses = 0.0
    for i in range(1, len(closes_1h)):
        d = closes_1h[i] - closes_1h[i-1]
        if d > 0:
            gains += d
        else:
            losses -= d
    rsi_val = 100 - 100/(1 + gains/losses) if losses > 0 else 100

    if rsi_val < 40:
        log.debug("Wyckoff upthrust blocked — RSI %.1f oversold on %s", rsi_val, symbol)
        return []

    # Compute SL/TP using wick-based SL
    bars_15m = cache.get_ohlcv(symbol, window=5, tf="15m")
    if not bars_15m:
        return []

    upthrust_bar = bars_15m[-2]   # upthrust signal fires on bar[-2], bar[-1] confirms
    entry = bars_15m[-1]["c"]
    if entry == 0:
        return []

    # SL just above upthrust wick high
    sl_dist = max(
        upthrust_bar["h"] * (1.0 + _SL_BUFFER) - entry,
        entry * 0.002   # minimum 0.2% SL
    )
    stop = entry + sl_dist
    tp   = entry - sl_dist * _RR_RATIO

    if stop <= 0 or tp <= 0:
        return []

    # Score: upthrust(0.40) + htf(0.35) + rsi(0.25)
    score_val = 0.40 + 0.35 + (0.25 if rsi_val > 50 else 0.10)

    signals = {
        "wyckoff_upthrust": True,
        "htf_bearish":      htf_bearish,
        "rsi_ok":           rsi_val > 40,
        "atr_spike_ok":     True,
        "weekly_gate_ok":   True,
    }

    fire = score_val >= _THRESHOLD and cool_ok and stop > 0 and tp > 0

    if fire:
        set_cooldown(symbol)

    results.append({
        "symbol":    symbol,
        "regime":    "WYCKOFF_UPTHRUST",
        "direction": "SHORT",
        "score":     round(score_val, 4),
        "signals":   signals,
        "fire":      fire,
        "wu_stop":   round(stop, 8),
        "wu_tp":     round(tp, 8),
    })

    return results
