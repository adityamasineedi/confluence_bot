# Confluence Bot — Complete Trading System Guide

## Overview

Multi-regime crypto futures trading bot running on Binance Futures (ISOLATED margin, 3x leverage).
8 coins: BTCUSDT, ETHUSDT, SOLUSDT, BNBUSDT, XRPUSDT, LINKUSDT, DOGEUSDT, SUIUSDT.

The bot detects market conditions (regimes), selects the right strategies for each regime,
evaluates confluence signals, and executes bracket orders with SL/TP automatically.

---

## How It Works — End to End

```
Every 60 seconds, for each symbol:

  1. DETECT REGIME        What kind of market is this?
         |                (TREND / RANGE / CRASH / PUMP / BREAKOUT)
         v
  2. SELECT STRATEGIES    Which strategies are active for this regime + symbol?
         |                (from config.yaml routing table)
         v
  3. EVALUATE SIGNALS     Does any strategy fire? (score >= threshold)
         |                Each scorer checks 4-6 signals, applies hard gates
         v
  4. FILTER GATES         Pass 8 independent safety gates?
         |                (DI edge, funding, volume, ETH confirm, etc.)
         v
  5. RISK CHECK           Circuit breaker OK? Cooldown clear? Slots available?
         |                (max 5 positions, max 3 same direction)
         v
  6. SIZE + EXECUTE       Calculate position size, place bracket order
         |                (SL + TP placed atomically)
         v
  7. MONITOR              Watch for TP/SL hit, move to breakeven at +2R
                          Force-close if regime flips (LONG in CRASH, SHORT in PUMP)
```

---

## 1. Regime Detection

The regime detector classifies the current market into one of 5 states.
Uses 4H candles (ADX, +DI, -DI) and 1D candles (EMA50, 7-day price change).

| Regime | Conditions | Priority |
|--------|-----------|----------|
| **CRASH** | Price < EMA50(1D) AND 7-day drop > 12% AND making new lows | Highest |
| **PUMP** | Price > EMA50(1D) AND 7-day gain > 12% AND making new highs | 2nd |
| **RANGE** | ADX(4H) < 20 for 3 consecutive readings, 20-bar range < 12% | 3rd |
| **BREAKOUT** | First 3 bars after exiting RANGE, price outside range by > 1% | 4th |
| **TREND** | Default when none of the above apply | Fallback |

**Hysteresis** prevents rapid flipping:
- Enter RANGE when ADX < 20 (all 3 recent)
- Exit RANGE only when 2 of 3 recent ADX > 25
- Minimum 12 bars (48 hours on 4H) before switching regimes

---

## 2. Strategy Routing Table

Each symbol has a specific set of strategies activated per regime.
The routing table is in `config.yaml` under `strategy_routing:`.

| Symbol | TREND | RANGE | BREAKOUT | CRASH | PUMP |
|--------|-------|-------|----------|-------|------|
| **BTCUSDT** | fvg, liq_sweep, spring, cme_gap, br | spring, cme_gap, br | fvg, cme_gap, br | fvg, liq_sweep, upthrust, cme_gap, br | cme_gap |
| **ETHUSDT** | fvg, liq_sweep, spring, br | br | fvg, spring, br | br | fvg, br |
| **SOLUSDT** | fvg, ema_pb_short, spring, br | br | fvg, ema_pb_short, br | ema_pb_short, upthrust, br | fvg, br |
| **BNBUSDT** | spring, liq_sweep, br | micro, br | liq_sweep, spring, br | liq_sw_short, upthrust, micro, br | liq_sweep, br |
| **XRPUSDT** | br | br | br | br | — |
| **LINKUSDT** | ema_pb_short, br | br | br | ema_pb_short, br | — |
| **DOGEUSDT** | liq_sweep, ema_pb_short, br | br | liq_sweep, br | liq_sw_short, upthrust, ema_pb_short, br | liq_sweep, br |
| **SUIUSDT** | spring, ema_pb_short_v2, br | br | spring, br | liq_sw_short, upthrust, micro, br | br |

*br = breakout_retest, spring = wyckoff_spring_v2, micro = microrange, upthrust = wyckoff_upthrust_v2*

**Key insight:** `breakout_retest` is the universal backstop — present on almost every symbol in every regime. Other strategies layer on top for additional edge.

---

## 3. The 6 Live Strategies

### 3.1 Breakout Retest (breakout_retest)

**What it does:** Detects a tight 5M consolidation range, waits for a breakout with volume, then enters on the retest of the broken level (resistance becomes support, or vice versa).

**How it works — 3-phase state machine:**
1. **Range Detection:** 8 bars forming a box, width 0.18%-0.80%, low ATR
2. **Breakout:** Close beyond range boundary + volume >= 1.25x average + 4H EMA20 confirms direction
3. **Retest:** Price pulls back to the flip level, touches it, and closes on the right side. Entry fires here.

**Entry:** At the flip level (the broken range boundary)
**Stop:** ATR(14) x 1.3 below/above entry (minimum 0.5%)
**Target:** 1.5R (TP1 default), also available as 2.2R and 3.0R variants
**Cooldown:** 15 minutes per symbol
**Daily cap:** Max 4 trades per symbol per day

**Special gates:**
- Skips 14:00-15:00 UTC (CME settlement noise)
- 4H exhaustion check: rejects if price already moved > 2.5% in signal direction
- Weekly trend gate: no LONGs below BTC 10W EMA, no SHORTs above

**Backtest:** All 8 coins, PF 3.0+, WR 67-68%, 24,313 trades over 3 years

---

### 3.2 Fair Value Gap (fvg)

**What it does:** Enters into unfilled Fair Value Gaps on the 1H chart — price inefficiencies left by impulsive moves.

**Entry conditions (score >= 0.67 to fire):**

| Signal | Weight | Description |
|--------|--------|-------------|
| fvg_detected | 0.30 | Unfilled bullish/bearish gap on 1H |
| htf_aligned | 0.25 | 4H close vs EMA21 confirms direction |
| rsi_confirm | 0.20 | RSI(14) <= 45 (LONG) or >= 55 (SHORT) |
| vol_not_dist | 0.10 | No distribution/accumulation divergence |
| irb_confirm | 0.10 | 2-bar pullback reversal pattern |
| vol_confirm | 0.05 | Volume spike present |
| +key_level bonus | 0.15 | Near PDH/PDL/PWH/PWL |

**Stop:** Below gap low (LONG) or above gap high (SHORT) with 0.2% buffer
**Target:** 2.0R
**Cooldown:** 45 minutes

---

### 3.3 Liquidity Sweep (liq_sweep)

**What it does:** Enters after equal highs/lows are swept (stop hunt) — smart money sweeps retail stops, then reverses.

**How detection works:**
1. Find two swing lows within 0.2% tolerance, separated by 10+ bars (equal lows)
2. Current bar wicks below that level (the sweep)
3. Current bar closes back above (rejection)
4. Volume >= 1.5x the 20-bar average

**Score components:** sweep (0.40) + HTF aligned (0.35) + RSI ok (0.25) = fire at >= 0.75
**Stop:** Below the sweep wick (LONG) with 0.1% buffer
**Target:** 2.5R
**Cooldown:** 60 minutes per direction independently

**Special gate:** Entry drift — rejects if price has moved > 0.3% from the sweep bar close (late entry destroys edge).

---

### 3.4 Wyckoff Spring (wyckoff_spring)

**What it does:** LONG-only entries on Wyckoff spring patterns — a false breakdown below support that quickly reverses (institutions absorbing supply).

**Entry conditions (all must pass):**
- Spring signal detected on 15M
- 4H close > EMA21 (HTF bullish)
- RSI(1H) <= 60 (not overbought)
- Weekly gate allows longs
- ATR spike gate clear

**Score:** Always 0.85-1.00 when all gates pass (0.40 spring + 0.35 HTF + 0.10-0.25 RSI)
**Stop:** Below the spring wick low with 0.1% buffer (minimum 0.2%)
**Target:** 2.5R
**Cooldown:** 60 minutes

---

### 3.5 EMA Pullback (ema_pullback)

**What it does:** Trend-continuation entries when price pulls back to the 15M EMA21 and bounces — the classic "buy the dip in an uptrend" pattern.

**Entry conditions (score >= 0.75 to fire):**

| Signal | Weight | Description |
|--------|--------|-------------|
| htf_aligned | 0.25 | 4H macro direction matches |
| ema_structure | 0.25 | EMA21 > EMA50 (LONG) or < (SHORT) |
| pullback_touch | 0.25 | Price touched EMA21 (always true when scorer runs) |
| irb_confirm | 0.25 | Reversal bar pattern |
| +key_level bonus | 0.25 | Near key level |

**Hard gates (must all pass for fire):**
- Bounce bar must be bullish with body >= 0.2% and close >= 0.2% beyond EMA21
- Bounce bar volume > prior bar volume

**Stop/Target:** Tier-aware, computed by `get_ema15m_long/short_levels()`
**Cooldown:** 45 minutes

---

### 3.6 Microrange (microrange)

**What it does:** Mean-reversion entries at the boundaries of a tight 5M consolidation box — buy at the floor, sell at the ceiling.

**Entry conditions (score >= 0.75 to fire):**

| Signal | Weight | Description |
|--------|--------|-------------|
| box_detected | 0.25 | Tight range confirmed (width <= 0.5%) |
| entry_zone | 0.25 | Price within 0.1% of boundary |
| volume_ok | 0.25 | No volume spike (breakout risk) |
| rsi_aligned | 0.25 | RSI <= 40 (LONG) or >= 60 (SHORT) |

**Stop:** 0.2% beyond the range boundary
**Target:** 75% of range width
**Cooldown:** 20 minutes

**Drawdown protection (in microrange_loop):**
- At 15% drawdown from peak: risk halved (0.5% instead of 1%)
- At 20% drawdown: loop pauses entirely

---

## 4. Safety Gates (8-Gate Hard Filter)

Every trade must pass these independent gates before execution.
These are checked inside each scorer and by the filter module.

### TREND LONG — 8 gates:
1. BTC 4H close above EMA(200)
2. Symbol 4H +DI exceeds -DI by >= 5
3. ADX not declining (not at 3-bar low)
4. BTC not parabolic (price <= EMA200 x 1.15)
5. Funding rate < 0.03% per 8h
6. 24H volume above minimum threshold
7. ETH 4H close above EMA(21) — Dow Theory cross-confirmation
8. No distribution pattern (CVD divergence check)

### TREND SHORT — 6 gates:
1. BTC 4H close below EMA(200)
2. Symbol 4H -DI exceeds +DI by >= 5
3. ADX not declining
4. Funding not in extreme panic
5. 24H volume above minimum
6. ETH 4H close below EMA(21)

### Additional gates applied per-scorer:
- **ATR Spike Gate:** Blocks entry when current ATR > 3x average (flash crash protection)
- **Weekly Trend Gate:** BTC 10W EMA — no LONGs below, no SHORTs above
- **Vol Ratio Gate:** Recent 6H volatility vs 48H baseline (optional, disabled by default)

---

## 5. Risk Management

### Position Sizing

Two modes available (toggled via `config.yaml` `risk.fixed_risk_mode`):

| Mode | How it works | When to use |
|------|-------------|-------------|
| **Fixed** | Risk exactly $X per trade (e.g., $50) | Early stages, drawdown protection |
| **Compound** | Risk 1% of current equity per trade | After consistent profitability proven |

**Sizing formula:**
```
available_equity = balance - committed_risk_from_open_trades
risk_usdt = available_equity x risk_pct   (or fixed_risk_usdt)
position_size = risk_usdt / (|entry - stop| x round_trip_adj)
round_trip_adj = 1 + (slippage + taker_fee) x 2
```

### Position Limits
- Max 5 open positions total
- Max 3 in the same direction (LONG or SHORT)
- Race condition protected: asyncio.Lock prevents duplicate entries

### Circuit Breaker
Halts all trading when any condition triggers:
- Daily loss > 3% of balance
- Daily loss > $250
- 6 consecutive losing trades (breakeven/scratch counts as loss)
- Auto-resets at UTC midnight

### Cooldowns
- Per-strategy cooldown after each trade (15-60 minutes depending on strategy)
- Post-trade cooldown per symbol (10 minutes)
- Prevents whipsaw re-entry after stops

### Trade Monitoring
Runs every 30 seconds checking all open positions:
- **Breakeven move:** At +2R profit, SL moves to entry + 0.1% (covers fees)
- **Regime flip protection:** Force-closes LONG if regime switches to CRASH, SHORT if PUMP
- **Software SL/TP:** If exchange bracket orders disappear, monitors price and market-closes at levels
- **Stale price guard:** Falls back to REST API if WebSocket data > 2 minutes old

---

## 6. Trading Costs (Accounted in Backtest)

| Cost | Rate | How applied |
|------|------|-------------|
| Taker fee | 0.05% per side | Deducted from PnL as round-trip 0.10% |
| Slippage | 0.02% per side | Entry price worsened, round-trip 0.04% |
| Funding | 0.01% per 8h | Deducted per bar held (proportional to hold time) |
| **Total per trade** | **~0.15%** | Fees + slippage + funding for typical 12h hold |

---

## 7. Data Sources

| Source | Data | Update frequency |
|--------|------|-----------------|
| Binance Futures WS | 1m/5m/15m/1h/4h klines, aggTrades | Real-time |
| Binance REST | OI, funding rates, weekly/daily bars | Every 30-300s |
| Coinglass | Liquidation data | Every 5 min (optional) |
| CryptoQuant | Exchange flow (whale signals) | Every 30s |
| Deribit | Options IV/skew (BTC+ETH) | Every 5 min |
| Coinbase | Spot price for basis calculation | Every 30s |
| CoinGecko | BTC dominance | Every 30 min |

---

## 8. Dashboard (port 8001)

| Tab | Shows |
|-----|-------|
| **Overview** | Account balance, open positions, regime per coin, recent signals |
| **Trades** | All logged trades with PnL, strategy breakdown |
| **Backtest** | Run historical backtests: auto-regime, per-strategy, compound vs fixed |
| **Debug** | Per-symbol live state: ADX, DI, EMA, filter gates, recent signals |
| **Gates** | All trade-blocking gates with live status, risk mode toggle |
| **Strategies** | Complete strategy reference with routing table |

---

## 9. Bot Startup Sequence

1. Load config, initialize data cache
2. Connect to Binance: set leverage (3x ISOLATED) on all symbols
3. Start WebSocket streams (klines + aggTrades)
4. Start REST pollers (OI, funding, whale flow, options)
5. Restore any open trades from database
6. Start strategy loops (breakout_retest, microrange, etc.)
7. Start trade monitor (SL/TP/breakeven watcher)
8. Start metrics dashboard (FastAPI on port 8001)
9. Wait for cache warmup (50x 1H bars + 32x 5M bars per symbol)
10. Begin main evaluation loop (60s per full cycle)

---

## 10. Key Config Parameters

```yaml
risk:
  risk_per_trade: 0.01          # 1% of equity (compound mode)
  fixed_risk_mode: true         # true = flat dollar risk
  fixed_risk_usdt: 50           # $50 per trade (fixed mode)
  max_open_positions: 5
  max_same_direction_positions: 3
  leverage: 3
  rr_ratio: 2.5
  max_daily_loss_pct: 3.0
  max_consecutive_losses: 6
  post_trade_cooldown_mins: 10
  breakeven_trigger_r: 2.0

weekly_trend_gate:
  enabled: true
  ema_period: 10                # 10-week EMA of BTC

backtest:
  taker_fee_pct: 0.0005         # 0.05% per side
  slippage_pct: 0.0002          # 0.02% per side
  funding_cost_per_8h: 0.0001   # 0.01% per 8h
```

---

## 11. Backtest Pass Criteria

A strategy must meet ALL of these to stay in the routing table:
- **Profit Factor >= 1.50**
- **Win Rate >= 38%**
- **Minimum 20 trades** in the test period

Below these thresholds: removed from routing, never traded live.

---

## 12. Disabled / Removed Strategies

These were tested and confirmed losers:

| Strategy | Result | Action |
|----------|--------|--------|
| vwap_band SHORT | PF 0.01, WR 0.5% | SHORT disabled (long_only: true) |
| ema_pullback LONG (BTC/ETH) | PF 0.28-0.35 | Removed from routing |
| wyckoff_range | PF 0.81-0.93 | Removed |
| insidebar | WR 33.7%, account wipe | Files deleted |
| funding_harvest | WR 9.1%, 11 trades in 6m | Files deleted |
| sweep, bos | Poor edge | Deleted |
| leadlag, zone, oi_spike, session_trap | Not validated | Disabled |
