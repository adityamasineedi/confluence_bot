"""In-memory cache — single source of truth for all market data."""
import time
import threading
from collections import defaultdict, deque

# ── Per-timeframe candle retention limits ─────────────────────────────────────
_TF_MAXLEN: dict[str, int] = {
    "1m":  500,
    "5m":  500,    # breakout_retest needs 200+ bars
    "15m": 500,
    "1h":  500,
    "4h":  300,
    "1d":  200,
    "1w":  100,
}
_DEFAULT_TF_MAXLEN = 200

# Fixed-size rolling windows for non-OHLCV series
_OI_MAXLEN        = 500    # hourly snapshots → ~20 days
_BASIS_MAXLEN     = 500
_SKEW_MAXLEN      = 500
_INFLOW_MAXLEN    = 365    # daily readings → 1 year
_AGG_TRADE_MAXLEN = 10_000
_LIQ_EVENT_MAXLEN = 500    # forced-liquidation events (~last hour at busy markets)


_global_cache: "DataCache | None" = None


class DataCache:
    """Thread-safe in-memory store for all market data.

    Writers are synchronous and acquire a threading.Lock.
    Readers are lock-free: they snapshot the relevant deque into a plain list
    before processing, which is safe under CPython's GIL.

    Internal layout:
        _ohlcv[(symbol, tf)]      deque[dict]   {o,h,l,c,v,ts}  per-tf maxlen
        _cvd[(symbol, tf)]        deque[float]  per-candle net delta, same maxlen
        _oi[symbol]               deque[dict]   {ts:int, oi:float}
        _funding[symbol]          float         latest 8-h rate
        _liq_clusters[symbol]     list[dict]    {price, size_usd, side}
        _range_high[symbol]       float
        _range_low[symbol]        float
        _range_start_ts[symbol]   int           Unix ms
        _basis[symbol]            deque[float]
        _skew[symbol]             deque[float]
        _agg_trades[symbol]       deque[dict]   {ts, price, qty, is_buyer_maker}
        _inflow[symbol]           deque[dict]   {ts, value}
        _order_book[symbol]       dict          {bids: [(price,qty)], asks: [(price,qty)], ts: float}
        _liq_events[symbol]       deque[dict]   {side, qty, price, ts_ms}
        _long_short_ratio[symbol] float         latest global L/S account ratio (Coinglass)
        _account_balance          float
    """

    def __init__(self) -> None:
        global _global_cache
        _global_cache = self

        self._lock = threading.Lock()

        # OHLCV and CVD: keyed by (symbol, tf); deque maxlen set at creation
        self._ohlcv: dict[tuple[str, str], deque] = {}
        self._cvd:   dict[tuple[str, str], deque] = {}

        # Scalar lookups
        self._funding:          dict[str, float] = {}
        self._liq_clusters:     dict[str, list]  = {}
        self._range_high:       dict[str, float] = {}
        self._range_low:        dict[str, float] = {}
        self._range_start_ts:   dict[str, int]   = {}
        self._long_short_ratio: dict[str, float] = {}
        self._account_balance:  float = 0.0

        # BTC dominance — single global value + rolling history (last 24 readings)
        self._btc_dominance:         float       = 0.0
        self._btc_dominance_history: list[float] = []

        # Order book: latest L2 snapshot per symbol
        self._order_book: dict[str, dict] = {}

        # Liquidation events: rolling window per symbol
        self._liq_events: defaultdict[str, deque] = defaultdict(
            lambda: deque(maxlen=_LIQ_EVENT_MAXLEN))

        # Rolling deques via defaultdict (created on first write)
        # OI keyed by (symbol, exchange) — prevents mixing incomparable units
        self._oi:         defaultdict[tuple, deque] = defaultdict(
            lambda: deque(maxlen=_OI_MAXLEN))
        self._basis:      defaultdict[str, deque] = defaultdict(
            lambda: deque(maxlen=_BASIS_MAXLEN))
        self._skew:       defaultdict[str, deque] = defaultdict(
            lambda: deque(maxlen=_SKEW_MAXLEN))
        self._agg_trades: defaultdict[str, deque] = defaultdict(
            lambda: deque(maxlen=_AGG_TRADE_MAXLEN))
        self._inflow:     defaultdict[str, deque] = defaultdict(
            lambda: deque(maxlen=_INFLOW_MAXLEN))

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _ohlcv_buf(self, symbol: str, tf: str) -> deque:
        """Return (creating if needed) the OHLCV deque for (symbol, tf).
        Must be called under self._lock when writing.
        """
        key = (symbol, tf)
        if key not in self._ohlcv:
            self._ohlcv[key] = deque(maxlen=_TF_MAXLEN.get(tf, _DEFAULT_TF_MAXLEN))
        return self._ohlcv[key]

    def _cvd_buf(self, symbol: str, tf: str) -> deque:
        key = (symbol, tf)
        if key not in self._cvd:
            self._cvd[key] = deque(maxlen=_TF_MAXLEN.get(tf, _DEFAULT_TF_MAXLEN))
        return self._cvd[key]

    # ── OHLCV reads ───────────────────────────────────────────────────────────

    def get_closes(self, symbol: str, window: int, tf: str) -> list[float]:
        """Return last `window` closing prices, oldest→newest. [] if no data."""
        buf = self._ohlcv.get((symbol, tf))
        if not buf:
            return []
        snap = list(buf)
        return [c["c"] for c in snap[-window:]]

    def get_ohlcv(self, symbol: str, window: int, tf: str) -> list[dict]:
        """Return last `window` candle dicts, oldest→newest.

        Each dict: {o: float, h: float, l: float, c: float, v: float, ts: int}
        """
        buf = self._ohlcv.get((symbol, tf))
        if not buf:
            return []
        snap = list(buf)
        return snap[-window:]

    def get_ohlcv_since(self, symbol: str, timestamp_ms: int, tf: str) -> list[dict]:
        """Return all candles with ts >= timestamp_ms."""
        buf = self._ohlcv.get((symbol, tf))
        if not buf:
            return []
        snap = list(buf)
        return [c for c in snap if c["ts"] >= timestamp_ms]

    def get_vol_ma(self, symbol: str, window: int, tf: str) -> float:
        """Simple volume MA over last `window` candles. 0.0 if < 2 candles."""
        buf = self._ohlcv.get((symbol, tf))
        if not buf:
            return 0.0
        snap = list(buf)[-window:]
        if len(snap) < 2:
            return 0.0
        return sum(c["v"] for c in snap) / len(snap)

    def get_vol_24h(self, symbol: str) -> float | None:
        """Estimated 24h volume in USDT from the last 24 hourly candles.

        Returns None if fewer than 2 candles are available.
        Each hourly candle volume is multiplied by its close price to convert
        base units to approximate USDT notional.
        """
        buf = self._ohlcv.get((symbol, "1h"))
        if not buf:
            return None
        snap = list(buf)[-24:]
        if len(snap) < 2:
            return None
        return sum(c["v"] * c["c"] for c in snap)

    def get_last_price(self, symbol: str) -> float:
        """Most recent close across timeframes (1m preferred). 0.0 if no data."""
        for tf in ("1m", "5m", "15m", "1h", "4h"):
            buf = self._ohlcv.get((symbol, tf))
            if buf:
                return float(buf[-1]["c"])
        return 0.0

    # ── Key institutional levels ──────────────────────────────────────────────

    def get_key_levels(self, symbol: str) -> dict:
        """Return prior day and prior week high/low for symbol.

        Returns zeros for any unavailable level — callers must handle 0.0 gracefully.
        """
        daily  = self.get_ohlcv(symbol, window=3, tf="1d")
        weekly = self.get_ohlcv(symbol, window=3, tf="1w")

        pdh = daily[-2]["h"]  if len(daily)  >= 2 else 0.0
        pdl = daily[-2]["l"]  if len(daily)  >= 2 else 0.0
        pwh = weekly[-2]["h"] if len(weekly) >= 2 else 0.0
        pwl = weekly[-2]["l"] if len(weekly) >= 2 else 0.0

        return {"pdh": pdh, "pdl": pdl, "pwh": pwh, "pwl": pwl}

    def near_key_level(self, symbol: str, price: float,
                       tolerance_pct: float = 0.003) -> bool:
        """True when price is within tolerance_pct of any PDH/PDL/PWH/PWL level."""
        levels = self.get_key_levels(symbol)
        for level in levels.values():
            if level > 0 and abs(price - level) / level <= tolerance_pct:
                return True
        return False

    # ── CVD reads ─────────────────────────────────────────────────────────────

    def get_cvd(self, symbol: str, window: int, tf: str) -> list[float]:
        """Return last `window` per-candle CVD values (buy_vol − sell_vol), oldest→newest."""
        buf = self._cvd.get((symbol, tf))
        if not buf:
            return []
        snap = list(buf)
        return snap[-window:]

    # ── Open Interest ─────────────────────────────────────────────────────────

    def get_oi(self, symbol: str, offset_hours: int = 0, exchange: str = "binance") -> float | None:
        """Return OI value `offset_hours` ago (0 = latest) for a specific exchange."""
        dq = self._oi.get((symbol, exchange))
        if not dq:
            return None
        snap = list(dq)
        idx = -(1 + offset_hours)
        try:
            return snap[idx]["oi"]
        except IndexError:
            return snap[0]["oi"] if snap else None

    def get_oi_history(self, symbol: str, window: int, exchange: str = "binance") -> list[float]:
        """Return last `window` OI floats for a specific exchange, oldest→newest."""
        dq = self._oi.get((symbol, exchange))
        if not dq:
            return []
        snap = list(dq)
        return [e["oi"] for e in snap[-window:]]

    def get_oi_all_exchanges(self, symbol: str) -> dict[str, float]:
        """Return latest OI per exchange. {} if no data."""
        result = {}
        for exchange in ("binance", "bybit", "okx"):
            dq = self._oi.get((symbol, exchange))
            if dq:
                result[exchange] = list(dq)[-1]["oi"]
        return result

    # ── Funding Rate ──────────────────────────────────────────────────────────

    def get_funding_rate(self, symbol: str) -> float | None:
        """Latest funding rate. None if not yet received."""
        return self._funding.get(symbol)

    # ── Liquidation Clusters ──────────────────────────────────────────────────

    def get_liq_clusters(self, symbol: str) -> list[dict]:
        """Return Coinglass liq cluster list. Each dict: {price, size_usd, side}."""
        return list(self._liq_clusters.get(symbol, []))

    # ── Range Bounds ──────────────────────────────────────────────────────────

    def get_range_high(self, symbol: str) -> float | None:
        return self._range_high.get(symbol)

    def get_range_low(self, symbol: str) -> float | None:
        return self._range_low.get(symbol)

    def get_range_start_timestamp(self, symbol: str) -> int | None:
        return self._range_start_ts.get(symbol)

    def set_range_high(self, symbol: str, val: float) -> None:
        with self._lock:
            self._range_high[symbol] = float(val)

    def set_range_low(self, symbol: str, val: float) -> None:
        with self._lock:
            self._range_low[symbol] = float(val)

    def set_range_start_timestamp(self, symbol: str, ts: int) -> None:
        with self._lock:
            self._range_start_ts[symbol] = int(ts)

    # ── BTC Dominance ─────────────────────────────────────────────────────────

    def push_btc_dominance(self, value: float) -> None:
        """Store a new dominance reading and append to the rolling 24-entry history."""
        with self._lock:
            self._btc_dominance = float(value)
            self._btc_dominance_history.append(float(value))
            if len(self._btc_dominance_history) > 24:
                self._btc_dominance_history.pop(0)

    def get_btc_dominance(self) -> float:
        """Return the latest BTC dominance reading (0.0 if not yet fetched)."""
        return self._btc_dominance

    def get_btc_dominance_trend(self) -> str:
        """Return 'rising', 'falling', or 'flat' based on the last 6 readings.

        Compares the average of the 3 most recent readings to the 3 before those.
        Returns 'flat' when fewer than 3 readings are available.
        """
        h = self._btc_dominance_history
        if len(h) < 3:
            return "flat"
        recent_avg  = sum(h[-3:]) / 3
        earlier_avg = sum(h[-6:-3]) / 3 if len(h) >= 6 else h[0]
        diff = recent_avg - earlier_avg
        if diff > 0.003:
            return "rising"
        if diff < -0.003:
            return "falling"
        return "flat"

    # ── Perp Basis ────────────────────────────────────────────────────────────

    def push_basis(self, symbol: str, val: float) -> None:
        with self._lock:
            self._basis[symbol].append(float(val))

    def get_basis_history(self, symbol: str, n: int) -> list[float]:
        """Return last `n` basis values, oldest→newest."""
        dq = self._basis.get(symbol)
        if not dq:
            return []
        snap = list(dq)
        return snap[-n:]

    # ── Options Skew ──────────────────────────────────────────────────────────

    def push_skew(self, symbol: str, val: float) -> None:
        with self._lock:
            self._skew[symbol].append(float(val))

    def get_skew_history(self, symbol: str, n: int) -> list[float]:
        """Return last `n` skew values, oldest→newest."""
        dq = self._skew.get(symbol)
        if not dq:
            return []
        snap = list(dq)
        return snap[-n:]

    # ── Aggregate Trades ──────────────────────────────────────────────────────

    def push_agg_trade(self, symbol: str, trade: dict) -> None:
        """Append an agg trade. Required keys: ts (Unix ms), price, qty, is_buyer_maker."""
        with self._lock:
            self._agg_trades[symbol].append(trade)

    def get_agg_trades(self, symbol: str, window_seconds: float) -> list[dict]:
        """Return trades from the last `window_seconds` seconds, oldest→newest.

        Reverse-scans from the tail so cost is O(k) in returned trades, not
        O(total buffer size).
        """
        dq = self._agg_trades.get(symbol)
        if not dq:
            return []
        cutoff_ms = (time.time() - window_seconds) * 1000.0
        snap = list(dq)
        result: list[dict] = []
        for trade in reversed(snap):
            if trade["ts"] < cutoff_ms:
                break
            result.append(trade)
        result.reverse()
        return result

    # ── Exchange Inflow ───────────────────────────────────────────────────────

    def get_exchange_inflow(self, symbol: str) -> float | None:
        """Most recent daily exchange inflow (USD). None if unavailable."""
        dq = self._inflow.get(symbol)
        if not dq:
            return None
        return dq[-1]["value"]

    def get_inflow_ma(self, symbol: str, days: int) -> float:
        """Simple MA of inflow over last `days` daily readings. 0.0 if < 2 readings."""
        dq = self._inflow.get(symbol)
        if not dq:
            return 0.0
        snap = list(dq)[-days:]
        if len(snap) < 2:
            return 0.0
        return sum(e["value"] for e in snap) / len(snap)

    # ── Account Balance ───────────────────────────────────────────────────────

    def get_account_balance(self) -> float:
        return self._account_balance

    # ── Write helpers (called by data source modules) ─────────────────────────

    def push_candle(self, symbol: str, tf: str, candle: dict) -> None:
        """Append a closed candle. If last candle shares ts, replace it (live update).

        candle must have keys: o, h, l, c, v, ts
        """
        with self._lock:
            buf = self._ohlcv_buf(symbol, tf)
            if buf and buf[-1]["ts"] == candle["ts"]:
                buf[-1] = candle
            else:
                buf.append(candle)

    def push_cvd_value(self, symbol: str, tf: str, value: float) -> None:
        """Append a per-candle CVD value."""
        with self._lock:
            self._cvd_buf(symbol, tf).append(float(value))

    def push_oi(self, symbol: str, ts: int, oi: float, exchange: str = "binance") -> None:
        """Append an OI snapshot {ts, oi} for a specific exchange."""
        with self._lock:
            self._oi[(symbol, exchange)].append({"ts": ts, "oi": float(oi)})

    def set_funding_rate(self, symbol: str, rate: float) -> None:
        with self._lock:
            self._funding[symbol] = float(rate)

    def set_long_short_ratio(self, symbol: str, ratio: float) -> None:
        """Store latest global long/short account ratio from Coinglass."""
        with self._lock:
            self._long_short_ratio[symbol] = float(ratio)

    def get_long_short_ratio(self, symbol: str) -> float | None:
        """Latest long/short ratio. None if not yet received.

        >1.0 = more longs than shorts globally.
        Contrarian thresholds: >1.8 = crowded long (bearish); <0.6 = crowded short (bullish).
        """
        return self._long_short_ratio.get(symbol)

    def set_liq_clusters(self, symbol: str, clusters: list[dict]) -> None:
        """Atomically replace the full liquidation cluster list."""
        with self._lock:
            self._liq_clusters[symbol] = list(clusters)

    def push_exchange_inflow(self, symbol: str, ts: int, value: float) -> None:
        with self._lock:
            self._inflow[symbol].append({"ts": ts, "value": float(value)})

    def set_account_balance(self, balance: float) -> None:
        with self._lock:
            self._account_balance = float(balance)

    # ── Order Book (L2 depth snapshot) ───────────────────────────────────────

    def push_order_book(self, symbol: str, bids: list, asks: list) -> None:
        """Store latest L2 snapshot. bids/asks are [(price, qty), ...] best-first."""
        with self._lock:
            self._order_book[symbol] = {
                "bids": list(bids),
                "asks": list(asks),
                "ts":   time.time(),
            }

    def get_order_book(self, symbol: str) -> dict:
        """Return latest order book dict or {} if no snapshot yet."""
        return dict(self._order_book.get(symbol, {}))

    # ── Forced Liquidation Events ─────────────────────────────────────────────

    def push_liquidation(self, symbol: str, side: str, qty: float, price: float, ts_ms: int) -> None:
        """Append a forced liquidation event.

        side: order side of the liquidation engine ('BUY' = short was liquidated,
              'SELL' = long was liquidated).
        """
        with self._lock:
            self._liq_events[symbol].append({
                "side":  side,
                "qty":   float(qty),
                "price": float(price),
                "ts_ms": int(ts_ms),
            })

    def get_recent_liquidations(self, symbol: str, window_seconds: float) -> list[dict]:
        """Return liquidation events within the last window_seconds, oldest→newest."""
        dq = self._liq_events.get(symbol)
        if not dq:
            return []
        cutoff_ms = (time.time() - window_seconds) * 1000.0
        snap = list(dq)
        return [e for e in snap if e["ts_ms"] >= cutoff_ms]


# Backward-compatible alias — existing code imports `from data.cache import Cache`
Cache = DataCache
