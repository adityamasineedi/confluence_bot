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

## Active strategies (per routing table in config.yaml)
fvg          — 1H Fair Value Gap fills (TREND + BREAKOUT + CRASH SHORT)
ema_pullback — 15M EMA21 pullback (SUI + SUIUSDT TREND/BREAKOUT only)
vwap_band    — 15M VWAP ±2σ reversion LONG only (LINK + XRP RANGE)
microrange   — 5M tight box SHORT (SUI + DOGE CRASH only)

## Disabled / removed strategies
insidebar, sweep, funding_harvest, bos — confirmed losers, deleted.
VWAP SHORT — WR 0.5% confirmed, removed from all routing.
BTC/ETH ema_pullback — PF 0.28/0.35, removed from routing.

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
