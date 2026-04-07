"""CME Gap scorer — BTC only, PF 2.74, 135 trades/3yr.

Every week Binance CME futures close Friday ~21:00 UTC and reopen Sunday ~23:00 UTC.
The price gap between Friday close and Sunday open fills ~80% of the time within 72h.
Entry direction = toward the gap fill:
  Sunday open > Friday close → SHORT (fill down toward Friday close)
  Sunday open < Friday close → LONG  (fill up toward Friday close)
"""
import logging
import os
import yaml

from core.cooldown_store import CooldownStore
from core.filter import atr_spike_ok

log = logging.getLogger(__name__)

_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "config.yaml")
with open(_CONFIG_PATH) as _f:
    _cfg = yaml.safe_load(_f)

_CME_CFG       = _cfg.get("cme_gap", {})
_MIN_GAP_PCT   = float(_CME_CFG.get("min_gap_pct", 0.003))
_COOLDOWN_SECS = float(_CME_CFG.get("cooldown_hours", 72)) * 3600.0
_SL_PCT        = float(_CME_CFG.get("sl_pct", 0.010))
_THRESHOLD     = 0.66   # need ≥ 2 of 3 signals (gap_exists is hard gate anyway)

_cd = CooldownStore("CME_GAP")


def _detect_gap(cache) -> tuple[float, float, float] | None:
    """Return (friday_close, sunday_open, gap_pct) or None if no gap detected.

    Uses 1W close for Friday's closing price and 1D open for Sunday's opening price.
    """
    # Previous week close = Friday close
    weekly = cache.get_ohlcv("BTCUSDT", window=3, tf="1w")
    if not weekly or len(weekly) < 2:
        return None
    friday_close = weekly[-2]["c"]   # prev week close
    if friday_close <= 0:
        return None

    # Current day open on Sunday/Monday = gap open
    daily = cache.get_ohlcv("BTCUSDT", window=3, tf="1d")
    if not daily or len(daily) < 2:
        return None
    sunday_open = daily[-1]["o"]   # most recent daily open
    if sunday_open <= 0:
        return None

    gap_pct = (sunday_open - friday_close) / friday_close
    return friday_close, sunday_open, gap_pct


def _htf_no_oppose(direction: str, cache) -> bool:
    """Return True unless 4H EMA21 strongly opposes the fill direction."""
    bars_4h = cache.get_ohlcv("BTCUSDT", window=25, tf="4h")
    if not bars_4h or len(bars_4h) < 22:
        return True   # insufficient data — don't block
    closes = [b["c"] for b in bars_4h]
    k = 2.0 / (21 + 1)
    ema = sum(closes[:21]) / 21
    for c in closes[21:]:
        ema = c * k + ema * (1.0 - k)
    price = closes[-1]
    # Only block if EMA strongly opposes: LONG blocked if price well below EMA,
    # SHORT blocked if price well above EMA
    if direction == "LONG" and price < ema * 0.98:
        return False
    if direction == "SHORT" and price > ema * 1.02:
        return False
    return True


async def score(symbol: str, cache) -> list[dict]:
    """Score BTC for CME gap fill setup.

    Returns a single-element list with the gap trade, or empty list.
    """
    if symbol != "BTCUSDT":
        return []

    # Cooldown check — one trade per weekend gap (72h)
    if _cd.is_active(symbol):
        return []

    gap_result = _detect_gap(cache)
    if gap_result is None:
        return []

    friday_close, sunday_open, gap_pct = gap_result

    # Determine direction
    if abs(gap_pct) < _MIN_GAP_PCT:
        return []   # gap too small

    gap_exists = True

    if gap_pct > 0:
        direction = "SHORT"   # gapped UP → fill DOWN toward Friday close
    else:
        direction = "LONG"    # gapped DOWN → fill UP toward Friday close

    # Signals
    htf_ok    = _htf_no_oppose(direction, cache)
    crisis_ok = atr_spike_ok(symbol, cache, tf="1h")

    score_val = (
        (0.34 if gap_exists else 0.0)
        + (0.33 if htf_ok else 0.0)
        + (0.33 if crisis_ok else 0.0)
    )

    # SL/TP
    entry = sunday_open
    tp    = friday_close   # gap fill target

    if direction == "LONG":
        stop = entry * (1.0 - _SL_PCT)
    else:
        stop = entry * (1.0 + _SL_PCT)

    fire = (gap_exists and htf_ok and crisis_ok
            and score_val >= _THRESHOLD
            and stop > 0 and entry > 0)

    if fire:
        _cd.set(symbol, _COOLDOWN_SECS)

    return [{
        "symbol":    symbol,
        "regime":    "cme_gap",
        "direction": direction,
        "score":     round(score_val, 4),
        "signals":   {
            "gap_exists":    gap_exists,
            "htf_no_oppose": htf_ok,
            "not_in_crisis": crisis_ok,
            "gap_pct":       round(gap_pct, 6),
            "friday_close":  round(friday_close, 2),
            "sunday_open":   round(sunday_open, 2),
        },
        "fire":    fire,
        "cg_stop": round(stop, 2),
        "cg_tp":   round(tp, 2),
    }]
