# confluence_bot — CLAUDE.md

## Project
Multi-regime crypto trading bot. Python 3.11. Async/await everywhere.
Regimes: TREND / RANGE / CRASH. Directions: LONG / SHORT.
Pairs: BTCUSDT, SOLUSDT, ETHUSDT on Binance Futures.

## Strict code rules
- Signal functions: def check_X(symbol: str, cache) -> bool
- Scorer output: dict with keys: symbol, regime, direction, score, signals(dict), fire(bool)
- Cache API: cache.get_closes(symbol, window, tf), cache.get_ohlcv(symbol, window, tf)
- All config from config.yaml via yaml.safe_load — never hardcode values
- API keys always from os.environ — never in source code
- No pandas. No TA-Lib. Use numpy only where needed.
- Handle None and empty list from cache gracefully — return False, never raise

## Architecture layers
signals/trend/   — CVD div, liq sweep, OI+funding, VPVR, HTF, whale flow
signals/range/   — absorption, wyckoff spring, perp basis, skew RoC, anchored VWAP, time dist
signals/bear/    — bearish CVD, bear OB, OI flush, HTF lower high, funding extreme, whale inflow
signals/crash/   — dead cat, liq grab short
core/            — scorer, filter, range_scorer, range_filter, bear_scorer, crash_scorer,
                   regime_detector, direction_router, executor, rr_calculator
data/            — binance_ws, binance_rest, coinglass, cryptoquant, deribit, coinbase_rest, cache
logging_/        — logger (SQLite), schema.sql, metrics_api (FastAPI)
n8n/             — confluence_workflow.json

## Free APIs used (no paid keys)
- Binance WS/REST — market data, OI, funding (no key for read)
- Coinbase REST — spot price for basis signal (no key)
- Deribit public — options skew, IV (no key)
- Bybit REST — cross-exchange OI (no key)
- Coinglass free tier — liq clusters
- Telegram Bot — alerts