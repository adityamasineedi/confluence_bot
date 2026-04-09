# confluence_bot

Multi-regime, multi-exchange crypto futures trading bot.
Python 3.11 · async/await throughout · no pandas · no TA-Lib · ccxt for multi-exchange.

---

## Exchanges

| Exchange | Live Orders | SL/TP | Connectivity Test | Via |
|----------|-------------|-------|-------------------|-----|
| Binance Futures | yes | 3-tier (algo → STOP_MARKET → abort) | yes | `binance_rest.py` |
| Bitget | yes | yes | yes | ccxt |
| BingX | yes | yes | yes | ccxt |
| Bybit | yes | yes | yes | ccxt |
| OKX | yes | yes | yes | ccxt |

Configure exchanges via the **dashboard UI** (Exchanges tab at `http://localhost:8001`)
or via environment variables. API keys stored locally in `exchanges.json` (gitignored).

---

## Regimes & strategies

| Regime | Directions | Active Scorers |
|--------|-----------|----------------|
| TREND | LONG / SHORT | fvg, ema_pullback, wyckoff_spring, liq_sweep, breakout_retest, cme_gap |
| RANGE | LONG / SHORT | wyckoff_spring, microrange, breakout_retest, cme_gap |
| BREAKOUT | LONG / SHORT | fvg, liq_sweep, breakout_retest, cme_gap |
| CRASH | SHORT only | fvg, liq_sweep, wyckoff_upthrust, ema_pullback_short, breakout_retest |
| PUMP | LONG only | fvg, liq_sweep, cme_gap, breakout_retest |

Regime classification runs every loop tick via `core/regime_detector.py`
(4H ADX + weekly return, with hysteresis).
Weekly trend gate blocks LONGs below 10W EMA and SHORTs above 10W EMA.

---

## Project layout

```
confluence_bot/
├── main.py                     entry point — starts WS streams, eval loop
├── config.yaml                 all thresholds, weights, risk params
├── requirements.txt
├── .env.example                template for API keys
├── .env.local                  local dev config (paper mode, separate DB)
├── exchanges.json              exchange API keys (gitignored)
│
├── signals/
│   ├── trend/                  cvd, fvg, vpvr, htf_structure, liquidity,
│   │                           oi_funding, order_block, whale_flow,
│   │                           rsi_divergence, ema_cross, long_short_ratio,
│   │                           bb_squeeze, ema_pullback_15m, irb, distribution
│   ├── range/                  absorption, wyckoff_spring, upthrust,
│   │                           ask_absorption, perp_basis, anchored_vwap,
│   │                           vwap_bands, rsi_oversold
│   ├── bear/                   cvd_bearish, funding_ramp
│   ├── crash/                  dead_cat, liq_grab_short
│   ├── volume_momentum.py
│   └── microrange/detector.py
│
├── core/
│   ├── regime_detector.py      5-regime detection with hysteresis
│   ├── weekly_trend_gate.py    BTC 10W EMA macro gate
│   ├── direction_router.py     LONG/SHORT/NONE per regime
│   ├── strategy_router.py      symbol × regime → active strategies
│   ├── filter.py               8-gate hard filter
│   ├── fvg_scorer.py           FVG entries with IRB + key level boost
│   ├── ema_pullback_scorer.py  EMA21 pullback (LONG + SHORT)
│   ├── microrange_scorer.py    5M tight box mean-reversion
│   ├── wyckoff_scorer.py       Wyckoff spring LONG, wick-based SL
│   ├── liq_sweep_scorer.py     equal highs/lows stop hunt
│   ├── breakout_retest_scorer.py  5M range breakout + level retest
│   ├── executor.py             bracket orders, 3-tier SL, dynamic slippage
│   ├── exchange_manager.py     multi-exchange config storage + test
│   ├── rr_calculator.py        position size with committed risk
│   ├── circuit_breaker.py      daily loss + streak halt
│   ├── trade_monitor.py        TP/SL detection, breakeven move
│   ├── cooldown_store.py       per-symbol cooldown tracking
│   └── symbol_config.py        per-coin tier + dynamic params
│
├── data/
│   ├── cache.py                in-memory store, all market data
│   ├── binance_ws.py           kline + aggTrade WebSocket streams
│   ├── binance_rest.py         Binance Futures REST (OI, funding, orders)
│   ├── exchange_router.py      unified order API → binance_rest or ccxt
│   ├── coinglass.py            liquidation data
│   ├── cryptoquant.py          exchange flow (whale signals)
│   ├── deribit.py              options IV/skew
│   └── coinbase_rest.py        spot price for basis calculation
│
├── logging_/
│   ├── schema.sql              SQLite: signals, trades, regimes tables
│   ├── logger.py               TradeLogger (async SQLite writes)
│   └── metrics_api.py          FastAPI dashboard with 9 tabs (port 8001)
│
├── backtest/
│   ├── fetch.py                download Binance historical OHLCV
│   ├── engine.py               vectorized numpy backtest
│   └── run.py                  CLI: python backtest/run.py --symbol BNBUSDT --strategy fvg
│
├── notifications/
│   ├── telegram.py             trade alerts, regime changes
│   └── telegram_commands.py    /market, /status, /help commands
│
├── ops/
│   ├── health_check.py         liveness check + daily heartbeat
│   └── deploy_vps.sh           VPS deployment (systemd service)
│
└── tests/
    └── test_signals.py         pytest unit tests (MockCache)
```

---

## Setup

```bash
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env
# edit .env with your API keys

python main.py
```

Dashboard (once running): `http://localhost:8001`

---

## Local development (without affecting VPS)

```bash
cp .env.local .env
python main.py
```

`.env.local` sets:
- `PAPER_MODE=1` — no real orders placed
- `DB_PATH=confluence_bot_local.db` — separate database from production
- `METRICS_PORT=8002` — no port conflict with VPS
- `TELEGRAM_CHAT_ID=` — no alert spam

---

## Exchange configuration

**Option 1 — Dashboard UI:**
1. Open `http://localhost:8001` → Exchanges tab
2. Add exchange (Binance/Bitget/BingX/Bybit/OKX) with API key + secret
3. Click **Test** to verify connectivity and see USDT balance
4. Click **Set Active** → restart bot

**Option 2 — Environment variables (Binance only):**
```
BINANCE_API_KEY=your_key
BINANCE_SECRET=your_secret
```

**Testnet:** Check the "Testnet" box in the UI, or set:
```
BINANCE_BASE_URL=https://testnet.binancefuture.com
```

---

## Environment variables

| Variable | Purpose |
|---|---|
| `PAPER_MODE` | `1` = no real orders (default: `0`) |
| `BINANCE_API_KEY` / `BINANCE_SECRET` | Futures trading (or use dashboard UI) |
| `BINANCE_BASE_URL` | Override base URL (e.g. testnet) |
| `COINGLASS_API_KEY` | OI, funding, liquidations |
| `TELEGRAM_CHAT_ID` | Telegram alerts |
| `DB_PATH` | SQLite file path (default: `confluence_bot.db`) |
| `METRICS_PORT` | Dashboard port override (default: `8001`) |

---

## Risk management

| Parameter | Value | Notes |
|---|---|---|
| `fixed_risk_usdt` | 50 | Per-trade risk in USDT |
| `max_open_positions` | 5 | |
| `max_same_direction_positions` | 3 | Correlated-exposure cap |
| `leverage` | 3 | ISOLATED margin |
| `max_daily_loss_pct` | 3.0% | Circuit breaker triggers |
| `max_consecutive_losses` | 6 | Circuit breaker triggers |
| `post_trade_cooldown_mins` | 10 | Per-symbol cooldown |
| `atr_spike_gate` | enabled | Blocks entries during flash crashes |
| `weekly_trend_gate` | enabled | Macro regime filter |

---

## Running tests

```bash
pytest tests/test_signals.py -v
```

## Backtest

```bash
python backtest/run.py --symbol BNBUSDT --strategy fvg
```

Pass criteria: PF >= 1.50, WR >= 38%, trades >= 20.
