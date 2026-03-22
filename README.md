# confluence_bot

Multi-regime, multi-direction crypto futures trading bot.
Python 3.11 · async/await throughout · no pandas · no TA-Lib.

---

## Regimes & directions

| Regime | Directions | Scorers |
|--------|-----------|---------|
| TREND  | LONG / SHORT | `core/scorer.py`, `core/bear_scorer.py` |
| RANGE  | LONG / SHORT | `core/range_scorer.py`, `core/range_short_scorer.py` |
| CRASH  | SHORT only   | `core/crash_scorer.py` |

Regime classification runs every loop tick via `core/regime_detector.py`
(ADX-14 on 4H for TREND, max-drawdown on 1H for CRASH, default RANGE).
Direction routing is handled by `core/direction_router.py`.

---

## Project layout

```
confluence_bot/
├── main.py                     entry point — starts WS streams, eval loop
├── config.yaml                 all thresholds, weights, risk params
├── requirements.txt
├── .env.example                template for API keys
│
├── signals/
│   ├── trend/                  TREND LONG signals
│   │   ├── cvd.py              check_cvd_bullish, check_cvd_divergence
│   │   ├── liquidity.py        check_liq_sweep
│   │   ├── oi_funding.py       check_oi_funding
│   │   ├── vpvr.py             check_vpvr_support
│   │   ├── htf_structure.py    check_htf_structure
│   │   ├── order_block.py      check_order_block
│   │   ├── options.py          check_options_flow
│   │   └── whale_flow.py       check_whale_flow
│   │
│   ├── range/                  RANGE signals (both directions)
│   │   ├── absorption.py       check_absorption, check_absorption_ratio
│   │   ├── ask_absorption.py   check_ask_absorption
│   │   ├── wyckoff_spring.py   check_wyckoff_spring
│   │   ├── upthrust.py         check_upthrust
│   │   ├── perp_basis.py       check_perp_basis
│   │   ├── options_skew.py     check_options_skew
│   │   ├── anchored_vwap.py    check_anchored_vwap
│   │   ├── time_distribution.py check_time_distribution
│   │   └── call_skew_roc.py    check_call_skew_roc
│   │
│   ├── bear/                   TREND SHORT signals
│   │   ├── cvd_bearish.py      check_cvd_bearish
│   │   ├── bear_ob.py          check_bear_ob
│   │   ├── oi_flush.py         check_oi_flush
│   │   ├── htf_lower_high.py   check_htf_lower_high
│   │   ├── funding_extreme.py  check_funding_extreme
│   │   └── whale_inflow.py     check_whale_inflow
│   │
│   └── crash/                  CRASH SHORT signals
│       ├── dead_cat.py         check_dead_cat
│       └── liq_grab_short.py   check_liq_grab_short
│
├── core/
│   ├── regime_detector.py      detect_regime(), get_trend_bias(), get_adx_info()
│   ├── direction_router.py     route_direction()
│   ├── scorer.py               TREND LONG  async score()
│   ├── bear_scorer.py          TREND SHORT async score()
│   ├── range_scorer.py         RANGE LONG  async score()
│   ├── range_short_scorer.py   RANGE SHORT async score()
│   ├── crash_scorer.py         CRASH SHORT async score()
│   ├── filter.py               passes_trend_long_filters()
│   ├── range_filter.py         passes_range_filters()
│   ├── executor.py             execute_signal()
│   └── rr_calculator.py        compute(), position_size()
│
├── data/
│   ├── cache.py                Cache — in-memory store, all market data
│   ├── binance_ws.py           kline + aggTrade WebSocket streams
│   ├── binance_rest.py         historical klines, order placement
│   ├── coinglass.py            OI, funding, liquidations
│   ├── cryptoquant.py          exchange inflow/outflow
│   ├── deribit.py              options IV, skew, P/C ratio
│   └── coinbase_rest.py        spot price for basis calculation
│
├── logging_/
│   ├── schema.sql              SQLite: signals, trades, regimes tables
│   ├── logger.py               TradeLogger (async SQLite writes)
│   └── metrics_api.py          FastAPI: /health /signals/recent /stats/summary
│
├── n8n/
│   └── confluence_workflow.json  webhook → Telegram alert workflow
│
├── ops/
│   ├── confluence.service      systemd unit file
│   └── health_check.py         liveness check script
│
└── tests/
    ├── test_signals.py         pytest unit tests (MockCache, no external deps)
    └── backtest_signals.py     offline replay harness (BacktestCache)
```

---

## Code conventions

All signal functions follow the same signature:

```python
def check_X(symbol: str, cache) -> bool:
    ...
```

All scorers follow:

```python
async def score(symbol: str, cache) -> dict:
    # returns: {symbol, regime, direction, score, signals, fire}
```

Cache reads (synchronous, lock-free):

```python
cache.get_closes(symbol, window, tf)      # list[float]
cache.get_ohlcv(symbol, window, tf)       # list[dict]  {o,h,l,c,v,ts}
cache.get_ohlcv_since(symbol, ts, tf)     # list[dict]
cache.get_cvd(symbol, window, tf)         # list[float]
cache.get_vol_ma(symbol, window, tf)      # float
cache.get_oi(symbol, offset_hours)        # float | None
cache.get_funding_rate(symbol)            # float | None
cache.get_liq_clusters(symbol)            # list[dict]
cache.get_range_high(symbol)              # float | None
cache.get_range_low(symbol)               # float | None
cache.get_basis_history(symbol, n)        # list[float]
cache.get_skew_history(symbol, n)         # list[float]
cache.get_agg_trades(symbol, window_secs) # list[dict]
cache.get_exchange_inflow(symbol)         # float | None
cache.get_inflow_ma(symbol, days)         # float | None
```

---

## Setup

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env
# edit .env with your API keys

python main.py
```

Metrics API (once running):  `http://localhost:8000/health`

---

## Environment variables

| Variable | Purpose |
|---|---|
| `BINANCE_API_KEY` / `BINANCE_SECRET` | Futures trading |
| `COINGLASS_API_KEY` | OI, funding, liquidations |
| `CRYPTOQUANT_API_KEY` | Exchange flow data |
| `DERIBIT_CLIENT_ID` / `DERIBIT_CLIENT_SECRET` | Options data |
| `COINBASE_API_KEY` / `COINBASE_API_SECRET` | Spot price reference |
| `DB_PATH` | SQLite file path (default: `confluence_bot.db`) |

---

## Running tests

```bash
pytest tests/test_signals.py -v
```

Backtest replay (requires historical JSON data):

```bash
python tests/backtest_signals.py BTCUSDT data/btc_1h.json
```
