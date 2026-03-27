"""Risk/Reward calculator — computes entry, stop loss, take profit, and position size."""
import os
import yaml

_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "config.yaml")
with open(_CONFIG_PATH) as _f:
    _cfg = yaml.safe_load(_f)

_RR_RATIO          = _cfg["risk"]["rr_ratio"]            # 2.5
_RISK_PER_TRADE    = _cfg["risk"].get("risk_per_trade", 0.01)  # fallback 1% (max_risk_usdt takes priority)
_MAX_RISK_USDT     = float(_cfg["risk"].get("max_risk_usdt", 100))  # $100 hard cap
_MAX_SIZE_USDT     = _cfg["risk"]["max_position_size_usdt"]  # 5000
_STOP_ATR_MULT     = 1.5   # stop = entry ± ATR × multiplier
# Sub-$5 coins (ADA, DOGE, DOT) have tiny ATR in absolute terms — a 0.3-point
# stop on a $1 coin gets taken out by normal spread noise.  Enforce a minimum
# stop distance of 0.5 % of entry price so we always stay outside the noise band.
_MIN_STOP_PCT      = 0.005  # 0.5 % minimum stop distance
_PAPER_DEFAULT_BAL = 10_000.0  # default paper balance when no API key is set
# Binance Futures taker fee: 0.05% per side = 0.10% round-trip.
# Inflating the effective stop distance by the round-trip fee ensures the
# dollar risk calculation already accounts for both entry and exit commissions.
_TAKER_FEE_RT      = 0.0010   # 0.10% round-trip (entry + exit)

# Binance Futures lot-size step sizes (decimal places for quantity)
# stepSize=0.001 → 3 dp, stepSize=1 → 0 dp, etc.
_STEP_DECIMALS: dict[str, int] = {
    "BTCUSDT":  3,
    "ETHUSDT":  3,
    "SOLUSDT":  2,
    "BNBUSDT":  2,
    "AVAXUSDT": 0,
    "ADAUSDT":  0,
    "DOTUSDT":  1,
    "DOGEUSDT": 0,
    "SUIUSDT":  1,
}


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

    min_stop  = entry * _MIN_STOP_PCT
    stop_dist = max(atr_val * _STOP_ATR_MULT, min_stop)

    if direction == "LONG":
        stop        = entry - stop_dist
        take_profit = entry + stop_dist * _RR_RATIO
    else:  # SHORT
        stop        = entry + stop_dist
        take_profit = entry - stop_dist * _RR_RATIO

    return entry, round(stop, 8), round(take_profit, 8)


def position_size(entry: float, stop: float, cache, symbol: str = "") -> float:
    """Return position size in base currency units.

    Formula: size = (balance × risk_pct) / |entry − stop|
    Capped at max_position_size_usdt / entry.
    Rounded to the symbol's Binance lot-size step precision.
    """
    if entry == 0.0 or stop == 0.0 or entry == stop:
        return 0.0

    balance = cache.get_account_balance()
    if balance <= 0.0:
        paper_mode = os.environ.get("PAPER_MODE", "0") == "1"
        if paper_mode:
            balance = _PAPER_DEFAULT_BAL
        else:
            return 0.0

    risk_usdt = min(balance * _RISK_PER_TRADE, _MAX_RISK_USDT)
    # Inflate stop distance by round-trip fees so risk_usdt is the true net loss
    # including commissions (entry + exit taker fees).
    fee_per_unit     = entry * _TAKER_FEE_RT
    effective_stop   = abs(entry - stop) + fee_per_unit
    size = risk_usdt / effective_stop

    max_size = _MAX_SIZE_USDT / entry
    size = min(size, max_size)

    decimals = _STEP_DECIMALS.get(symbol.upper(), 3)
    rounded = round(size, decimals)
    return int(rounded) if decimals == 0 else rounded
