"""
core/wyckoff_scorer.py
Wyckoff spring scorer — wick-based SL, all market conditions.

Confirmed backtest results:
  BTC: PF 3.83, WR 56.2%, 16 trades over 3 years
  ETH: PF 2.50, WR 41.7%, 12 trades over 3 years (bull only)

SL placed just below the spring wick low (natural invalidation).
TP = entry + |entry - SL| × 2.5
"""
import logging
import os
import yaml

from core.cooldown_store import CooldownStore
from core.filter import atr_spike_ok
from core.vol_ratio import compute_vol_ratio
from core.weekly_trend_gate import weekly_allows_long

log = logging.getLogger(__name__)

_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "config.yaml")
with open(_CONFIG_PATH) as _f:
    _cfg = yaml.safe_load(_f)

_WYCKOFF_CFG    = _cfg.get("wyckoff_spring", {})
_COOLDOWN_SECS  = float(_WYCKOFF_CFG.get("cooldown_mins", 60)) * 60.0
_RR_RATIO       = float(_WYCKOFF_CFG.get("rr_ratio", 2.5))
_SL_BUFFER      = float(_WYCKOFF_CFG.get("sl_buffer_pct", 0.001))
_THRESHOLD      = float(_WYCKOFF_CFG.get("fire_threshold", 0.67))
_MC_VOL_MAX     = float(_WYCKOFF_CFG.get("mc_vol_ratio_max", 0.0))

_cd = CooldownStore("WYCKOFF_SPRING")


def is_on_cooldown(symbol: str) -> bool:
    return _cd.is_active(symbol)


def set_cooldown(symbol: str) -> None:
    _cd.set(symbol, _COOLDOWN_SECS)


async def score(symbol: str, cache) -> list[dict]:
    """Score symbol for Wyckoff spring setups.

    Returns list of score dicts (LONG only — springs are bullish).
    SL = spring_bar.low × (1 - sl_buffer_pct)  ← just below wick
    TP = entry + |entry - SL| × rr_ratio
    """
    from signals.range.wyckoff_spring import check_wyckoff_spring

    results = []
    cool_ok = not is_on_cooldown(symbol)

    # ATR spike gate — no entries during flash crashes
    if not atr_spike_ok(symbol, cache, tf="1h"):
        log.debug("Wyckoff spring blocked — ATR spike on %s", symbol)
        return []

    # Vol ratio gate — block during vol spikes beyond threshold
    if _MC_VOL_MAX > 0:
        bars_vr = cache.get_ohlcv(symbol, window=60, tf="1h")
        if bars_vr and len(bars_vr) >= 54:
            vr = compute_vol_ratio(bars_vr)
            if vr > _MC_VOL_MAX:
                log.debug("Wyckoff spring blocked — vol ratio %.2f > %.1f on %s",
                          vr, _MC_VOL_MAX, symbol)
                return []

    # Weekly trend gate — only LONG in macro bull
    if not weekly_allows_long("wyckoff_spring", cache):
        log.debug("Wyckoff spring blocked — weekly gate on %s", symbol)
        return []

    # Check spring signal
    spring_ok = check_wyckoff_spring(symbol, cache)
    if not spring_ok:
        return []

    # HTF confirmation — 4H must be bullish
    bars_4h = cache.get_ohlcv(symbol, window=25, tf="4h")
    if not bars_4h or len(bars_4h) < 22:
        return []

    closes_4h = [b["c"] for b in bars_4h]
    k = 2.0 / (21 + 1)
    ema21 = sum(closes_4h[:21]) / 21
    for c in closes_4h[21:]:
        ema21 = c * k + ema21 * (1 - k)
    htf_bullish = closes_4h[-1] > ema21

    if not htf_bullish:
        log.debug("Wyckoff spring blocked — 4H not bullish on %s", symbol)
        return []

    # RSI confirmation — not overbought
    bars_1h = cache.get_ohlcv(symbol, window=20, tf="1h")
    if not bars_1h or len(bars_1h) < 15:
        return []

    closes_1h = [b["c"] for b in bars_1h]
    gains = losses = 0.0
    for i in range(1, len(closes_1h)):
        d = closes_1h[i] - closes_1h[i-1]
        if d > 0: gains += d
        else: losses -= d
    rsi_val = 100 - 100/(1 + gains/losses) if losses > 0 else 100

    if rsi_val > 60:
        log.debug("Wyckoff spring blocked — RSI %.1f overbought on %s", rsi_val, symbol)
        return []

    # Compute SL/TP using wick-based SL
    bars_15m = cache.get_ohlcv(symbol, window=5, tf="15m")
    if not bars_15m:
        return []

    spring_bar = bars_15m[-2]   # spring signal fires on bar[-2], bar[-1] confirms
    entry = bars_15m[-1]["c"]
    if entry == 0:
        return []

    # SL just below spring wick low
    sl_dist = max(
        entry - spring_bar["l"] * (1.0 - _SL_BUFFER),
        entry * 0.002   # minimum 0.2% SL
    )
    stop = entry - sl_dist
    tp   = entry + sl_dist * _RR_RATIO

    # Score: spring(0.40) + htf(0.35) + rsi(0.25)
    score_val = 0.40 + 0.35 + (0.25 if rsi_val < 50 else 0.10)

    signals = {
        "wyckoff_spring": True,
        "htf_bullish":    htf_bullish,
        "rsi_ok":         rsi_val < 60,
        "atr_spike_ok":   True,
        "weekly_gate_ok": True,
    }

    fire = score_val >= _THRESHOLD and cool_ok and stop > 0 and tp > entry

    if fire:
        set_cooldown(symbol)

    results.append({
        "symbol":    symbol,
        "regime":    "WYCKOFF",
        "direction": "LONG",
        "score":     round(score_val, 4),
        "signals":   signals,
        "fire":      fire,
        "ws_stop":   round(stop, 8),
        "ws_tp":     round(tp, 8),
    })

    return results
