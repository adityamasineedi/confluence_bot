# 📖 confluence_bot — Complete System Bible

A comprehensive reference explaining every component of the bot, what it does,
how it interacts with other parts, and why it exists.

This is the **what is the bot?** document. For strategy state and deferred
changes, see [STRATEGY_PLAYBOOK.md](STRATEGY_PLAYBOOK.md).

---

## Table of Contents

1. [What the bot is](#1-what-the-bot-is)
2. [Big-picture architecture](#2-big-picture-architecture)
3. [The trade lifecycle (end to end)](#3-the-trade-lifecycle-end-to-end)
4. [Component reference](#4-component-reference)
   - [4.1 Entry point: main.py](#41-entry-point--mainpy)
   - [4.2 Configuration: config.yaml](#42-configuration--configyaml)
   - [4.3 Data layer](#43-data-layer)
   - [4.4 Regime detection](#44-regime-detection)
   - [4.5 Strategies (scorers)](#45-strategies--scorers)
   - [4.6 Risk management](#46-risk-management)
   - [4.7 Execution layer](#47-execution-layer)
   - [4.8 Trade monitoring](#48-trade-monitoring)
   - [4.9 Logging and persistence](#49-logging-and-persistence)
   - [4.10 Dashboard / metrics API](#410-dashboard--metrics-api)
   - [4.11 Notifications](#411-notifications)
   - [4.12 Backtest engine](#412-backtest-engine)
   - [4.13 Tools and diagnostics](#413-tools-and-diagnostics)
5. [The 8 hard gates that protect every entry](#5-the-8-hard-gates-that-protect-every-entry)
6. [The 12 fake-breakout filters](#6-the-12-fake-breakout-filters)
7. [How leverage actually works in this bot](#7-how-leverage-actually-works-in-this-bot)
8. [State machines and persistence](#8-state-machines-and-persistence)
9. [What runs every 30 seconds](#9-what-runs-every-30-seconds)
10. [Glossary](#10-glossary)

---

## 1. What the bot is

**confluence_bot** is a multi-strategy crypto futures trading bot for Binance
Futures. It runs 24/7 on a VPS, evaluates 11 cryptocurrencies every 30
seconds, and opens leveraged positions when high-confluence signals fire.

### Core characteristics

| Property | Value |
|---|---|
| **Language** | Python 3.11+ async/await |
| **Exchange** | Binance Futures (with ccxt fallback for Bitget/BingX/Bybit/OKX) |
| **Symbols** | BTC, ETH, SOL, BNB, XRP, LINK, DOGE, SUI, ADA, AVAX, TAO (11 coins) |
| **Strategies** | 6+ active scorers (BR, FVG, Wyckoff Spring, Liq Sweep, EMA Pullback, Microrange) |
| **Position cap** | 5 simultaneous max, $1000 notional each |
| **Risk per trade** | $50 fixed (capped to ~$10 actual loss by notional cap) |
| **Leverage** | 3× ISOLATED |
| **Timeframes** | 1m / 5m / 15m / 1h / 4h / 1d / 1w |
| **Modes** | PAPER (simulation) / DEMO (Binance testnet) / LIVE (real money) |
| **Database** | SQLite (single file, write-ahead-logging) |
| **Dashboard** | FastAPI + HTML/JS, live updates |

### Design philosophy

The bot is built on **confluence** — multiple independent signals must agree
before a trade fires. No single signal is enough. Every entry passes through:

1. **Regime classifier** (what kind of market?)
2. **Strategy router** (which strategies are valid for this regime?)
3. **Per-strategy scorer** (does this specific pattern exist right now?)
4. **8-gate hard filter** (are macro conditions safe?)
5. **Risk manager** (do we have margin? Are we over-exposed?)
6. **Executor** (place the order, attach SL/TP, verify the fill)
7. **Trade monitor** (watch the position until close)

If any layer rejects, the trade doesn't happen. This makes the bot **paranoid
by design** — it would rather miss a trade than take a bad one.

---

## 2. Big-picture architecture

```
┌────────────────────────────────────────────────────────────────────────┐
│                              main.py                                    │
│  Bootstraps everything, starts asyncio tasks, never exits unless killed │
└─────┬────────────────────────────────┬─────────────────────────────────┘
      │                                │
      │                                │
┌─────▼────────┐              ┌────────▼─────────┐
│  Data layer  │              │  Strategy loops  │
│              │              │                  │
│ binance_ws   │ ◄────reads───┤ breakout_retest  │
│ binance_rest │              │ fvg              │
│ cache.py     │              │ wyckoff          │
│ coinbase_rest│              │ liq_sweep        │
│ coinglass    │              │ ema_pullback     │
│ cryptoquant  │              │ microrange       │
│ deribit      │              │ ...              │
└──────────────┘              └────────┬─────────┘
                                       │ score()
                                       │
                              ┌────────▼─────────┐
                              │  scorer returns  │
                              │  {fire: True/    │
                              │   False, sl, tp} │
                              └────────┬─────────┘
                                       │ if fire
                                       │
              ┌────────────────────────▼────────────────────────┐
              │                  filter.py                       │
              │     8 hard gates: weekly, ATR, distribution,     │
              │     ETH cross-confirm, IRB, vol_ratio, etc.      │
              └────────────────────────┬────────────────────────┘
                                       │ if all pass
                                       │
              ┌────────────────────────▼────────────────────────┐
              │              circuit_breaker.py                  │
              │     daily loss cap, consecutive loss halt        │
              └────────────────────────┬────────────────────────┘
                                       │ if not tripped
                                       │
              ┌────────────────────────▼────────────────────────┐
              │                 executor.py                      │
              │     async lock → DB pre-check → exchange         │
              │     position check → rr_calculator → place order │
              └────────────────────────┬────────────────────────┘
                                       │ if order placed
                                       │
              ┌────────────────────────▼────────────────────────┐
              │              binance_rest.py                     │
              │     MARKET entry → 3-tier SL → algo TP →         │
              │     verify position → return                     │
              └────────────────────────┬────────────────────────┘
                                       │ trade is live
                                       │
              ┌────────────────────────▼────────────────────────┐
              │              trade_monitor.py                    │
              │     polls every 30s, manages SL→BE move,         │
              │     detects fills, max-hold timeout, force-close │
              └────────────────────────┬────────────────────────┘
                                       │ on close
                                       │
              ┌────────────────────────▼────────────────────────┐
              │                logger.py                         │
              │     write trade to SQLite, update equity,        │
              │     trigger Telegram alert                       │
              └──────────────────────────────────────────────────┘
```

In parallel, the **dashboard (metrics_api.py)** reads from the same DB and
exposes a web UI on port 8001 (VPS) / 8002 (local).

---

## 3. The trade lifecycle (end to end)

Walk through what happens from "scorer fires" to "trade closed":

### Step 1 — Scorer detects a setup (every 30s per symbol)

Every 30 seconds, the breakout_retest loop evaluates each of 11 symbols:

```python
# core/breakout_retest_loop.py
while True:
    await asyncio.sleep(30)
    for symbol in symbols:
        regime = detect_regime(symbol, cache)         # what kind of market?
        if "breakout_retest" in routing[symbol][regime]:  # is BR allowed?
            score_dicts = await score(symbol, cache)   # check pattern
            for sd in score_dicts:
                if sd["fire"]:
                    await execute_signal(sd, cache)
```

The scorer runs through:
- Range detection (last 8 5m bars form a tight box)
- Boundary touch validation
- ATR regime check
- Volume confirmation
- HTF (1H) trend alignment
- Weekly macro gate
- 4H exhaustion check
- BTC crash cooldown
- Choppy market gate
- Anti-correlation check

If all pass, the scorer enters `AWAITING_BREAKOUT_CONFIRM` state. On the next
bar, if the breakout closes through the level, it transitions to
`AWAITING_RETEST`. When price comes back to the level and confirms the flip
with a strong-body close, the scorer **fires** with a signal dict:

```python
{
    "symbol":    "BTCUSDT",
    "regime":    "BREAKOUT_RETEST",
    "direction": "LONG",
    "score":     1.0,
    "signals":   {...},
    "fire":      True,
    "br_stop":   72400.0,
    "br_tp":     73200.0,
    "br_flip":   72600.0,
}
```

### Step 2 — Executor takes over (`core/executor.py`)

```python
async def execute_signal(score_dict, cache):
    # Layer 1: async lock — claim the deal slot
    async with _deal_lock:
        if (symbol, direction) in _active_deals: return None
        if (symbol, opposite) in _active_deals: return None
        if len(_active_deals) >= max_open: return None
        _pending_deals.add(deal_key)

    # Layer 2: DB-level open trade check
    open_row = SELECT id FROM trades WHERE symbol=? AND status='OPEN'
    if open_row: return None

    # Layer 3: exchange position check
    pos_amt = await get_position_amt(symbol)
    if abs(pos_amt) > 0.0001: return None

    # Layer 4: circuit breaker
    if circuit_breaker.is_tripped(): return None

    # Layer 5: position sizing
    invalidate_committed_cache()  # ensure fresh equity
    qty = position_size(entry, stop, ...)

    # Layer 6: place the order
    order = await place_market_with_bracket(
        symbol, side, qty, stop, take_profit
    )
    if not order: return None

    # Layer 7: log and notify
    logger.log_trade(...)
    telegram.send_trade_open(...)
```

Each layer is a separate veto. **All must pass.** Order: lock → DB → exchange → CB → size → order → log.

### Step 3 — Order placement (`data/binance_rest.py`)

`place_market_with_bracket()` does the actual API calls:

```python
async def place_market_with_bracket(symbol, side, qty, stop, tp):
    # 1. POST market entry
    entry_resp = await session.post('/fapi/v1/order', {
        "symbol": symbol, "side": side, "type": "MARKET", "quantity": qty
    })

    # 2. Wait 1s for exchange to settle
    await asyncio.sleep(1)

    # 3. Verify position is REAL on exchange (catch phantom fills)
    pos_amt = await get_position_amt(symbol)
    if abs(pos_amt) < 0.0001:
        await cancel_all_orders(symbol)
        return {}

    # 4. Place SL — 3-tier fallback
    sl_resp = await session.post('/fapi/v1/algoOrder', {SL params})
    if sl_resp rejected:
        sl_resp = await session.post('/fapi/v1/order', {STOP_MARKET params})
    if sl_resp rejected:
        # ABORT — flatten the position immediately
        await session.post('/fapi/v1/order', {flatten_market})
        send_critical_telegram_alert("NAKED POSITION")
        return {}

    # 5. Place TP via algo order (soft failure OK — software TP covers it)
    tp_resp = await session.post('/fapi/v1/algoOrder', {TP params})

    return {entry, qty, sl_placed: True, tp_placed: ...}
```

### Step 4 — Trade is live; trade_monitor takes over

`core/trade_monitor.py` polls every 30 seconds:

```python
async def monitor_trades(cache):
    while True:
        await asyncio.sleep(30)
        trades = load_open_trades_from_db()
        for trade in trades:
            # 1. Check max-hold timeout (4h for BR)
            if max_hold_exceeded(trade):
                close_at_market(trade)
                send_alert("MAX_HOLD")
                continue

            # 2. In live mode, check breakeven move
            if not paper_mode:
                check_breakeven(trade)
                if regime_conflicts(trade):
                    force_close(trade)
                    continue

            # 3. Check if TP/SL hit
            if paper_mode:
                result = check_paper_order(trade, cache)  # scans 1m bars
            else:
                result = check_live_order(trade, session)  # checks exchange

            # 4. If hit, close and log
            if result:
                outcome, exit_price = result
                close_trade_db(trade, exit_price)
                send_alert(outcome)
```

### Step 5 — Trade closes (TP / SL / max-hold / regime flip)

When a trade closes:
1. DB row updated: `status='FILLED'`, `exit_price`, `pnl_usdt`, `closed_ts`
2. Position slot freed in `_active_deals`
3. Account balance refreshed
4. Cooldown set for the symbol (15 min by default)
5. Telegram alert sent: `TP HIT` / `SL HIT` / `MAX_HOLD` / `REGIME_FLIP`
6. Equity history updated
7. Circuit breaker recomputes daily loss

### Step 6 — Idle until next signal

The position slot is now free. The scorer keeps polling every 30s. New trades
can fire on this symbol after the cooldown expires.

---

## 4. Component reference

### 4.1 Entry point — `main.py`

**Purpose**: Bootstrap the entire bot. Initialize config, cache, logger,
WebSocket streams, scorer loops, trade monitor, and metrics server.

**Key responsibilities**:
- Load `config.yaml`
- Set leverage + margin type on Binance (only if `PAPER_MODE=0`)
- Start WebSocket streams for OHLCV + aggTrades
- Launch one asyncio task per strategy loop
- Launch trade_monitor task
- Launch dashboard server (FastAPI)
- Handle graceful shutdown on SIGTERM/Ctrl+C

**Lines of interest**:
- [main.py:101 `async def main()`](main.py#L101) — the entry point
- [main.py:236 `setup_symbols(...)`](main.py#L236) — sets 3× ISOLATED on Binance
- [main.py:378 `run_breakout_retest_loop`](main.py#L378) — launches BR loop
- [main.py:423 `monitor_trades`](main.py#L423) — launches trade monitor

**Run command**:
```bash
python main.py
```

---

### 4.2 Configuration — `config.yaml`

**Purpose**: Single source of truth for all tunable parameters. 700+ lines
covering risk, strategy params, routing, gates, slippage, fees, regimes, etc.

**Key sections**:

| Section | What it controls |
|---|---|
| `symbols:` | List of 11 coins to trade |
| `risk:` | Position sizing, leverage, margin type, daily caps |
| `regime:` | ADX thresholds for TREND/RANGE/CRASH/PUMP/BREAKOUT |
| `weekly_trend_gate:` | BTC 10W EMA macro filter |
| `breakout_retest:` | All BR strategy parameters |
| `fvg:` | FVG strategy parameters |
| `wyckoff_spring:` | Wyckoff Spring parameters |
| `liq_sweep:` | Liquidation sweep parameters |
| `strategy_routing:` | Which strategies fire on which symbol+regime |
| `backtest:` | Fee/slippage assumptions for backtest |

**Reload behavior**: Most loops re-read config on every iteration (e.g., the
weekly trend gate reloads every 30s), so you can change values without
restarting. Some changes (`leverage`, `max_open_positions`) require restart.

---

### 4.3 Data layer

#### `data/cache.py` — In-memory store

The single source of truth for all market data. Thread-safe via
`threading.Lock` for writes, lock-free reads using snapshot-then-process.

**What it stores per symbol**:
- OHLCV bars (deques with maxlen) for tf in [1m, 5m, 15m, 1h, 4h, 1d, 1w]
- CVD (cumulative volume delta) per timeframe
- Open interest history
- Funding rate (latest value)
- Liquidation events (rolling window)
- Order book L2 snapshot
- Aggregated trades stream
- Long/short ratio
- Account balance (latest)
- BTC dominance
- Range high/low/start_ts (calculated on-the-fly)

**Public API used by scorers**:
```python
cache.get_ohlcv(symbol, window, tf)        # list[dict]
cache.get_closes(symbol, window, tf)        # list[float]
cache.get_last_price(symbol)                # float
cache.get_account_balance()                 # float
cache.get_key_levels(symbol)                # {pdh, pdl, pwh, pwl}
cache.near_key_level(symbol, price, tol)    # bool
cache.get_regime(symbol)                    # current regime string
```

#### `data/binance_ws.py` — WebSocket streams

Connects to Binance Futures WebSocket and subscribes to:
- Kline streams for each symbol × tf (e.g. `btcusdt@kline_5m`)
- AggTrades streams for CVD calculation
- Liquidation streams for stop-hunt detection

Auto-reconnects on disconnect. Rebuilds CVD warmup on every reconnect (20-min warmup).

#### `data/binance_rest.py` — REST API client

Synchronous Binance Futures REST calls (signed and unsigned):
- `_fetch_klines()` — historical OHLCV (used to backfill cache on startup)
- `_fetch_oi()` — open interest snapshot
- `_fetch_funding()` — current funding rate
- `_fetch_account_balance()` — get USDT balance (signed)
- `place_market_with_bracket()` — entry + SL + TP (the main order function)
- `cancel_order()` / `cancel_all_orders()` — cleanup
- `get_position_amt()` — current position size for a symbol
- `setup_symbols()` — set leverage + margin type for all symbols

**Critical detail**: All entries are MARKET orders (not LIMIT). This was an
intentional decision after the Demo testnet ghost-fill investigation —
LIMIT orders on demo sometimes report fills without an actual position.
MARKET makes the fill→position relationship deterministic.

#### `data/exchange_router.py` — Multi-exchange dispatcher

If you switch the active exchange in the dashboard, this routes calls:
- `_use_binance()` returns True → calls `binance_rest.py` directly
- Otherwise → calls ccxt unified API for Bitget/BingX/Bybit/OKX

Same function names, different backends. The bot doesn't care which exchange
is active.

#### `data/coinbase_rest.py` — Spot price (for basis calculation)
#### `data/coinglass.py` — Liquidation cluster data
#### `data/cryptoquant.py` — Exchange flow (whale signals)
#### `data/deribit.py` — Options IV/skew data

These four are external data feeds. They run periodically (every 5-30 min)
and write into the cache. Strategies that use them (Wyckoff, FVG) read from
the cache.

---

### 4.4 Regime detection — `core/regime_detector.py`

**Purpose**: Classify each symbol's current market regime into one of 5 categories.

**The 5 regimes**:

| Regime | Definition | Strategy fit |
|---|---|---|
| **TREND** | 4H ADX > 25 + directional bias | Default — most strategies work |
| **RANGE** | 4H ADX < 20 + tight price band | Mean-reversion strategies (microrange, wyckoff) |
| **CRASH** | EMA50 cross + 7d return < -12% + no recovery | Liq sweep shorts, fade dead cats |
| **PUMP** | EMA50 above + 7d return > +12% + new highs | Liq sweep longs, momentum |
| **BREAKOUT** | ADX rising after a tight range | Breakout retest, FVG continuation |

**Detection order** (first match wins):
1. CRASH (highest priority)
2. PUMP
3. RANGE (if ADX low + tight)
4. BREAKOUT (if ADX rising from tight range)
5. TREND (default)

**Why it matters**: The strategy router uses regime to decide which scorers
are allowed to fire. E.g., `breakout_retest` is allowed on every regime, but
`microrange` only on RANGE. Wyckoff Spring (LONG) only fires when weekly
trend gate allows LONGs.

---

### 4.5 Strategies (scorers)

Each strategy has two files:
- `core/<strategy>_loop.py` — the asyncio loop that polls every N seconds
- `core/<strategy>_scorer.py` — the actual signal logic

#### `core/breakout_retest_scorer.py` — Breakout Retest (your main strategy)

**Pattern**: A tight 5M range forms (8 bars). Price breaks out with volume.
Then comes back to retest the broken level. If retest holds (closes back
through the level), enter in the breakout direction.

**State machine**:
```
IDLE → AWAITING_BREAKOUT_CONFIRM → AWAITING_RETEST → IDLE
                                        │
                                        └─ on retest success → fire signal
```

**State persistence**: Saved to `br_state.json` so restarts don't lose
in-flight setups (added in commit `be67b87`).

**Why it works**: The retest validates that the breakout was real
(institutional buying held the level), filtering out fake breakouts.

**Validated PF**: 2.38 blended over 6,979 backtest trades, 51.2% WR.

#### `core/fvg_scorer.py` — Fair Value Gap

**Pattern**: 3-bar imbalance gap (e.g. bar 1 high < bar 3 low for LONG).
When price returns to fill the gap, enter on the bounce.

**Edge**: Smart-money tries to defend imbalance gaps because they were left
by previous aggressive orders. Filling them tends to be reactive.

#### `core/wyckoff_scorer.py` — Wyckoff Spring (LONG only)

**Pattern**: Price dips below a range support, traps shorts, then snaps back.
The "spring" wick is the entry trigger. SL is below the wick, TP at 2.5R.

**Key feature**: Wick-based SL — natural invalidation. If price comes back
under the spring low, the entire setup is wrong and the trade exits.

#### `core/liq_sweep_scorer.py` — Liquidation Sweep

**Pattern**: Equal highs/lows have stop clusters. Price spikes through to
take the stops, then reverses. Enter on the reversal candle.

**Edge**: Stop clusters are predictable. Smart money runs them on purpose.

#### `core/ema_pullback_scorer.py` — EMA Pullback

**Pattern**: In a strong trend, price pulls back to EMA21. Bounce off EMA21
with a strong body candle = continuation entry.

#### `core/microrange_scorer.py` — Micro Range

**Pattern**: Very tight 5M box (< 0.5%). Mean reversion at the boundaries.

#### `core/cme_gap_scorer.py` — CME Gap (BTC only)

**Pattern**: BTC futures markets close on weekends. When they reopen, the
price often "gaps" from Friday close. Trade the gap fill.

#### Common scorer return format

```python
def score(symbol, cache) -> list[dict]:
    return [{
        "symbol":    "BTCUSDT",
        "regime":    "BREAKOUT_RETEST",  # which strategy fired
        "direction": "LONG" or "SHORT",
        "score":     1.0,                 # 0.0 to 1.0
        "signals":   {feature_name: bool, ...},
        "fire":      True or False,       # actually trade?
        # strategy-specific keys:
        "br_stop":   72400.0,             # for breakout_retest
        "br_tp":     73200.0,
        "br_flip":   72600.0,
    }]
```

---

### 4.6 Risk management

#### `core/rr_calculator.py` — Position sizing

Computes the dollar position size for a given trade:

```python
def position_size(entry, stop, cache, symbol, risk_pct=None, slip_pct=0.0002):
    balance = cache.get_account_balance()         # $5000 (paper) or real
    committed = _committed_risk()                 # already-locked $
    available = balance - committed
    risk_usdt = min(_FIXED_RISK_USDT, ...)        # $50 per config

    # Inflate stop distance by slippage + fees
    fee_pct = 0.0005
    round_trip_adj = 1.0 + (slip_pct + fee_pct) * 2
    effective_stop = abs(entry - stop) * round_trip_adj

    size = risk_usdt / effective_stop             # in base currency units

    # Cap at max position size
    max_size = max_position_size_usdt / entry     # $1000 cap
    size = min(size, max_size)

    return round(size, decimals_for_symbol)
```

**Key insight**: Because of the $1000 notional cap, actual loss per trade
is usually ~$10 (1% of $1000), not the configured $50. The risk amount
binds for tighter stops; the notional cap binds for wider stops.

#### `core/circuit_breaker.py` — Daily loss + streak halt

Halts all new entries if either:
- **Daily PnL loss > 3%** of opening balance, OR
- **Daily PnL loss > $500** USDT, OR
- **Consecutive losing trades >= 6**

Resets at UTC midnight automatically. Can be manually reset from the
dashboard (Circuit Breaker card → Reset button).

**Critical fix in commit `92d29bf`**: The streak counter was bugged — any
$0.01 win would reset the streak. Now it requires PnL >= 10% of risk
amount to count as a "real win".

#### `core/cooldown_store.py` — Per-symbol cooldown

After any trade closes, that symbol enters cooldown (15 min by default).
Prevents immediate re-entry on the same coin.

---

### 4.7 Execution layer — `core/executor.py`

**Purpose**: The single funnel through which all signals must pass to become
trades. Implements 4 layers of duplicate prevention.

**The 4 layers**:

1. **Async lock + in-memory pending set** — fastest gate, prevents two
   concurrent scorers from racing on the same symbol+direction
2. **DB pre-check** — `SELECT id FROM trades WHERE symbol=? AND status='OPEN'`
   catches duplicates across processes
3. **Exchange position check** — `get_position_amt(symbol)` confirms reality
   on Binance, catches state desync between bot and exchange
4. **DB UPDATE guard** — `UPDATE trades SET status=... WHERE status='OPEN'`
   atomic at the SQL level

Plus all the gates in `filter.py` and the circuit breaker.

**Order placement flow**: see Section 3, Steps 2-3.

---

### 4.8 Trade monitoring — `core/trade_monitor.py`

**Purpose**: Watch every open trade, detect TP/SL hits, manage breakeven
moves, force-close timeouts and regime flips.

**Polls every 30 seconds**. For each open trade:

1. **Check max-hold timeout** — if open > 4 hours, close at market
2. **Check breakeven trigger** (live mode only) — if price > entry + 2R,
   move SL to entry + fee buffer
3. **Check regime flip** (live mode only, BR is excluded) — if regime
   reverses, force-close
4. **Check fill status**:
   - Paper mode: scan recent 1m bars for SL/TP touches
   - Live mode: query exchange for open orders + position

**Three exit reasons can be tagged**:
- `TP` — take profit hit, full +RR win
- `SL` — stop loss hit, full -1R loss
- `MAX_HOLD` — 4h timeout, partial PnL based on current price
- `MANUAL` (rare) — closed via dashboard or DB query
- `REGIME_FLIP` — regime detector flipped against the trade (only non-BR strategies)

**Post-close actions**:
- DB update (status, exit_price, pnl_usdt, closed_ts)
- `close_deal()` removes from `_active_deals` set
- Account balance refresh
- Telegram alert
- Equity history update

---

### 4.9 Logging and persistence

#### `logging_/logger.py` — Async SQLite logger

Async wrapper around SQLite. Two main functions:
- `log_signal(score_dict)` — every scorer evaluation (fire or not)
- `log_trade(trade_dict)` — every executed trade (open, close)

Uses `asyncio.to_thread` to avoid blocking the event loop on DB writes.

#### `logging_/schema.sql` — Database schema

```sql
CREATE TABLE signals (
    id INTEGER PRIMARY KEY,
    ts TEXT,            -- ISO-8601 UTC
    symbol TEXT,
    regime TEXT,
    direction TEXT,     -- LONG | SHORT
    score REAL,
    signals TEXT,       -- JSON of feature dict
    fire INTEGER        -- 0 or 1
);

CREATE TABLE trades (
    id INTEGER PRIMARY KEY,
    ts TEXT,            -- open time
    symbol TEXT,
    direction TEXT,
    regime TEXT,        -- which strategy fired this
    entry REAL,
    stop_loss REAL,
    take_profit REAL,
    size REAL,
    order_id TEXT,
    status TEXT,        -- OPEN | FILLED | CANCELLED
    exit_price REAL,
    pnl_usdt REAL,
    closed_ts TEXT
);

CREATE TABLE regimes (
    id INTEGER PRIMARY KEY,
    ts TEXT,
    symbol TEXT,
    regime TEXT
);

CREATE TABLE bot_state (
    key TEXT PRIMARY KEY,
    value TEXT,
    updated TEXT
);

CREATE TABLE cooldowns (
    symbol TEXT,
    strategy TEXT,
    expires_ts REAL,
    PRIMARY KEY (symbol, strategy)
);
```

**File location**: `confluence_bot.db` (production) or `confluence_bot_local.db` (local dev).

---

### 4.10 Dashboard / metrics API — `logging_/metrics_api.py`

**Purpose**: Web UI for monitoring and controlling the bot. FastAPI app on
port 8001 (VPS) or 8002 (local).

**5,800+ lines** — by far the largest file. Includes the full HTML/CSS/JS
inline (no separate templates).

**Key endpoints**:

| Endpoint | What it does |
|---|---|
| `GET /` | Main dashboard HTML |
| `GET /signals/recent` | Last N signals from DB |
| `GET /trades/recent` | Last N trades |
| `GET /trades/open` | Currently open positions |
| `GET /positions/exchange` | Live positions from Binance |
| `GET /signals/live` | Snapshot of every coin's current state |
| `GET /signals/readiness` | How close each coin is to firing |
| `GET /stats/summary` | Total trades, WR, PnL summary |
| `GET /api/circuit-breaker/status` | CB state |
| `POST /api/circuit-breaker/reset` | Manual CB reset |
| `GET /api/gates` | Live status of all gates (BLOCKING/OK/etc.) |
| `GET /api/weekly-gate` | BTC vs 10W EMA |
| `GET /api/risk-mode` | Get fixed/compound risk mode |
| `POST /api/risk-mode` | Toggle risk mode |
| `POST /api/audit/run` | Kick off Phase A audit (background thread) |
| `GET /api/audit/status` | Get audit status + result |
| `POST /api/backtest/run` | Run a backtest from the UI |

**Dashboard tabs**:
- **Signals** — live readiness, recent signals, account balance, circuit breaker status
- **Trades** — open positions, exchange positions, trade history
- **Regimes** — current regime per symbol
- **Market** — live prices, 24h changes, funding rates, ADX, vol
- **Backtest** — run backtests interactively, see results + charts
- **Debug** — diagnostic dump per symbol
- **Strategies** — which strategies are enabled per symbol
- **Gates** — all gates with BLOCKING/OK status
- **Exchanges** — manage API keys for multi-exchange
- **Audit** — Phase A statistical audit (added in commit `76d6d5c`)

---

### 4.11 Notifications — `notifications/`

#### `notifications/telegram.py`

Sends formatted alerts to Telegram for:
- New trade open
- Trade close (with TP/SL/MAX_HOLD outcome)
- Regime change
- Circuit breaker trip
- Critical errors (naked position, force-close failed)
- Daily heartbeat

Uses `BOT_TOKEN` and `CHAT_ID` from environment variables.

#### `notifications/telegram_commands.py`

Listens for Telegram commands like `/status`, `/open`, `/close BTCUSDT`,
`/pause`, `/resume`. Disabled by default (set `TELEGRAM_TOKEN` to enable).

---

### 4.12 Backtest engine — `backtest/`

#### `backtest/engine.py`

**Pure numpy vectorized backtest engine**. No scorer code calls — entirely
re-implemented in fast vectorized form for speed (~30s for 3 years on 1 coin).

**Key functions**:
- `load(symbol)` — load OHLCV from `backtest/data/<symbol>.json`
- `simulate(entry_idx, bars, atr, ...)` — walk forward from each entry, find SL/TP/timeout
- `run_breakout_retest()` — full BR strategy simulation
- `run_fvg()`, `run_wyckoff_spring_v2()`, etc.
- `RUNNERS` dict — map strategy names to runner functions

**Result**: list of `Trade` dataclass objects:
```python
@dataclass
class Trade:
    symbol: str
    strategy: str
    direction: str
    bar_idx: int
    entry: float
    stop: float
    tp: float
    outcome: str   # "TP" | "SL" | "TIMEOUT"
    pnl_r: float   # in R-multiples
    vol_ratio: float
```

#### `backtest/run.py`

CLI runner. Calls the engine and prints results.

```bash
python backtest/run.py --symbol ALL --strategy breakout_retest
python backtest/run.py --symbol BTCUSDT --strategy fvg --show-trades
python backtest/run.py --symbol BTCUSDT --strategy breakout_retest --show-balance --capital 5000 --risk-usdt 50
```

#### `backtest/fetcher.py` and `backtest/download_data.py`

Download historical data from Binance Futures API into `backtest/data/`.
Cached as `.json.gz` per month. The legacy `BTCUSDT.json` monolith is the
old format used by `engine.load()`.

#### `backtest/data_store.py`

Helper to read the compressed monthly cache:
```python
load_bars(symbol, tf, from_ms, to_ms) -> list[dict]
```

Used by tools and the live bot to backfill cache on startup.

#### `backtest/regime_classifier.py`

Pure-function regime classifier (no cache dependency). Mirrors
`core/regime_detector.py` exactly. Used by tools that need to classify
regime at a specific timestamp.

---

### 4.13 Tools and diagnostics — `tools/`

| Tool | What it does | When to use |
|---|---|---|
| `tools/full_audit_phase_a.py` | 5-part statistical audit (also via dashboard) | Monthly health check |
| `tools/reverse_engineer_br.py` | Per-bucket WR/PF/MFE/MAE breakdown | Strategy diagnosis |
| `tools/walk_forward_br.py` | Walk-forward of narrow filter buckets | Filter validation |
| `tools/tp_sweep_br.py` | Sweep `rr_ratio` 2.0 → 3.0 | TP optimization check |
| `tools/vol_filter_wf.py` | Walk-forward of `vol_spike_mult` | Volume threshold check |
| `tools/filter_ablation.py` | 13-run ablation: each filter's impact | Strategy ablation |
| `tools/filter_ablation_walkforward.py` | Walk-forward of full filter ablation | The definitive filter test |

All tools are **read-only** — they don't modify any state. They just read
backtest data and print results.

---

## 5. The 8 hard gates that protect every entry

Defined in `core/filter.py` and called from each scorer's loop. Every
signal must pass ALL 8 to reach the executor.

| Gate | What it checks |
|---|---|
| **Gate 1** — Weekly trend gate | BTC is above 10W EMA for LONGs (or below for SHORTs) |
| **Gate 2** — ATR spike | 1H ATR is not > 3× the average (no flash crashes) |
| **Gate 3** — Distribution | No bearish distribution pattern (smart money exit) |
| **Gate 4** — Volume sanity | Volume isn't dead (no entries during illiquid hours) |
| **Gate 5** — IRB (Inside Range Bar) | Rob Hoffman IRB confirmation if applicable |
| **Gate 6** — ETH cross-confirm (Dow Theory) | ETH agrees with the BTC direction (or is independent) |
| **Gate 7** — Funding rate | Not in extreme funding territory (suggests reversal) |
| **Gate 8** — Open interest | OI direction agrees with the trade direction |

If any gate is BLOCKING, the trade doesn't fire. The dashboard "Gates" tab
shows the live state of every gate.

---

## 6. The 12 fake-breakout filters

These live inside the scorer (`core/breakout_retest_scorer.py`) and check
the breakout setup itself, not macro conditions.

| # | Filter | What it does |
|---|---|---|
| 1 | **Two-bar breakout confirm** | Next bar must close beyond the level too |
| 2 | **Retest requirement** | Must retest the broken level with a strong close |
| 3 | **Retest body ratio ≥ 0.40** | Reject indecision/wick retest candles |
| 4 | **Volume ≥ 1.25× avg** | Real breakouts have volume |
| 5 | **HTF (1H) trend alignment** | Don't fight 1H trend |
| 6 | **Weekly macro gate** | Don't fight macro |
| 7 | **4H exhaustion gate** | Skip if 4H has already moved > 2.5% in direction |
| 8 | **Boundary touch gate** | Reject ranges with > 5 touches on both sides |
| 9 | **Range width filter** | Min 0.1%, max 2% range width |
| 10 | **ATR regime check** | Reject ATR-spike bars |
| 11 | **Choppy market gate** | Skip if 1H ATR > 2× 24H average |
| 12 | **Crash cooldown** | Block LONGs for 4h after BTC drops > 1.5%/1H |

The walk-forward in `tools/filter_ablation_walkforward.py` proves that
filters #1, #3, #7, #8 (and partially #4) are actively losing money on both
in-sample and out-of-sample data. These are the deferred changes in the
playbook.

---

## 7. How leverage actually works in this bot

**Leverage doesn't change the strategy. It only changes margin requirement.**

### Position sizing math

```python
# core/rr_calculator.py
risk_usdt = $50                                    # what you're willing to lose
size = risk_usdt / |entry - stop|                  # base currency units
size = min(size, max_position_size_usdt / entry)   # cap at $1000 notional
```

Example for BTC at $72,000:
- Stop at $71,280 (1% below entry)
- Theoretical size: $50 / $720 = 0.0694 BTC ($5000 notional)
- Capped to: $1000 / $72000 = 0.01388 BTC ($1000 notional)
- **Actual loss if SL hits**: 0.01388 × $720 = **$10**

### What leverage changes

| Leverage | Notional | Margin required | $ loss if SL hits | $ gain if TP hits |
|---|---|---|---|---|
| **1×** | $1000 | $1000 | $10 | $22 |
| **3× (production)** | $1000 | $333 | $10 | $22 |
| **10×** | $1000 | $100 | $10 | $22 |

**Leverage only frees up capital. It does NOT change the price levels, the
SL trigger, or the dollar P&L.**

### Liquidation safety at 3×

Approximate liquidation distance: `100 / leverage - fees ≈ 32.8%` from entry.
Your SL is at 1% from entry. **The SL fires 32× sooner than liquidation could happen.**

### Margin pool with 5 concurrent positions

```
Max simultaneous margin = 5 positions × $333 = $1,667
Free balance buffer    = $5000 - $1667 = $3,333
```

Plenty of headroom. No margin call risk at this configuration.

### Why ISOLATED margin (not CROSS)

ISOLATED walls off each position's margin so one losing position can't
drain your whole account. Worst case per position = $333 margin lost
(but realistically $10 due to SL firing first). CROSS would let one bad
position cascade-liquidate others. ISOLATED is the safe choice.

---

## 8. State machines and persistence

### Scorer state machine (per symbol, in `breakout_retest_scorer.py`)

```
       ┌─────────────────────────────────────────────┐
       │                    IDLE                      │
       │  (looking for a tight range to break)        │
       └────────┬─────────────────────────────────────┘
                │ range detected + breakout candle
                ▼
       ┌──────────────────────────────────────────────┐
       │       AWAITING_BREAKOUT_CONFIRM              │
       │  (waiting for next bar to confirm breakout)  │
       └────────┬─────────────────────────────────────┘
                │ next bar closes beyond level
                ▼
       ┌──────────────────────────────────────────────┐
       │         AWAITING_RETEST                      │
       │  (waiting for price to retest the level)     │
       └────────┬─────────────────────────────────────┘
                │ retest confirmed + body quality OK
                ▼
       ┌──────────────────────────────────────────────┐
       │              FIRE SIGNAL                     │
       │  (execute_signal() is called)                │
       └────────┬─────────────────────────────────────┘
                │
                ▼
       ┌──────────────────────────────────────────────┐
       │                IDLE (reset)                  │
       └──────────────────────────────────────────────┘
```

### Persistence — `br_state.json`

Saves between restarts so AWAITING_RETEST setups don't get lost. Stored in
the project root, gitignored. Format:
```json
{
  "state": {"BTCUSDT": {"state": "AWAITING_RETEST", "direction": "LONG", "flip_level": 72600.0, "bars_waited": 2}},
  "daily_trades": {"BTCUSDT": ["2026-04-11", 3]},
  "recent_entries": [[1744323600.0, "SHORT"], ...]
}
```

Reloaded on import in `breakout_retest_scorer.py`.

### Trade state machine (in DB)

```
NULL → OPEN → FILLED  (TP or SL or MAX_HOLD or REGIME_FLIP or MANUAL)
            ↘ CANCELLED  (entry rejected before fill)
```

Status is the source of truth. Position counts come from `WHERE status='OPEN'`.

### Circuit breaker state

In-memory only. Resets at UTC midnight. Manual reset clears the trip but
remembers the consec count to prevent immediate re-tripping.

---

## 9. What runs every 30 seconds

This is a snapshot of the bot's main loop activity:

```
T+0s:    Breakout retest loop ticks
         For each of 11 symbols:
           - Detect regime
           - Check strategy router
           - Call score() — runs through all 12 filters
           - If fire: execute_signal() → place order

T+0s:    FVG loop ticks (every 60s)
T+0s:    Wyckoff loop ticks (every 60s)
T+0s:    Liq sweep loop ticks (every 60s)
T+0s:    EMA pullback loop ticks (every 30s)
T+0s:    CME gap loop ticks (every 5 min, BTC only)

T+0s:    Trade monitor ticks
         For each open trade:
           - Check max-hold timeout
           - Check breakeven
           - Check regime flip
           - Check TP/SL hit (paper or live)

T+5s:    Account balance refresh (REST poll)
T+10s:   OI + funding refresh (every minute, staggered)
T+30s:   Dashboard auto-refresh on browsers
T+60s:   Health check / heartbeat
T+300s:  Full reconciliation (compare DB to exchange positions)
T+300s:  Coinglass refresh
T+300s:  Cryptoquant refresh
T+86400s (daily): DB pruning, daily heartbeat to Telegram
```

In parallel:
- WebSocket streams continuously update `cache.py` with new bars
- Aggregated trades update CVD per symbol
- Liquidation events update the liq event ring buffer

CPU usage at idle: ~10%. Spikes to 30-50% during scorer evaluation. RAM: ~150 MB.

---

## 10. Glossary

| Term | Meaning |
|---|---|
| **ADX** | Average Directional Index — measures trend strength (0-100) |
| **ATR** | Average True Range — measures volatility |
| **Backtest** | Simulating the strategy on historical data to estimate performance |
| **BE (breakeven)** | SL moved to entry price after price runs in favor by 2R |
| **BR** | breakout_retest strategy |
| **Cohen's d** | Effect size — how much winners differ from losers (>0.5 = meaningful) |
| **CVD** | Cumulative Volume Delta — buying vs selling pressure |
| **Drawdown** | Peak-to-trough decline in equity |
| **EMA** | Exponential Moving Average |
| **Expectancy** | Expected $ return per trade (positive = profitable strategy) |
| **FVG** | Fair Value Gap — 3-bar imbalance |
| **HTF** | Higher Timeframe (e.g. 1H trend for a 5M setup) |
| **Liq sweep** | Liquidation sweep — price spike to take stop clusters |
| **MAE** | Maximum Adverse Excursion — worst PnL the trade saw |
| **MFE** | Maximum Favorable Excursion — best PnL the trade saw |
| **OI** | Open Interest — total open futures contracts |
| **OOS** | Out-of-sample (data the strategy hasn't been fit to) |
| **PF** | Profit Factor = gross wins / gross losses |
| **PnL** | Profit and Loss |
| **R / R-multiple** | Risk unit; +2R = won 2× the risk amount |
| **Regime** | Market type: TREND, RANGE, CRASH, PUMP, BREAKOUT |
| **RR (risk/reward)** | TP distance / SL distance ratio |
| **Scorer** | The function that decides if a strategy should fire |
| **SL** | Stop Loss |
| **TP** | Take Profit |
| **Walk-forward** | Validating an idea on data not used to fit it |
| **WR** | Win Rate (% of trades that hit TP) |
| **Wyckoff Spring** | False breakdown that traps shorts then reverses |

---

## 11. Quick reference — files you'll touch most often

| File | When you'd edit it |
|---|---|
| `config.yaml` | Tuning any strategy parameter |
| `core/breakout_retest_scorer.py` | Modifying the BR strategy logic |
| `core/circuit_breaker.py` | Adjusting halt thresholds |
| `core/trade_monitor.py` | Changing trade management (BE, max-hold, force-close) |
| `core/executor.py` | Order placement and duplicate prevention |
| `data/binance_rest.py` | Exchange API details (rarely) |
| `STRATEGY_PLAYBOOK.md` | Deferred changes, validation procedures |
| `BOT_BIBLE.md` | This file — system documentation |

## 12. Files you should NEVER touch unless you know exactly why

| File | Reason |
|---|---|
| `confluence_bot.db` | Live trade DB — corruption = data loss |
| `br_state.json` | Scorer state — modifying breaks in-flight setups |
| `data/cache.py` | Thread safety is delicate; bugs cause cascade |
| `backtest/engine.py` | Vectorized math; one error invalidates all results |
| `logging_/schema.sql` | Schema migration is unimplemented; changes break DB |

---

## 13. Where each rule lives

Quick lookup for "where is the code that controls X":

| Rule | Location |
|---|---|
| Max 5 concurrent positions | `config.yaml` → `risk.max_open_positions` |
| Max 3 same-direction positions | `config.yaml` → `risk.max_same_direction_positions` |
| 3× leverage on Binance | `config.yaml` → `risk.leverage` (set on startup by `setup_symbols`) |
| ISOLATED margin | `config.yaml` → `risk.margin_type` |
| $50 risk per trade | `config.yaml` → `risk.fixed_risk_usdt` |
| $1000 max notional | `config.yaml` → `risk.max_position_size_usdt` |
| 3% daily loss limit | `config.yaml` → `risk.max_daily_loss_pct` |
| 6 consecutive losses limit | `config.yaml` → `risk.max_consecutive_losses` |
| 4h max hold for BR | `config.yaml` → `breakout_retest.max_hold_hours` |
| 15 min cooldown after a trade | `config.yaml` → `breakout_retest.cooldown_mins` |
| 6 trades/day cap per symbol | `config.yaml` → `breakout_retest.max_trades_per_day` |
| 2 entries per 30min same direction | `config.yaml` → `breakout_retest.max_entries_per_30min` |
| 1H crash cooldown of 1.5% blocks LONGs | `config.yaml` → `breakout_retest.crash_cooldown_pct` |
| 0.40 retest body ratio | `config.yaml` → `breakout_retest.min_retest_body_ratio` |
| Two-bar breakout confirm | `config.yaml` → `breakout_retest.require_breakout_confirm` |
| Skip 14:00-15:00 UTC | `core/breakout_retest_scorer.py` → `_SKIP_HOUR_S/E` constants |
| BTC 10W EMA macro gate | `core/weekly_trend_gate.py` |
| Don't enable BE for BR | `config.yaml` → `risk.breakeven_disabled_strategies` |
| Don't apply regime flip to BR | `config.yaml` → `risk.regime_flip_disabled_strategies` |
| Dashboard URL | VPS: `http://165.22.57.158:8001` / Local: `http://localhost:8002` |
| DB path (production) | `confluence_bot.db` |
| DB path (local dev) | `confluence_bot_local.db` |

---

## 14. The big picture in one sentence

> **The bot watches 11 crypto markets every 30 seconds, runs each through 6+
> independent strategies, applies 8 hard gates and 12 strategy-specific
> filters, sizes positions for fixed-dollar risk on 3× ISOLATED leverage,
> places guaranteed-fill MARKET orders with 3-tier SL fallback, monitors
> every position to TP/SL/4h-timeout, logs everything to SQLite, and
> exposes a live dashboard — and all of it is validated by a 6,979-trade
> backtest showing PF 2.38 / WR 51.2% over 3 years.**

That's the bot. Everything else in this document is detail.

---

## See also

- [STRATEGY_PLAYBOOK.md](STRATEGY_PLAYBOOK.md) — strategy state, deferred changes, deployment commands
- [CLAUDE.md](CLAUDE.md) — project rules and constraints
- [README.md](README.md) — quickstart (if it exists)
