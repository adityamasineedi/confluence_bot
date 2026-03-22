"""BacktestCache — time-aware DataCache backed by pre-loaded historical data.

Implements the full DataCache public API so all existing signal functions,
scorers, filters, and the regime detector work without modification.

The cursor advances bar-by-bar; every get_* call returns only data whose
timestamp is <= the current cursor.
"""
import bisect


class BacktestCache:
    """
    Drop-in replacement for DataCache during backtesting.

    Construction
    ------------
    ohlcv:   dict  "SYMBOL:tf" → list[{ts,o,h,l,c,v}]  (all history, sorted by ts)
    oi:      dict  "SYMBOL"    → list[{ts, oi}]
    funding: dict  "SYMBOL"    → list[{ts, rate}]

    Usage
    -----
    cache.advance(bar_ts_ms)   # move cursor forward one bar
    regime = detector.detect(symbol, cache)
    fired  = await route_direction(symbol, cache, regime)
    """

    def __init__(
        self,
        ohlcv:   dict[str, list[dict]],
        oi:      dict[str, list[dict]],
        funding: dict[str, list[dict]],
    ) -> None:
        self._ohlcv   = ohlcv
        self._oi      = oi
        self._funding = funding
        self._cursor_ts: int = 0

        # Pre-build sorted timestamp arrays for fast bisect slicing
        self._ohlcv_ts: dict[str, list[int]] = {
            k: [c["ts"] for c in v] for k, v in ohlcv.items()
        }
        self._oi_ts: dict[str, list[int]] = {
            k: [e["ts"] for e in v] for k, v in oi.items()
        }
        self._fund_ts: dict[str, list[int]] = {
            k: [e["ts"] for e in v] for k, v in funding.items()
        }

        # Writable state — set by RegimeDetector during detect()
        self._range_high:     dict[str, float] = {}
        self._range_low:      dict[str, float] = {}
        self._range_start_ts: dict[str, int]   = {}

    # ── Cursor ────────────────────────────────────────────────────────────────

    def advance(self, ts_ms: int) -> None:
        """Move the time cursor to ts_ms (inclusive)."""
        self._cursor_ts = ts_ms

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _slice_ohlcv(self, symbol: str, tf: str, window: int) -> list[dict]:
        key = f"{symbol}:{tf}"

        def _visible(k: str) -> list[dict]:
            c = self._ohlcv.get(k)
            if not c:
                return []
            ts = self._ohlcv_ts.get(k, [])
            end = bisect.bisect_right(ts, self._cursor_ts)
            return c[:end][-window:]

        visible = _visible(key)
        if visible:
            return visible

        # Fallback: requested TF has no data before the cursor — use lower frequency
        for fallback_tf in ("1h", "4h", "1d"):
            fkey = f"{symbol}:{fallback_tf}"
            if fkey == key:
                continue
            visible = _visible(fkey)
            if visible:
                return visible

        return []

    # ── OHLCV reads ───────────────────────────────────────────────────────────

    def get_ohlcv(self, symbol: str, window: int, tf: str) -> list[dict]:
        return self._slice_ohlcv(symbol, tf, window)

    def get_closes(self, symbol: str, window: int, tf: str) -> list[float]:
        return [c["c"] for c in self._slice_ohlcv(symbol, tf, window)]

    def get_vol_ma(self, symbol: str, window: int, tf: str) -> float:
        candles = self._slice_ohlcv(symbol, tf, window)
        if len(candles) < 2:
            return 0.0
        return sum(c["v"] for c in candles) / len(candles)

    def get_vol_24h(self, symbol: str) -> float | None:
        candles = self._slice_ohlcv(symbol, "1h", 24)
        if len(candles) < 2:
            return None
        return sum(c["v"] * c["c"] for c in candles)

    def get_last_price(self, symbol: str) -> float:
        for tf in ("1m", "5m", "15m", "1h", "4h"):
            s = self._slice_ohlcv(symbol, tf, 1)
            if s:
                return s[-1]["c"]
        return 0.0

    # ── CVD (not available historically → always empty) ───────────────────────

    def get_cvd(self, symbol: str, window: int, tf: str) -> list[float]:
        return []

    # ── Open Interest ─────────────────────────────────────────────────────────

    def get_oi_history(self, symbol: str, window: int) -> list[float]:
        entries  = self._oi.get(symbol, [])
        ts_list  = self._oi_ts.get(symbol, [])
        end_idx  = bisect.bisect_right(ts_list, self._cursor_ts)
        visible  = entries[:end_idx]
        return [e["oi"] for e in visible[-window:]]

    def get_oi(self, symbol: str, offset_hours: int = 0) -> float | None:
        hist = self.get_oi_history(symbol, offset_hours + 2)
        if not hist:
            return None
        idx = -(1 + offset_hours)
        try:
            return hist[idx]
        except IndexError:
            return hist[0]

    # ── Funding Rate ──────────────────────────────────────────────────────────

    def get_funding_rate(self, symbol: str) -> float | None:
        entries = self._funding.get(symbol, [])
        ts_list = self._fund_ts.get(symbol, [])
        end_idx = bisect.bisect_right(ts_list, self._cursor_ts)
        if end_idx == 0:
            return None
        return entries[end_idx - 1]["rate"]

    # ── Range bounds (written by RegimeDetector) ──────────────────────────────

    def get_range_high(self, symbol: str) -> float | None:
        return self._range_high.get(symbol)

    def get_range_low(self, symbol: str) -> float | None:
        return self._range_low.get(symbol)

    def get_range_start_timestamp(self, symbol: str) -> int | None:
        return self._range_start_ts.get(symbol)

    def set_range_high(self, symbol: str, val: float) -> None:
        self._range_high[symbol] = float(val)

    def set_range_low(self, symbol: str, val: float) -> None:
        self._range_low[symbol] = float(val)

    def set_range_start_timestamp(self, symbol: str, ts: int) -> None:
        self._range_start_ts[symbol] = int(ts)

    # ── Unavailable data (return safe empty values) ───────────────────────────

    def get_liq_clusters(self, symbol: str) -> list[dict]:
        return []

    def get_basis_history(self, symbol: str, n: int) -> list[float]:
        return []

    def get_skew_history(self, symbol: str, n: int) -> list[float]:
        return []

    def get_agg_trades(self, symbol: str, window_seconds: float) -> list[dict]:
        return []

    def get_exchange_inflow(self, symbol: str) -> float | None:
        return None

    def get_inflow_ma(self, symbol: str, days: int) -> float:
        return 0.0

    def get_account_balance(self) -> float:
        return 10_000.0   # fixed account size for sizing calculations

    # ── Write no-ops (called by live data sources, ignored in backtest) ───────

    def push_candle(self, *args) -> None:        pass
    def push_cvd_value(self, *args) -> None:     pass
    def push_oi(self, *args) -> None:            pass
    def set_funding_rate(self, *args) -> None:   pass
    def set_liq_clusters(self, *args) -> None:   pass
    def push_basis(self, *args) -> None:         pass
    def push_skew(self, *args) -> None:          pass
    def push_agg_trade(self, *args) -> None:     pass
    def push_exchange_inflow(self, *args) -> None: pass
    def set_account_balance(self, *args) -> None:  pass
