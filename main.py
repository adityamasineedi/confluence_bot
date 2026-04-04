"""confluence_bot — entry point.

Startup sequence:
  1. Initialise DataCache
  2. Start Binance WebSocket streams (klines + aggTrades)
  3. Start Binance REST poller (OI, funding, history)
  4. Start optional external data pollers (Coinglass, CryptoQuant, Deribit, Coinbase)
  5. Start FastAPI metrics server in a daemon thread
  6. Run main evaluation loop: regime → direction → score → execute
"""
import asyncio
import logging
import os
import signal
import sys
import threading
import yaml

# Load .env before anything else reads environment variables
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # python-dotenv not installed — rely on shell env vars

from logging.handlers import RotatingFileHandler

os.makedirs("logs", exist_ok=True)

_rotating_handler = RotatingFileHandler(
    "logs/bot.log",
    maxBytes=10 * 1024 * 1024,   # 10 MB per file
    backupCount=5,
    encoding="utf-8",
)
_rotating_handler.setFormatter(logging.Formatter(
    "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
))

logging.basicConfig(
    level=logging.INFO,
    handlers=[
        logging.StreamHandler(),
        _rotating_handler,
    ],
)
log = logging.getLogger("confluence_bot")
log.info("=" * 60)
log.info("Bot started — PAPER_MODE=%s  balance will load shortly",
         os.environ.get("PAPER_MODE", "0") == "1")
log.info("=" * 60)


def _handle_shutdown(sig, frame):
    log.info("Shutdown signal received — stopping bot cleanly")
    sys.exit(0)


signal.signal(signal.SIGINT,  _handle_shutdown)
signal.signal(signal.SIGTERM, _handle_shutdown)

_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.yaml")
with open(_CONFIG_PATH) as _f:
    cfg = yaml.safe_load(_f)

# Paper mode: set PAPER_MODE=1 in environment to skip real order placement
PAPER_MODE = os.environ.get("PAPER_MODE", "0") == "1"


# ── Periodic task wrapper ─────────────────────────────────────────────────────

async def _periodic(fn, *args, interval: float, **kwargs) -> None:
    """Run an async coroutine function every `interval` seconds, forever."""
    while True:
        try:
            await fn(*args, **kwargs)
        except Exception as exc:
            log.warning("Periodic %s failed: %s", getattr(fn, "__name__", fn), exc)
        await asyncio.sleep(interval)


# ── Main ──────────────────────────────────────────────────────────────────────

async def main() -> None:
    """Bootstrap all components and run the main signal evaluation loop."""
    from data.cache        import DataCache
    from data.binance_ws   import start_streams
    from data.binance_rest import BinanceRestPoller
    from core.regime_detector import detect_regime
    from core.direction_router import route_direction
    from core.executor     import execute_signal, restore_active_deals
    from logging_.logger   import TradeLogger

    symbols: list[str]  = cfg.get("symbols",              ["BTCUSDT", "ETHUSDT"])
    loop_interval: float = cfg.get("loop_interval_seconds", 60.0)
    metrics_host: str   = cfg["logging"]["metrics_host"]
    metrics_port: int   = cfg["logging"]["metrics_port"]

    log.info("Starting confluence_bot  |  PAPER_MODE=%s  |  symbols=%s", PAPER_MODE, symbols)

    # 1. Cache
    cache = DataCache()

    # 2. TradeLogger (initialises DB schema on first run)
    logger = TradeLogger()
    restore_active_deals(await logger.load_active_deals())

    # 3. Configure leverage + margin type on exchange
    if not PAPER_MODE:
        from data.binance_rest import setup_symbols
        _leverage    = cfg["risk"]["leverage"]
        _margin_type = cfg["risk"].get("margin_type", "ISOLATED")
        await setup_symbols(symbols, _leverage, _margin_type)

    # 3b. Binance WebSocket streams
    ws_task = asyncio.create_task(start_streams(symbols, cache))

    # 4. Binance REST poller (OI, funding, weekly/daily history)
    rest_poller = BinanceRestPoller(symbols, cache)
    rest_task   = asyncio.create_task(rest_poller.run())

    # 5. Optional external data pollers (graceful skip when API keys absent)
    extra_tasks: list[asyncio.Task] = []
    if os.environ.get("COINGLASS_API_KEY"):
        from data.coinglass import refresh_cache as cg_refresh
        extra_tasks.append(
            asyncio.create_task(_periodic(cg_refresh, symbols, cache, interval=300))
        )
    # CryptoQuant: now uses large-trade heuristic — no API key required
    from data.cryptoquant import refresh_cache as cq_refresh
    extra_tasks.append(
        asyncio.create_task(_periodic(cq_refresh, symbols, cache, interval=30))
    )
    # Deribit options skew — public API, no key needed, always start (BTC+ETH only)
    from data.deribit import refresh_cache as db_refresh
    extra_tasks.append(
        asyncio.create_task(_periodic(db_refresh, ["BTC", "ETH"], cache, interval=300))
    )
    # Coinbase spot price — public API, no key needed, always start
    from data.coinbase_rest import refresh_cache as cb_refresh
    extra_tasks.append(
        asyncio.create_task(_periodic(cb_refresh, symbols, cache, interval=30))
    )

    # 6. FastAPI metrics server in a daemon thread
    from logging_.metrics_api import set_cache as _metrics_set_cache
    _metrics_set_cache(cache)   # give metrics API access to live Coinglass cache data

    def _start_metrics() -> None:
        import uvicorn
        from logging_.metrics_api import app
        uvicorn.run(app, host=metrics_host, port=metrics_port, log_level="warning")

    threading.Thread(target=_start_metrics, daemon=True).start()
    log.info("Metrics server starting on http://%s:%d", metrics_host, metrics_port)

    # 6b. Lead-lag strategy loop (independent of regime, runs every 30s)
    if cfg.get("leadlag", {}).get("enabled", False):
        from core.leadlag_loop import run_leadlag_loop
        extra_tasks.append(
            asyncio.create_task(run_leadlag_loop(symbols, cache))
        )
        log.info("Lead-lag strategy enabled")

    # 6b2. Session open trap (fires at session open + 15 min: Asia/London/NY)
    if cfg.get("session_trap", {}).get("enabled", False):
        from core.session_loop import run_session_loop
        extra_tasks.append(
            asyncio.create_task(run_session_loop(symbols, cache))
        )
        log.info("Session open trap enabled")

    # 6b3a0. OI Spike Fade (fades liquidation cascades on OI surge + wick rejection)
    if cfg.get("oi_spike", {}).get("enabled", False):
        from core.oi_spike_loop import run_oi_spike_loop
        extra_tasks.append(
            asyncio.create_task(run_oi_spike_loop(symbols, cache))
        )
        log.info("OI Spike Fade strategy enabled")

    # 6b3a. VWAP Band Reversion (15m ±2σ band touch rejections — mean reversion)
    if cfg.get("vwap_band", {}).get("enabled", False):
        from core.vwap_band_loop import run_vwap_band_loop
        extra_tasks.append(
            asyncio.create_task(run_vwap_band_loop(symbols, cache))
        )
        log.info("VWAP Band Reversion strategy enabled")

    # 6b3c. FVG Fill strategy (scans every 60s for 1H imbalance zone retests)
    if cfg.get("fvg", {}).get("enabled", False):
        from core.fvg_loop import run_fvg_loop
        extra_tasks.append(
            asyncio.create_task(run_fvg_loop(symbols, cache))
        )
        log.info("FVG Fill strategy enabled")

    # 6b5. 15m EMA Pullback (trend-continuation at EMA21, 4H filtered)
    if cfg.get("ema_pullback", {}).get("enabled", False):
        from core.ema_pullback_loop import run_ema_pullback_loop
        extra_tasks.append(
            asyncio.create_task(run_ema_pullback_loop(symbols, cache))
        )
        log.info("EMA Pullback strategy enabled")

    # 6b6. Wyckoff spring (range low spring + absorption — mean reversion long)
    if cfg.get("wyckoff_spring", {}).get("enabled", False):
        from core.wyckoff_loop import run_wyckoff_loop
        extra_tasks.append(
            asyncio.create_task(run_wyckoff_loop(symbols, cache))
        )
        log.info("Wyckoff Spring strategy enabled")

    # 6b8. Liquidity sweep (equal highs/lows stop hunt — long + short)
    if cfg.get("liq_sweep", {}).get("enabled", False):
        from core.liq_sweep_loop import run_liq_sweep_loop
        extra_tasks.append(
            asyncio.create_task(run_liq_sweep_loop(symbols, cache))
        )
        log.info("Liquidity Sweep strategy enabled")

    # 6b7. HTF Demand/Supply Zone (4H zone retests with 1H confirmation)
    if cfg.get("zone", {}).get("enabled", False):
        from core.zone_loop import run_zone_loop
        extra_tasks.append(
            asyncio.create_task(run_zone_loop(symbols, cache))
        )
        log.info("Zone Retest strategy enabled")

    # 6c. Micro-range flip strategy loop (independent, runs every 30s)
    if cfg.get("microrange", {}).get("enabled", False):
        from core.microrange_loop import run_microrange_loop
        _mr_exclude = [s.upper() for s in cfg["microrange"].get("exclude_symbols", [])]
        _mr_symbols = [s for s in symbols if s not in _mr_exclude]
        extra_tasks.append(
            asyncio.create_task(run_microrange_loop(_mr_symbols, cache))
        )
        log.info("Micro-range flip strategy enabled — symbols=%s  excluded=%s",
                 _mr_symbols, _mr_exclude)

    # 6c1. Breakout retest (5M range breakout + level retest — all 8 coins)
    if cfg.get("breakout_retest", {}).get("enabled", False):
        from core.breakout_retest_loop import run_breakout_retest_loop
        extra_tasks.append(
            asyncio.create_task(run_breakout_retest_loop(symbols, cache))
        )
        log.info("Breakout Retest strategy enabled")

    # 6c2. BTC dominance poller — refreshes every 30 min from CoinGecko
    async def _refresh_btc_dominance(interval_secs: int = 1800) -> None:
        from data.binance_rest import get_btc_dominance
        while True:
            dom = await get_btc_dominance()
            if dom > 0:
                cache.push_btc_dominance(dom)
                log.info("BTC dominance updated: %.1f%%", dom * 100)
            await asyncio.sleep(interval_secs)

    _dom_interval = int(cfg.get("btc_dominance", {}).get("fetch_interval_mins", 30)) * 60
    asyncio.create_task(_refresh_btc_dominance(_dom_interval))

    # 6c3. Hourly DB pruning — keeps signals table from growing unbounded
    async def _prune_db_periodic() -> None:
        import sqlite3
        db = os.environ.get("DB_PATH", "confluence_bot.db")
        while True:
            await asyncio.sleep(3600)  # every hour
            try:
                with sqlite3.connect(db) as conn:
                    deleted_s = conn.execute(
                        "DELETE FROM signals WHERE ts < datetime('now', '-1 day')"
                    ).rowcount
                    deleted_r = conn.execute(
                        "DELETE FROM regimes WHERE ts < datetime('now', '-30 days')"
                    ).rowcount
                    conn.commit()
                # VACUUM must run outside a transaction
                v_conn = sqlite3.connect(db)
                v_conn.execute("VACUUM")
                v_conn.close()
                log.info("DB prune: removed %d signals, %d regimes",
                         deleted_s, deleted_r)
            except Exception as exc:
                log.warning("DB prune failed: %s", exc)

    asyncio.create_task(_prune_db_periodic())

    # 6d. Trade monitor — closes positions when TP/SL is hit
    from core.trade_monitor import monitor_trades
    asyncio.create_task(monitor_trades(cache))

    # 6d2. Telegram command listener (/market, /status, /help)
    from notifications.telegram_commands import start_command_listener
    extra_tasks.append(
        asyncio.create_task(start_command_listener(cache, symbols))
    )

    # 6f. Swing structure monitor — Telegram alert when HH+HL confirmed
    from core.swing_monitor import run_swing_monitor
    extra_tasks.append(
        asyncio.create_task(run_swing_monitor(symbols, cache, interval=300))
    )
    log.info("Swing monitor started (5-min check, alerts on HH+HL flip)")

    # 6e. Periodic health check — alerts via Telegram if bot goes silent
    async def _health_check_periodic() -> None:
        from ops.health_check import run_silent_check
        await run_silent_check()

    extra_tasks.append(
        asyncio.create_task(_periodic(_health_check_periodic, interval=300))
    )

    # 7. Wait for cache to be populated before firing any signals
    _CACHE_MIN_BARS   = 50    # need at least 50 × 1h bars for ADX/EMA warmup
    _CACHE_MIN_BARS5m = 32    # need 32 × 5m bars for microrange warmup
    _CACHE_WAIT_MAX   = 120   # max 2 minutes; then proceed anyway with a warning
    _cache_waited     = 0
    log.info("Waiting for cache to populate (need %d × 1h bars per symbol)...",
             _CACHE_MIN_BARS)
    while _cache_waited < _CACHE_WAIT_MAX:
        ready = all(
            len(cache.get_ohlcv(s, _CACHE_MIN_BARS, "1h")) >= _CACHE_MIN_BARS
            for s in symbols
        )
        ready5m = all(
            len(cache.get_ohlcv(s, _CACHE_MIN_BARS5m, "5m")) >= _CACHE_MIN_BARS5m
            for s in symbols
        )
        if ready and ready5m:
            log.info("Cache ready — starting signal evaluation loop")
            break
        await asyncio.sleep(5)
        _cache_waited += 5
    else:
        log.warning("Cache not fully populated after %ds — starting anyway", _CACHE_WAIT_MAX)

    for sym in symbols:
        bars_5m = cache.get_ohlcv(sym, window=50, tf="5m")
        bars_1h = cache.get_ohlcv(sym, window=50, tf="1h")
        log.info("Cache check %s: 5m bars=%d  1h bars=%d",
                 sym, len(bars_5m) if bars_5m else 0,
                 len(bars_1h) if bars_1h else 0)

    # 8. Main evaluation loop
    prev_regimes: dict[str, str] = {}
    _regime_alert_ts: dict[str, float] = {}   # symbol → last Telegram alert timestamp
    _REGIME_ALERT_COOLDOWN = 1800.0            # max one Telegram alert per symbol per 30 min
    _regime_eval_count: dict[str, int] = {}   # suppress alerts for first 2 evals after startup
    # Stagger per-symbol evaluation evenly across the loop interval so each
    # symbol gets a distinct timestamp in the signals log.
    stagger_sleep = loop_interval / max(len(symbols), 1)

    while True:
        for symbol in symbols:
            try:
                regime = detect_regime(symbol, cache)
                regime_str = str(regime)

                # Log regime changes
                _regime_eval_count[symbol] = _regime_eval_count.get(symbol, 0) + 1
                if prev_regimes.get(symbol) != regime_str:
                    import time as _time
                    old_regime = prev_regimes.get(symbol, "")
                    prev_regimes[symbol] = regime_str
                    log.info("%s regime → %s", symbol, regime_str)
                    asyncio.create_task(logger.log_regime(symbol, regime_str))
                    now = _time.monotonic()
                    # Skip alert for first 2 evals per symbol (startup warmup) and enforce cooldown
                    if (old_regime
                            and _regime_eval_count[symbol] > 2
                            and (now - _regime_alert_ts.get(symbol, 0)) >= _REGIME_ALERT_COOLDOWN):
                        _regime_alert_ts[symbol] = now
                        from notifications.telegram import send_regime_change
                        asyncio.create_task(
                            send_regime_change(symbol, old_regime, regime_str)
                        )

                all_signals = await route_direction(symbol, cache, regime)

                for score_dict in all_signals:
                    asyncio.create_task(logger.log_signal(score_dict))

                    if not score_dict.get("fire"):
                        continue

                    log.info(
                        "Signal FIRE: %s %s %s  score=%.2f",
                        score_dict["direction"],
                        symbol,
                        regime_str,
                        score_dict["score"],
                    )
                    order = await execute_signal(score_dict, cache)
                    if order:
                        log.info(
                            "Order placed: %s %s  qty=%.4f  entry=%s",
                            score_dict["direction"],
                            symbol,
                            order.get("qty", 0),
                            order.get("entry", "MARKET"),
                        )

            except Exception as exc:
                log.exception("Error evaluating %s: %s", symbol, exc)
            finally:
                await asyncio.sleep(stagger_sleep)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        log.info("Bot stopped by user")
    except Exception as exc:
        log.exception("Bot crashed unexpectedly: %s", exc)
        raise
