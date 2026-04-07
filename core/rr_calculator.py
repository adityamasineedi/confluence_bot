"""Risk/Reward calculator — computes entry, stop loss, take profit, and position size."""
import logging
import os
import time as _time
import yaml

log = logging.getLogger(__name__)

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
_PAPER_DEFAULT_BAL = 5_000.0  # default paper balance when no API key is set

# Binance Futures lot-size step sizes (decimal places for quantity)
# stepSize=0.001 → 3 dp, stepSize=1 → 0 dp, etc.
_STEP_DECIMALS: dict[str, int] = {
    "BTCUSDT":  3,
    "ETHUSDT":  3,
    "SOLUSDT":  1,
    "BNBUSDT":  2,
    "XRPUSDT":  0,
    "LINKUSDT": 1,
    "DOGEUSDT": 0,
    "SUIUSDT":  0,
}

assert _STEP_DECIMALS == {   # matches executor._QTY_PRECISION exactly
    "BTCUSDT": 3, "ETHUSDT": 3, "SOLUSDT": 1, "BNBUSDT": 2,
    "XRPUSDT": 0, "LINKUSDT": 1, "DOGEUSDT": 0, "SUIUSDT": 0,
}, "STEP_DECIMALS mismatch — update executor._QTY_PRECISION to match"


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


_committed_cache_value: float = 0.0
_committed_cache_ts:    float = 0.0
_COMMITTED_TTL = 10.0   # re-query at most every 10 seconds


def _committed_risk() -> float:
    """Return total risk currently committed to open trades.

    For each open trade: risk = abs(entry - stop_loss) × qty.
    This is the max additional loss if ALL open trades hit SL simultaneously.
    Results are cached for _COMMITTED_TTL seconds to avoid a DB connection per symbol.
    """
    global _committed_cache_value, _committed_cache_ts

    now = _time.monotonic()
    if now - _committed_cache_ts < _COMMITTED_TTL:
        return _committed_cache_value    # fast path — no DB hit

    try:
        import sqlite3
        db_path = os.environ.get("DB_PATH", "confluence_bot.db")
        with sqlite3.connect(db_path, timeout=2.0) as conn:
            conn.execute("PRAGMA journal_mode=WAL")   # allow concurrent readers
            rows = conn.execute(
                "SELECT entry, stop_loss, size FROM trades WHERE status='OPEN'"
            ).fetchall()
        total = sum(
            abs(float(e) - float(sl)) * float(qty)
            for e, sl, qty in rows
            if float(e) > 0 and float(sl) > 0
        )
        _committed_cache_value = total
        _committed_cache_ts    = now
        return total
    except Exception as exc:
        log.debug("_committed_risk query failed: %s — returning last known value", exc)
        return _committed_cache_value   # stale but safe


def invalidate_committed_cache() -> None:
    """Call after a trade opens or closes to force immediate re-query."""
    global _committed_cache_ts
    _committed_cache_ts = 0.0


def position_size(
    entry: float,
    stop: float,
    cache,
    symbol: str = "",
    risk_pct: float | None = None,
    slip_pct: float = 0.0002,
) -> float:
    """Return position size in base currency units.

    Formula: size = (balance × risk_pct) / (|entry − stop| × round_trip_adj)
    where round_trip_adj = 1 + (slip_pct + fee_pct) × 2 inflates the effective
    stop distance to account for slippage and taker fees on both legs.

    Capped at max_position_size_usdt / entry.
    Rounded to the symbol's Binance lot-size step precision.

    Args:
        risk_pct: override the config risk fraction (e.g. for drawdown scaling).
                  None = use the global _RISK_PER_TRADE from config.
        slip_pct: one-way slippage estimate (dynamic, regime-aware).
                  Defaults to 0.02% (calm market baseline).
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

    # Subtract risk already committed to open trades — prevents over-leveraging
    committed = _committed_risk()
    available_equity = balance - committed
    if available_equity <= 0:
        log.debug("No available equity for %s (balance=%.2f committed=%.2f)", symbol, balance, committed)
        return 0.0

    effective_risk = risk_pct if risk_pct is not None else _RISK_PER_TRADE
    risk_usdt = min(available_equity * effective_risk, _MAX_RISK_USDT)
    # Inflate stop distance by round-trip slippage + fees so risk_usdt is the
    # true net loss including market impact and commissions (both legs).
    _fee_pct        = 0.0005   # taker fee per side (0.05%)
    round_trip_adj  = 1.0 + (slip_pct + _fee_pct) * 2
    effective_stop  = abs(entry - stop) * round_trip_adj
    size = risk_usdt / effective_stop

    max_size = _MAX_SIZE_USDT / entry
    size = min(size, max_size)

    decimals = _STEP_DECIMALS.get(symbol.upper(), 3)
    rounded = round(size, decimals)
    return int(rounded) if decimals == 0 else rounded
