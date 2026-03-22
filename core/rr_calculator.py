"""Risk/Reward calculator — computes entry, stop loss, take profit, and position size."""
import os
import yaml

_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "config.yaml")
with open(_CONFIG_PATH) as _f:
    _cfg = yaml.safe_load(_f)

_RR_RATIO          = _cfg["risk"]["rr_ratio"]            # 2.5
_RISK_PER_TRADE    = _cfg["risk"]["risk_per_trade"]       # 0.01 (1%)
_MAX_SIZE_USDT     = _cfg["risk"]["max_position_size_usdt"]  # 5000
_STOP_ATR_MULT     = 1.5   # stop = entry ± ATR × multiplier


def _atr(candles: list[dict], period: int = 14) -> float:
    """Return Average True Range over the last `period` candles."""
    if len(candles) < period + 1:
        return 0.0
    trs = []
    for i in range(1, len(candles)):
        prev_c = candles[i - 1]["c"]
        c = candles[i]
        tr = max(c["h"] - c["l"], abs(c["h"] - prev_c), abs(c["l"] - prev_c))
        trs.append(tr)
    return sum(trs[-period:]) / period


def compute(symbol: str, direction: str, cache) -> tuple[float, float, float]:
    """Return (entry_price, stop_loss, take_profit).

    Entry  : current 1-minute close (market order proxy).
    Stop   : entry ± ATR(14) × 1.5  (below for LONG, above for SHORT).
    TP     : entry ± stop_distance × RR_RATIO.
    """
    closes_1m = cache.get_closes(symbol, window=1, tf="1m")
    if not closes_1m:
        return 0.0, 0.0, 0.0

    entry = closes_1m[-1]

    candles_15m = cache.get_ohlcv(symbol, window=20, tf="15m")
    atr_val = _atr(candles_15m)
    if atr_val == 0.0:
        return entry, 0.0, 0.0

    stop_dist = atr_val * _STOP_ATR_MULT

    if direction == "LONG":
        stop        = entry - stop_dist
        take_profit = entry + stop_dist * _RR_RATIO
    else:  # SHORT
        stop        = entry + stop_dist
        take_profit = entry - stop_dist * _RR_RATIO

    return entry, round(stop, 8), round(take_profit, 8)


def position_size(entry: float, stop: float, cache) -> float:
    """Return position size in base currency units.

    Formula: size = (balance × risk_pct) / |entry − stop|
    Capped at max_position_size_usdt / entry.
    """
    if entry == 0.0 or stop == 0.0 or entry == stop:
        return 0.0

    balance = cache.get_account_balance()
    if balance <= 0.0:
        return 0.0

    risk_usdt = balance * _RISK_PER_TRADE
    size = risk_usdt / abs(entry - stop)

    max_size = _MAX_SIZE_USDT / entry
    return round(min(size, max_size), 6)
