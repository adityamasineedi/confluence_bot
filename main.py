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
import threading
import yaml

# Load .env before anything else reads environment variables
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # python-dotenv not installed — rely on shell env vars

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("confluence_bot")

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

    # 3. Binance WebSocket streams
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
    def _start_metrics() -> None:
        import uvicorn
        from logging_.metrics_api import app
        uvicorn.run(app, host=metrics_host, port=metrics_port, log_level="warning")

    threading.Thread(target=_start_metrics, daemon=True).start()
    log.info("Metrics server starting on http://%s:%d", metrics_host, metrics_port)

    # 6b. Trade monitor — closes positions when TP/SL is hit
    from core.trade_monitor import monitor_trades
    asyncio.create_task(monitor_trades(cache))

    # 6c. Periodic health check — alerts via Telegram if bot goes silent
    async def _health_check_periodic() -> None:
        from ops.health_check import run_silent_check
        await run_silent_check()

    extra_tasks.append(
        asyncio.create_task(_periodic(_health_check_periodic, interval=300))
    )

    # 7. Main evaluation loop
    prev_regimes: dict[str, str] = {}
    # Stagger per-symbol evaluation evenly across the loop interval so each
    # symbol gets a distinct timestamp in the signals log.
    stagger_sleep = loop_interval / max(len(symbols), 1)

    while True:
        for symbol in symbols:
            try:
                regime = detect_regime(symbol, cache)
                regime_str = str(regime)

                # Log regime changes
                if prev_regimes.get(symbol) != regime_str:
                    old_regime = prev_regimes.get(symbol, "")
                    prev_regimes[symbol] = regime_str
                    log.info("%s regime → %s", symbol, regime_str)
                    asyncio.create_task(logger.log_regime(symbol, regime_str))
                    if old_regime:   # don't alert on first startup
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
    asyncio.run(main())
