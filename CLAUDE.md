# confluence_bot — CLAUDE.md

## Project
Multi-regime crypto futures trading bot.
Language: Python 3.11, async/await throughout.
Exchange: Binance Futures (ISOLATED margin, 3× leverage).
Coins: BTCUSDT, ETHUSDT, SOLUSDT, BNBUSDT, XRPUSDT,
       LINKUSDT, DOGEUSDT, SUIUSDT

## Regimes (5)
TREND · RANGE · CRASH · PUMP · BREAKOUT
Detected by core/regime_detector.py using 4H ADX + weekly return.
Weekly gate (core/weekly_trend_gate.py) blocks LONGs below 10W EMA
and SHORTs above 10W EMA — macro regime filter.

## Active strategies (confirmed by real backtest PF ≥ 1.50)

### Live scorers working NOW:
fvg              — core/fvg_scorer.py       — all regimes
ema_pullback     — core/ema_pullback_scorer.py — includes SHORT
microrange       — core/microrange_scorer.py — 5M tight box
wyckoff_spring   — core/wyckoff_scorer.py   — LONG, wick SL
liq_sweep        — core/liq_sweep_scorer.py — LONG + SHORT
breakout_retest  — core/breakout_retest_scorer.py — all 8 coins

### Scorers to build (stubs exist, implement in order):
cme_gap          — core/cme_gap_scorer.py (stub)         — BTC only, 135 trades/3yr, PF 2.74  ← BUILD NEXT
wyckoff_upthrust — core/wyckoff_upthrust_scorer.py (stub) — 7 coins in bear, PF 1.87-9.99
ema_pullback_short_v2 — core/ema_pullback_short_v2_scorer.py (stub) — XRP 233 trades/3yr, PF 1.58

### Confirmed backtest results per coin:
BTCUSDT:  cme_gap(PF2.74) liq_sweep(2.46) spring_v2(3.83) fvg(2.50)
ETHUSDT:  spring_v2(2.50) fvg(1.63) liq_sweep(1.50)
SOLUSDT:  ema_pb_short(1.74) fvg(3.39) spring_v2(1.50)
BNBUSDT:  spring_v2(5.33) liq_sweep(2.81) liq_sw_short(2.22) micro(1.75)
XRPUSDT:  ema_pb_short_v2(1.58) liq_sw_short(2.22) upthrust(1.87)
LINKUSDT: liq_sweep(3.74) upthrust(3.74) spring_v2(2.50) liq_sw_short(1.75)
DOGEUSDT: liq_sweep(7.49) liq_sw_short(2.56) upthrust(9.99)
SUIUSDT:  liq_sw_short(2.44) upthrust(9.99) micro(1.54)

### Disabled / removed strategies (confirmed losers):
vwap_band   — PF 0.60-0.88 across all coins, all periods — disabled
ema_pullback LONG on BTC/ETH — PF 0.28/0.35 — REMOVED
wyckoff_range — PF 0.81-0.93 on most coins — REMOVED
insidebar — WR 33.7%, account wipe — FILES DELETED
funding_harvest — WR 9.1%, 11 trades in 6m — FILES DELETED
call_skew_roc — confirmed -33% edge — FILE DELETED
time_distribution — confirmed -33% edge — FILE DELETED
sweep, bos — deleted
leadlag — disabled (live trade count unconfirmed)
zone — disabled (too few backtest trades)
oi_spike — disabled (not validated via backtest)
session_trap — disabled (not validated via backtest)

### Known bugs fixed:
1. FVG _touched TTL — _touched set grew forever blocking valid re-entries; now expires after 7 days
2. Liq sweep entry drift gate — late entries destroyed edge; now rejects if price moved > 0.3% from sweep bar close
3. Regime string standardisation — LIQSWEEP → liq_sweep; mismatched executor preset levels/min RR

### Build order for remaining scorers:
1. cme_gap_scorer.py          ← $1,739/yr impact (stub exists)
2. wyckoff_upthrust_scorer.py ← $940/yr, 7 coins (stub exists)
3. ema_pullback_short_v2      ← $2,538/yr, XRP+SOL (stub exists)

## Strict code rules
- Signal functions: def check_X(symbol: str, cache) -> bool
- Scorer output: dict with keys:
    symbol, regime, direction, score, signals(dict), fire(bool)
    plus strategy-specific: fvg_stop/fvg_tp, ep_stop/ep_tp etc.
- Cache API: cache.get_ohlcv(symbol, window, tf) → list[dict]
             cache.get_closes(symbol, window, tf) → list[float]
             cache.get_key_levels(symbol) → {pdh,pdl,pwh,pwl}
             cache.near_key_level(symbol, price, tol) → bool
- All config from config.yaml via yaml.safe_load
- API keys always from os.environ — never in source code
- No pandas. No TA-Lib. Pure Python + numpy only.
- Handle None and empty list gracefully — return False, never raise

## Architecture
signals/
  trend/    cvd.py fvg.py vpvr.py htf_structure.py liquidity.py
            oi_funding.py order_block.py whale_flow.py
            rsi_divergence.py ema_cross.py long_short_ratio.py
            bb_squeeze.py ema_pullback_15m.py irb.py
            distribution.py
  range/    absorption.py wyckoff_spring.py upthrust.py
            ask_absorption.py perp_basis.py anchored_vwap.py
            vwap_bands.py rsi_oversold.py
  bear/     cvd_bearish.py funding_ramp.py
  crash/    dead_cat.py liq_grab_short.py
  volume_momentum.py
  microrange/detector.py

core/
  regime_detector.py       — 5-regime detection with hysteresis
  weekly_trend_gate.py     — BTC 10W EMA macro gate
  direction_router.py      — LONG/SHORT/NONE per regime
  strategy_router.py       — symbol × regime → active strategies
  filter.py                — 8-gate hard filter (ETH cross-confirm,
                             distribution, ATR spike, etc.)
  fvg_scorer.py            — FVG entries with IRB + key level boost
  ema_pullback_scorer.py   — EMA21 pullback with IRB + key level boost
  vwap_band_scorer.py      — VWAP ±2σ reversion LONG only
  microrange_scorer.py     — 5M tight box mean-reversion
  wyckoff_scorer.py        — Wyckoff spring LONG, wick-based SL
  liq_sweep_scorer.py      — equal highs/lows stop hunt, LONG + SHORT
  vol_ratio.py             — shared 6H/48H vol ratio gate for scorers
  executor.py              — bracket orders, dynamic slippage
  rr_calculator.py         — position size with committed risk
  circuit_breaker.py       — daily loss + streak halt
  trade_monitor.py         — TP/SL detection, breakeven move
  cooldown_store.py        — per-symbol cooldown tracking
  symbol_config.py         — per-coin tier + dynamic params

data/
  cache.py          — in-memory store, get_key_levels() for PDH/PDL
  binance_ws.py     — klines + aggTrades WebSocket
  binance_rest.py   — OI, funding, history, order placement
  coinglass.py      — liquidation data
  cryptoquant.py    — exchange flow (whale signals)
  deribit.py        — options IV/skew
  coinbase_rest.py  — spot price for basis

backtest/
  fetch.py    — download Binance historical OHLCV
  engine.py   — vectorized numpy backtest (no scorer calls)
  run.py      — CLI: python backtest/run.py --symbol BNBUSDT --strategy fvg

logging_/
  logger.py      — async SQLite trade + signal logging
  schema.sql     — DB schema
  metrics_api.py — FastAPI dashboard (port 8001)

## Risk management (config.yaml)
max_risk_usdt: 5              — raise to 1% of account for live
max_open_positions: 5
max_same_direction_positions: 2
leverage: 3 ISOLATED
rr_ratio: 2.5
max_daily_loss_pct: 3.0
max_consecutive_losses: 6
post_trade_cooldown_mins: 30
atr_spike_gate_enabled: true   — blocks entries during flash crashes
weekly_trend_gate.enabled: true — blocks LONGs in macro bear

## Key institutional signals implemented
- PDH/PDL/PWH/PWL via cache.get_key_levels() + near_key_level()
- Rob Hoffman IRB via signals/trend/irb.py
- Distribution detection via signals/trend/distribution.py
- ETH cross-confirmation (Dow Theory) in filter.py Gate 7/6
- CVD divergence (48×1H slope) in signals/trend/cvd.py
- VPVR POC reclaim in signals/trend/vpvr.py (weight 0.15)

## Backtest pass criteria
PF ≥ 1.50, WR ≥ 38%, trades ≥ 20
Below these thresholds: remove from routing, never trade live.
