"""Binance Futures WebSocket manager — streams market data into the cache."""
import asyncio
import json
import logging
import time
from typing import Iterator

import websockets
import websockets.exceptions

from .cache import DataCache

log = logging.getLogger(__name__)

_WS_BASE          = "wss://fstream.binance.com/stream"
_KLINE_TFS        = ("1m", "5m", "15m", "1h", "4h")
_MAX_STREAMS      = 200          # Binance limit per connection (actual limit is 1024; 200 is conservative)
_BACKOFF_INIT_S   = 5.0
_BACKOFF_MAX_S    = 60.0
_BACKOFF_RESET_S  = 10.0         # connected this long → reset backoff on next reconnect
_CVD_WARMUP_S     = 20 * 60      # discard CVD for 20 min after reconnect


class BinanceWebSocket:
    """Async WebSocket manager for Binance Futures market data.

    Subscribes to aggTrade and kline_{1m,5m,15m,1h,4h} streams for every
    configured symbol via a single combined-stream URL.  If the total stream
    count exceeds _MAX_STREAMS, multiple connections are opened in parallel.

    CVD bookkeeping:
    - Per-candle delta is accumulated from aggTrade ticks per (symbol, tf).
    - On kline close the accumulated delta is flushed to cache via
      push_cvd_value() — but only after the warmup window has elapsed.
    - On every (re)connect the warmup timer is reset; signal functions that
      read CVD should call ws.is_cvd_ready(symbol) as a guard.

    Usage::

        ws = BinanceWebSocket(["BTCUSDT", "ETHUSDT"], cache)
        await ws.run()          # runs forever, call from asyncio.create_task()
    """

    def __init__(self, symbols: list[str], cache: DataCache) -> None:
        self._symbols    = [s.upper() for s in symbols]
        self._cache      = cache
        # (symbol, tf) -> running delta for the current (open) candle
        self._pending_cvd: dict[tuple[str, str], float] = {}
        # symbol -> monotonic time when CVD becomes trustworthy
        self._cvd_ready_at: dict[str, float] = {}
        self._mark_warming_up()

    # ── Public entry point ────────────────────────────────────────────────────

    async def run(self) -> None:
        """Start streaming.  Runs forever; meant to be launched as a task."""
        urls = list(self._build_urls())
        if not urls:
            log.warning("BinanceWebSocket: no symbols configured")
            return
        if len(urls) == 1:
            await self._run_with_backoff(urls[0])
        else:
            await asyncio.gather(*(self._run_with_backoff(u) for u in urls))

    # ── CVD warmup queries (called by signal functions) ───────────────────────

    def is_cvd_ready(self, symbol: str) -> bool:
        """True once the post-reconnect warmup has elapsed for *symbol*."""
        return time.monotonic() >= self._cvd_ready_at.get(symbol, 0.0)

    def cvd_warmup_remaining(self, symbol: str) -> float:
        """Seconds until CVD is trusted; 0.0 when ready."""
        return max(0.0, self._cvd_ready_at.get(symbol, 0.0) - time.monotonic())

    # ── URL construction ──────────────────────────────────────────────────────

    def _build_urls(self) -> Iterator[str]:
        """Yield one combined-stream URL per chunk of _MAX_STREAMS streams."""
        streams: list[str] = []
        for sym in self._symbols:
            s = sym.lower()
            streams.append(f"{s}@aggTrade")
            for tf in _KLINE_TFS:
                streams.append(f"{s}@kline_{tf}")

        for i in range(0, len(streams), _MAX_STREAMS):
            chunk = streams[i : i + _MAX_STREAMS]
            yield f"{_WS_BASE}?streams={'/'.join(chunk)}"

    # ── Reconnect loop ────────────────────────────────────────────────────────

    async def _run_with_backoff(self, url: str) -> None:
        """Connect to *url* forever, with exponential backoff on failure."""
        backoff = _BACKOFF_INIT_S
        while True:
            started = time.monotonic()
            try:
                await self._connect_and_stream(url)
                # Server-side clean close (24 h cycle) — treat as expected
            except (
                websockets.exceptions.ConnectionClosedError,
                websockets.exceptions.ConnectionClosedOK,
            ):
                pass  # logged below
            except OSError as exc:
                log.warning("WS network error: %s", exc)
            except Exception:
                log.exception("WS unexpected error")
            finally:
                self._on_reconnect()

            elapsed = time.monotonic() - started
            if elapsed >= _BACKOFF_RESET_S:
                backoff = _BACKOFF_INIT_S  # was stable — reset backoff

            log.info("WS disconnected after %.1fs. Retry in %.0fs.", elapsed, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, _BACKOFF_MAX_S)

    async def _connect_and_stream(self, url: str) -> None:
        log.info("WS connecting (%d streams) …", len(self._symbols) * (1 + len(_KLINE_TFS)))
        async with websockets.connect(
            url,
            # The websockets library auto-responds to server ping frames with pong.
            # We also send client-side pings so dead TCP connections are detected.
            ping_interval=20,
            ping_timeout=30,
            close_timeout=5,
            max_size=2 ** 20,   # 1 MiB — plenty for any single Binance message
        ) as ws:
            log.info("WS connected")
            async for raw in ws:
                self._dispatch(raw)

    # ── Message dispatch ──────────────────────────────────────────────────────

    def _dispatch(self, raw: str | bytes) -> None:
        try:
            msg = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            log.debug("WS: unparseable message: %r…", str(raw)[:60])
            return

        # Combined stream wraps every event: {"stream": "…", "data": {…}}
        data: dict = msg.get("data", msg)
        e_type: str = data.get("e", "")

        if e_type == "aggTrade":
            self._handle_agg_trade(data)
        elif e_type == "kline":
            self._handle_kline(data)
        # "ping" events at the application level are informational;
        # WebSocket-level pings are handled transparently by the library.

    # ── aggTrade handler ──────────────────────────────────────────────────────

    def _handle_agg_trade(self, data: dict) -> None:
        """Store trade tick and accumulate per-candle CVD across all active tfs."""
        symbol: str  = data["s"]
        qty          = float(data["q"])
        is_buyer_maker: bool = data["m"]   # True  = sell-initiated (maker is buyer → taker sold)
                                           # False = buy-initiated  (taker bought)
        trade = {
            "ts":             data["T"],   # trade time, Unix ms
            "price":          float(data["p"]),
            "qty":            qty,
            "is_buyer_maker": is_buyer_maker,
        }
        self._cache.push_agg_trade(symbol, trade)

        # Positive delta = net buying pressure
        delta = qty if not is_buyer_maker else -qty
        for tf in _KLINE_TFS:
            key = (symbol, tf)
            self._pending_cvd[key] = self._pending_cvd.get(key, 0.0) + delta

    # ── Kline handler ─────────────────────────────────────────────────────────

    def _handle_kline(self, data: dict) -> None:
        """Push candle to cache (live + closed). On close, flush CVD accumulator."""
        k         = data["k"]
        symbol    = k["s"]
        tf        = k["i"]         # "1m", "5m", "15m", "1h", "4h"
        is_closed = k["x"]

        candle = {
            "o":  float(k["o"]),
            "h":  float(k["h"]),
            "l":  float(k["l"]),
            "c":  float(k["c"]),
            "v":  float(k["v"]),
            "ts": int(k["t"]),     # candle open time, Unix ms — stable across ticks
        }

        # Always upsert so get_last_price() stays current on in-progress candles
        self._cache.push_candle(symbol, tf, candle)

        if is_closed:
            self._flush_cvd(symbol, tf)

    def _flush_cvd(self, symbol: str, tf: str) -> None:
        """Commit accumulated delta for the just-closed candle, then reset."""
        key   = (symbol, tf)
        delta = self._pending_cvd.pop(key, 0.0)

        if self.is_cvd_ready(symbol):
            # Warmup complete — delta represents a full, cleanly-started candle
            self._cache.push_cvd_value(symbol, tf, delta)
        # else: candle started before/during warmup; discard to avoid partial data

    # ── Warmup helpers ────────────────────────────────────────────────────────

    def _mark_warming_up(self) -> None:
        """Set warmup expiry for all symbols based on current monotonic clock."""
        ready_at = time.monotonic() + _CVD_WARMUP_S
        for sym in self._symbols:
            self._cvd_ready_at[sym] = ready_at

    def _on_reconnect(self) -> None:
        """Clear partial CVD state and restart warmup timers after a disconnect."""
        self._pending_cvd.clear()
        self._mark_warming_up()
        log.info(
            "WS: CVD warmup restarted (%.0f min) for %d symbol(s) after disconnect",
            _CVD_WARMUP_S / 60,
            len(self._symbols),
        )


# ── Module-level convenience wrapper ─────────────────────────────────────────

async def start_streams(symbols: list[str], cache: DataCache) -> None:
    """Instantiate BinanceWebSocket and run.  Drop-in for main.py task creation."""
    await BinanceWebSocket(symbols, cache).run()
