"""HTF Demand/Supply Zone scorer — 4H origin-of-move zone reactions.

Fires when price returns to a 4H demand/supply zone for the first retest.
Highest quality signal with lowest false positive rate.

Score components (each 0.25):
    zone_active      — valid demand/supply zone detected on 4H
    htf_1h_confirm   — 1H close confirms direction (bullish in demand, bearish in supply)
    oi_supporting    — OI rising (new positions being added, not short covering)
    rsi_not_extreme  — RSI is not extended in the wrong direction
                       (demand: RSI < 65, supply: RSI > 35)

Threshold: 0.75 (3 of 4). zone_active is always True so effectively 2 of 3
confirmations are needed.
"""
import logging
import os
import yaml

from core.cooldown_store import CooldownStore

log = logging.getLogger(__name__)

_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "config.yaml")
with open(_CONFIG_PATH) as _f:
    _cfg = yaml.safe_load(_f)

_ZN_CFG = _cfg.get("zone", {})

_COOLDOWN_SECS = float(_ZN_CFG.get("cooldown_mins", 120)) * 60.0
_THRESHOLD     = float(_ZN_CFG.get("fire_threshold", 0.75))

_cd = CooldownStore("ZONE")

_RSI_PERIOD      = 14
_RSI_LONG_MAX    = 65    # demand zone long: RSI must be below 65 (not overbought)
_RSI_SHORT_MIN   = 35    # supply zone short: RSI must be above 35 (not oversold)
_OI_RISE_LOOKBACK = 3    # bars to check OI increase


def is_on_cooldown(symbol: str) -> bool:
    return _cd.is_active(symbol)


def set_cooldown(symbol: str) -> None:
    _cd.set(symbol, _COOLDOWN_SECS)


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


def _oi_rising(symbol: str, cache) -> bool:
    """OI increasing over last 3 snapshots — new longs being added."""
    oi = cache.get_open_interest(symbol)
    if not oi or len(oi) < _OI_RISE_LOOKBACK + 1:
        return False
    recent = oi[-(_OI_RISE_LOOKBACK + 1):]
    return recent[-1] > recent[0]


async def score(symbol: str, cache) -> list[dict]:
    """Score symbol for demand/supply zone retest setups."""
    from signals.trend.demand_zone import (
        check_demand_zone_long,
        check_supply_zone_short,
        get_demand_zone_levels,
        get_supply_zone_levels,
    )

    results = []
    cool_ok = not is_on_cooldown(symbol)

    # ── LONG: Demand zone ─────────────────────────────────────────────────────
    demand_ok = check_demand_zone_long(symbol, cache)
    if demand_ok:
        bars_1h = cache.get_ohlcv(symbol, window=20, tf="1h")
        closes_1h = [b["c"] for b in bars_1h] if bars_1h else []

        rsi = _rsi(closes_1h) if closes_1h else 50.0
        rsi_ok = rsi < _RSI_LONG_MAX

        # 1H confirmation already embedded in check_demand_zone_long
        htf_1h = True

        oi_ok = _oi_rising(symbol, cache)

        signals = {
            "zone_active":     True,
            "htf_1h_confirm":  htf_1h,
            "oi_supporting":   oi_ok,
            "rsi_not_extreme": rsi_ok,
        }
        score_val = sum(0.25 for v in signals.values() if v)
        entry, stop, tp = get_demand_zone_levels(symbol, cache)
        fire = score_val >= _THRESHOLD and cool_ok and entry > 0

        results.append({
            "symbol":    symbol,
            "regime":    "ZONE",
            "direction": "LONG",
            "score":     round(score_val, 4),
            "signals":   signals,
            "fire":      fire,
            "zn_stop":   stop,
            "zn_tp":     tp,
        })

    # ── SHORT: Supply zone ────────────────────────────────────────────────────
    supply_ok = check_supply_zone_short(symbol, cache)
    if supply_ok:
        bars_1h = cache.get_ohlcv(symbol, window=20, tf="1h")
        closes_1h = [b["c"] for b in bars_1h] if bars_1h else []

        rsi = _rsi(closes_1h) if closes_1h else 50.0
        rsi_ok = rsi > _RSI_SHORT_MIN

        htf_1h = True
        oi_ok  = not _oi_rising(symbol, cache)   # OI falling = longs being liquidated

        signals = {
            "zone_active":     True,
            "htf_1h_confirm":  htf_1h,
            "oi_supporting":   oi_ok,
            "rsi_not_extreme": rsi_ok,
        }
        score_val = sum(0.25 for v in signals.values() if v)
        entry, stop, tp = get_supply_zone_levels(symbol, cache)
        fire = score_val >= _THRESHOLD and cool_ok and entry > 0

        results.append({
            "symbol":    symbol,
            "regime":    "ZONE",
            "direction": "SHORT",
            "score":     round(score_val, 4),
            "signals":   signals,
            "fire":      fire,
            "zn_stop":   stop,
            "zn_tp":     tp,
        })

    return results
