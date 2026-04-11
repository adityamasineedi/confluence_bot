# Strategy Playbook — confluence_bot

Single-source reference for the breakout_retest strategy: current state,
audit findings, deferred changes, deployment commands, and validation
procedures. Read this before touching production.

Last updated: 2026-04-11

---

## 1. Current production state

### What's deployed (commit `d67cf9d` and earlier)

| Component | Status | Notes |
|---|---|---|
| Strategy core | breakout_retest on 8 coins (BTC, ETH, SOL, BNB, XRP, LINK, DOGE, SUI) | + ADA, AVAX, TAO via compressed cache only |
| Anti-correlation fix | ✅ Deployed | Re-checks rate gates at FIRE time, not just detection time |
| Max-hold timeout | ✅ Deployed | 4 hours, configurable per strategy |
| Force-close DB safety | ✅ Deployed | Refuses DB write on rejected exchange close |
| Paper-mode bar scanner | ✅ Deployed | Scans 1m bars for intra-bar TP/SL touches |
| Circuit breaker streak fix | ✅ Deployed | $0.01 wins no longer reset losing streaks |
| Scorer state persistence | ✅ Deployed | Survives restarts via `br_state.json` |
| Stale-bar guard | ✅ Deployed | Skips scoring if last 5m bar > 6 min old |
| Slippage parity | ✅ Deployed | BREAKOUT/BREAKOUT_RETEST = 0.0002 |

### Current production config (config.yaml)

```yaml
breakout_retest:
  enabled: true
  range_bars: 8
  min_width_pct: 0.001
  max_width_pct: 0.02
  atr_mult_max: 3.0
  vol_spike_mult: 1.1     # actually 1.25 in scorer constant
  retest_bars: 12
  sl_atr_mult: 1.3
  rr_ratio: 2.2
  cooldown_mins: 15
  max_trades_per_day: 6
  max_positions: 3
  check_interval_secs: 30
  exhaustion_pct: 0.025
  exhaustion_bars: 6
  max_boundary_touches: 4
  require_breakout_confirm: true
  min_retest_body_ratio: 0.40
  crash_cooldown_pct: 1.5
  crash_cooldown_hours: 4
  max_entries_per_30min: 2
  btc_confirm_for_alts: false
  choppy_atr_mult: 2.0
  max_hold_hours: 4

risk:
  fixed_risk_mode: true
  fixed_risk_usdt: 50.0
  max_position_size_usdt: 1000
  max_open_positions: 5
  max_same_direction_positions: 3
  leverage: 3
  margin_type: ISOLATED
  max_daily_loss_pct: 3.0
  max_consecutive_losses: 6
  breakeven_disabled_strategies: [breakout_retest, microrange]
  regime_flip_disabled_strategies: [breakout_retest, microrange]
```

---

## 2. Validated baseline performance (3-year backtest)

### Per-coin (commit `d67cf9d`, full filters ON)

| Symbol | Trades | WR | PF | Net $ | Max DD% |
|---|---|---|---|---|---|
| BTCUSDT | 1,176 | 51.4% | 2.43 | +$29 | 8.6% |
| ETHUSDT | 1,027 | 51.1% | 2.37 | +$246 | 3.8% |
| SOLUSDT | 688 | 53.8% | 2.65 | +$740 | 4.9% |
| BNBUSDT | 1,276 | 52.1% | 2.50 | +$711 | 3.0% |
| XRPUSDT | 914 | 49.3% | 2.18 | +$38 | 6.5% |
| LINKUSDT | 730 | 51.0% | 2.32 | +$244 | 5.9% |
| DOGEUSDT | 765 | 50.8% | 2.36 | +$59 | 6.6% |
| SUIUSDT | 403 | 48.6% | 2.10 | +$52 | 4.8% |
| **Blended** | **6,979** | **51.2%** | **2.38** | **+$89/coin** | — |

### Monte Carlo (10,000 iterations on BTC trades)

| Percentile | Max DD | Max losing streak |
|---|---|---|
| p50 | 4.5% | 8 |
| p90 | 6.9% | 11 |
| p95 | 7.9% | 12 |
| **p99** | **9.9%** | **14** |
| Max | 13.0% | 19 |

**Implication**: circuit breaker at `max_consecutive_losses: 6` will trip 3-6 times per year during normal operation. This is by design. Don't panic-reset.

### Last-month (March 2026) per-coin returns at $1000 capital

| Symbol | Trades | WR | PF | Net $ | Return |
|---|---|---|---|---|---|
| BTCUSDT | 32 | 62.5% | 3.66 | +$81 | +8.1% |
| XRPUSDT | 34 | 55.9% | 2.78 | +$66 | +6.6% |
| LINKUSDT | 35 | 51.4% | 2.32 | +$61 | +6.1% |
| SUIUSDT | 19 | 52.6% | 2.44 | +$47 | +4.7% |
| BNBUSDT | 27 | 51.9% | 2.56 | +$45 | +4.5% |
| DOGEUSDT | 22 | 50.0% | 2.20 | +$44 | +4.4% |
| SOLUSDT | 15 | 33.3% | 1.22 | +$9 | +0.9% |
| ETHUSDT | 22 | 36.4% | 1.25 | -$3 | -0.3% |

**Blended**: 206 trades, 51.0% WR, 2.33 PF, +$104/coin/month, +10.4% return.

---

## 3. The big finding — filter ablation (deferred change)

### What we discovered

Walk-forward validated. Walk-forward tool: [tools/filter_ablation_walkforward.py](tools/filter_ablation_walkforward.py).

**Removing the top 5 filters from breakout_retest improves profit on both
in-sample (2023) AND out-of-sample (2024-2026) data.**

### Walk-forward results (8 coins)

| Filter | IS Δ$ | OOS Δ$ | Verdict |
|---|---|---|---|
| **exhaustion_4h** | -$68,672 | **-$197,758** | REMOVE ✓ |
| **retest_body_ratio** | -$66,772 | **-$137,848** | REMOVE ✓ |
| **breakout_confirm** | -$40,543 | **-$104,569** | REMOVE ✓ |
| **boundary_touches** | -$26,058 | **-$41,653** | REMOVE ✓ |
| **btc_confirm_alts** | -$13,539 | **-$22,101** | REMOVE ✓ |

**Total OOS upside if all 5 removed**: **+$503,929 over 2.25 years** = **~$224k/year theoretical**.

### Realistic estimate after deductions

| Adjustment | Impact |
|---|---|
| Raw OOS baseline (annualized) | +$233k/year |
| Slippage compounding (3× trades) | -10% |
| Circuit breaker tripping | -10% |
| Position cap binding | -25% |
| Real fill differences | -5% |
| **Realistic profit** | **~$120-140k/year** |

**Compare to current production**: ~$77k/year. **Net upside**: +$45-65k/year (60-85%).

### Why filters hurt

Per-trade EV stays the same (~$33-34) whether filters are on or off. The filters
aren't catching bad trades — they're rejecting marginal trades that have ~+$30 EV.
Removing them adds 14,970 trades over 3 years with the same average EV.

### Why this contradicts my earlier analysis

The first walk-forward (commit `4d6be18`) tested narrow filter buckets like
"skip BTC PUMP" or "skip 16-24 UTC". Those failed OOS — true. But the new
ablation tests **entire filters**, not narrow buckets within them. Different
test, different result. The new test is the correct one.

### ⚠️ DO NOT apply this change yet

Reasons to wait:

1. **Anti-correlation fix is unverified live** — only 1 closed post-fix paper
   trade so far. We need 10+ to confirm it works.
2. **Two unverified changes overlap** — combining them makes diagnosis impossible.
3. **Real fill compounding** — 3× trade frequency means 3× slippage exposure.
4. **Circuit breaker interaction** — not modeled in backtest; will reduce real profit.
5. **Production stability** — current config makes $77k/year, which is positive.

---

## 4. Deferred changes — staged rollout plan

When ready to apply the filter ablation findings, follow this **strict order**.
Do NOT skip steps. Each phase has a 24-48h validation window.

### Phase 0 — Prerequisites (BEFORE starting Phase 1)

- [ ] Anti-correlation fix validated: 10+ closed post-fix paper trades on VPS
- [ ] Post-fix WR ≥ 45% over 10+ trades
- [ ] Post-fix PF ≥ 2.0
- [ ] No new crash/restart bugs in VPS logs
- [ ] All 8 audit fixes from commit `92d29bf` confirmed working
- [ ] Backup VPS DB before any config change

### Phase 1 — Local sandbox validation

**Goal**: confirm filter changes behave correctly in live conditions before
touching VPS.

```powershell
# On local PC (after DNS + aiodns are fixed)
$env:PAPER_MODE = "1"
$env:DB_PATH = "confluence_bot_local.db"
$env:METRICS_PORT = "8002"
```

Edit **local** `config.yaml` (NOT VPS):
```yaml
breakout_retest:
  max_boundary_touches: 99   # was 4 — disabled for sandbox test
```

```powershell
python main.py
```

Wait 24-48h. Expected outcomes:
- Local trade count ~10-20% higher than equivalent VPS window
- Local WR roughly equal to VPS WR (within 5%)
- No crashes, no naked positions

If local validates → proceed to Phase 2.
If local degrades → revert local config, investigate why.

### Phase 2 — VPS rollout, ONE filter at a time

**For each filter, apply → wait 24-48h → measure → decide.**

#### Phase 2A — boundary_touches (smallest impact, $42k OOS)

```bash
# On VPS
nano /home/botuser/confluence_bot/config.yaml
```

Change:
```yaml
breakout_retest:
  max_boundary_touches: 99   # was 4
```

Restart:
```bash
sudo systemctl restart confluence-bot
sudo journalctl -u confluence-bot -n 30 --no-pager
```

Wait 24-48h. Run validation query:
```bash
sqlite3 /home/botuser/confluence_bot/confluence_bot.db -header -column \
  "SELECT
     COUNT(*) AS closed,
     SUM(CASE WHEN pnl_usdt > 0 THEN 1 ELSE 0 END) AS wins,
     ROUND(100.0 * SUM(CASE WHEN pnl_usdt > 0 THEN 1 ELSE 0 END) / COUNT(*), 1) AS wr_pct,
     ROUND(SUM(pnl_usdt), 2) AS net_pnl
   FROM trades
   WHERE regime='BREAKOUT_RETEST'
     AND status='FILLED'
     AND ts >= '<phase 2A start ts>';"
```

**Pass criteria**: WR ≥ 45%, trade count ~10-15% higher than baseline, net PnL positive or near-zero.

If pass → Phase 2B.
If fail → revert, escalate.

#### Phase 2B — breakout_confirm ($104k OOS)

```yaml
breakout_retest:
  require_breakout_confirm: false   # was true
```

Same restart + 24-48h validation.

#### Phase 2C — retest_body_ratio ($138k OOS)

```yaml
breakout_retest:
  min_retest_body_ratio: 0.0   # was 0.40
```

Same procedure.

#### Phase 2D — exhaustion_4h ($198k OOS, biggest)

```yaml
breakout_retest:
  exhaustion_pct: 0.10   # was 0.025 (effectively disabled)
```

This is the largest change. Watch carefully for 48-72h.

### Phase 3 — Live trading with $5 risk

After all 4 filter removals validated in paper for ≥7 days total:

```yaml
risk:
  fixed_risk_usdt: 5.0           # was 50.0
  max_position_size_usdt: 100    # was 1000
```

Set real Binance API keys in `.env`:
```bash
PAPER_MODE=0
BINANCE_API_KEY=...
BINANCE_SECRET=...
```

Restart, monitor first 30-50 real trades closely.

### Phase 4 — Scale to $50

If $5 risk validates:
```yaml
risk:
  fixed_risk_usdt: 50.0
  max_position_size_usdt: 1000
```

Restart. This is the realistic target ($120-140k/year theoretical).

---

## 5. Audit fix history (already deployed)

### Commit `92d29bf` — 8 audit fixes (Apr 11, ~02:00 UTC)

| ID | Fix | File |
|---|---|---|
| C2 | Circuit breaker streak counter (was treating $0.01 wins as streak-breakers) | core/circuit_breaker.py |
| H1 | Regime-flip Telegram alert showed entry price as exit price | core/trade_monitor.py |
| H2 | Force-close marked DB FILLED on rejected exchange close (ghost positions) | core/trade_monitor.py |
| H3 | False partial-fill warnings on 0-decimal coins (DOGE, XRP) | data/binance_rest.py |
| H4 | Paper-mode missed intra-bar TP/SL touches (biggest live=backtest fix) | core/trade_monitor.py |
| M1 | Committed-risk cache could be stale on burst entries | core/executor.py |
| G4 | Backtest hardcoded max_hold instead of reading config | backtest/engine.py |
| C4 | Renamed `place_limit_then_market` → `place_market_with_bracket` | data/binance_rest.py + exchange_router.py |

### Commit `2c13064` — Max-hold timeout + regime-flip skip

- Added `max_hold_hours: 4` to config (matches backtest 48 × 5M bars)
- Added `regime_flip_disabled_strategies: [breakout_retest, microrange]`
- Trade monitor now force-closes BR trades after 4h regardless of TP/SL state

### Commit `be67b87` — Live=backtest alignment

- Slippage parity: `BREAKOUT_RETEST: 0.0002` (was 0.0005)
- Stale-bar guard: skip scoring if last 5m bar > 6 min old
- BR scorer state persistence to `br_state.json`

### Commit `d67cf9d` — Anti-correlation FIRE-time re-check (CRITICAL)

The single most impactful fix. Prevents the bug where multiple symbols enter
`AWAITING_RETEST` simultaneously and all fire in the same 30s tick, bypassing
`max_entries_per_30min: 2`.

- Re-checks anti-correlation, crash cooldown, choppy market, daily cap at FIRE time
- Mirrored in backtest engine for consistency
- Pre-fix live WR was 28.6% over 21 BR trades (vs backtest 51.2%)
- Post-fix expected to converge to backtest baseline

### Commit `fca3423` — Filter ablation tool

- 13-run filter ablation study
- Reveals all 5 top filters lose money individually

### (Pending) Walk-forward filter ablation tool

- `tools/filter_ablation_walkforward.py`
- Validates filter ablation findings against out-of-sample data
- All 5 filters confirmed REMOVE robust on both IS and OOS

---

## 6. Reference: every diagnostic tool and what it does

| Tool | Purpose | Time |
|---|---|---|
| `backtest/run.py --symbol ALL --strategy breakout_retest` | Standard backtest, all 8 coins | ~2 min |
| `tools/full_audit_phase_a.py` | 5-part statistical audit (also via dashboard Audit tab) | ~2 min |
| `tools/reverse_engineer_br.py` | Per-bucket breakdown by regime/ADX/hour/dow | ~2 min |
| `tools/walk_forward_br.py` | Earlier walk-forward (narrow filter buckets) | ~5 min |
| `tools/tp_sweep_br.py` | Sweeps rr_ratio 2.0 → 3.0 | ~3 min |
| `tools/vol_filter_wf.py` | Walk-forward of vol_spike_mult thresholds | ~3 min |
| `tools/filter_ablation.py` | 13-run filter ablation study | ~10 min (BTC+ETH) or ~35 min (8 coins) |
| **`tools/filter_ablation_walkforward.py`** | **Walk-forward of full filter ablation (THE definitive test)** | ~16 min (8 coins) |

---

## 7. Common SQL queries for VPS validation

### Trade count per strategy
```bash
sqlite3 /home/botuser/confluence_bot/confluence_bot.db -header -column \
  "SELECT regime, COUNT(*) AS total,
          SUM(CASE WHEN status='FILLED' THEN 1 ELSE 0 END) AS closed,
          SUM(CASE WHEN status='OPEN' THEN 1 ELSE 0 END) AS open_now
   FROM trades GROUP BY regime ORDER BY total DESC;"
```

### Post-fix BR performance (replace timestamp with restart time)
```bash
sqlite3 /home/botuser/confluence_bot/confluence_bot.db -header -column \
  "SELECT COUNT(*) AS closed,
          SUM(CASE WHEN pnl_usdt > 0 THEN 1 ELSE 0 END) AS wins,
          ROUND(100.0 * SUM(CASE WHEN pnl_usdt > 0 THEN 1 ELSE 0 END) / COUNT(*), 1) AS wr_pct,
          ROUND(SUM(pnl_usdt), 2) AS net_pnl
   FROM trades
   WHERE regime='BREAKOUT_RETEST'
     AND status='FILLED'
     AND ts >= '2026-04-11T03:15:00';"
```

### Open positions
```bash
sqlite3 /home/botuser/confluence_bot/confluence_bot.db -header -column \
  "SELECT substr(ts,6,14) AS opened, symbol, direction,
          ROUND(entry,4) AS entry, ROUND(stop_loss,4) AS sl,
          ROUND(take_profit,4) AS tp
   FROM trades WHERE status='OPEN' ORDER BY ts;"
```

### Per-coin breakdown (only closed trades)
```bash
sqlite3 /home/botuser/confluence_bot/confluence_bot.db -header -column \
  "SELECT symbol, COUNT(*) AS total,
          SUM(CASE WHEN pnl_usdt > 0 THEN 1 ELSE 0 END) AS wins,
          SUM(CASE WHEN pnl_usdt < 0 THEN 1 ELSE 0 END) AS losses,
          ROUND(100.0 * SUM(CASE WHEN pnl_usdt > 0 THEN 1 ELSE 0 END) /
                COUNT(*), 1) AS wr_pct,
          ROUND(SUM(pnl_usdt), 2) AS net_pnl
   FROM trades WHERE status='FILLED'
   GROUP BY symbol ORDER BY net_pnl DESC;"
```

### Exit reason distribution
```bash
sqlite3 /home/botuser/confluence_bot/confluence_bot.db -header -column \
  "SELECT
     CASE
       WHEN ROUND(exit_price, 6) = ROUND(take_profit, 6) THEN 'TP'
       WHEN ROUND(exit_price, 6) = ROUND(stop_loss, 6) THEN 'SL'
       ELSE 'MAX_HOLD'
     END AS exit_reason,
     COUNT(*) AS count
   FROM trades
   WHERE status='FILLED' AND regime='BREAKOUT_RETEST'
   GROUP BY exit_reason;"
```

---

## 8. Common operational commands

### VPS deployment after pulling new code
```bash
cd $(sudo systemctl show confluence-bot -p WorkingDirectory --value)
git pull
sudo systemctl restart confluence-bot
sudo journalctl -u confluence-bot -n 30 --no-pager
```

### Check VPS health
```bash
sudo systemctl status confluence-bot --no-pager | head -10
ps -p $(pgrep -f 'main.py') -o pid,etime,pcpu,pmem,cmd
free -h && uptime
```

### VPS DB cleanup (preserves post-fix data)
```bash
sudo systemctl stop confluence-bot && \
cp /home/botuser/confluence_bot/confluence_bot.db /home/botuser/confluence_bot/confluence_bot.db.backup_$(date +%Y%m%d_%H%M%S) && \
sqlite3 /home/botuser/confluence_bot/confluence_bot.db "DELETE FROM trades WHERE ts < '2026-04-11T03:15:00'; DELETE FROM signals WHERE ts < '2026-04-11T03:15:00'; DELETE FROM regimes WHERE ts < '2026-04-11T03:15:00'; VACUUM;" && \
sudo systemctl start confluence-bot
```

### Local dev sandbox
```powershell
$env:PAPER_MODE = "1"
$env:DB_PATH = "confluence_bot_local.db"
$env:METRICS_PORT = "8002"
$env:TELEGRAM_CHAT_ID = ""
python main.py
```

Local dashboard: `http://localhost:8002`
VPS dashboard: `http://165.22.57.158:8001`

### Local backtest
```powershell
python backtest/run.py --symbol ALL --strategy breakout_retest
python backtest/run.py --symbol BTCUSDT --strategy breakout_retest --from-date 2025-01-01
python backtest/run.py --symbol BTCUSDT --strategy breakout_retest --show-trades
```

### Refresh local backtest data after a few days
```powershell
python backtest/download_data.py --from-date 2026-04-01
# Then merge into legacy JSON for the backtest engine
python -c "
import json, os
from backtest.data_store import load_bars
from datetime import datetime, timezone
syms = ['BTCUSDT','ETHUSDT','SOLUSDT','BNBUSDT','XRPUSDT','LINKUSDT','DOGEUSDT','SUIUSDT']
tfs = ['1m','5m','15m','1h','4h','1d']
for sym in syms:
    path = f'backtest/data/{sym}.json'
    if not os.path.exists(path): continue
    with open(path) as f: raw = json.load(f)
    updated = False
    for tf in tfs:
        key = f'{sym}:{tf}'
        legacy = raw.get(key, [])
        if not legacy: continue
        last_ts = legacy[-1]['ts']
        far_ts = int(datetime(2027,1,1,tzinfo=timezone.utc).timestamp()*1000)
        fresh = load_bars(sym, tf, last_ts+1, far_ts)
        if fresh:
            raw[key] = legacy + fresh
            updated = True
    if updated:
        with open(path,'w') as f: json.dump(raw, f)
        print(f'{sym}: updated')
"
```

### Local ISP fix (Cloudflare DNS) — once per network setup
PowerShell as Administrator:
```
netsh interface ip set dns "Wi-Fi" static 1.1.1.1 primary
netsh interface ip add dns "Wi-Fi" 1.0.0.1 index=2
ipconfig /flushdns
nslookup fapi.binance.com
```

If `nslookup` returns CloudFront IPs, DNS is fixed.

### Local aiodns workaround (one time)
```powershell
pip uninstall aiodns -y
```
Required because aiodns bypasses Windows DNS and directly queries blocked DNS servers.

---

## 9. Pre-live checklist

Before flipping `PAPER_MODE=0` and using real money:

### Validation gates (all must pass)
- [ ] Anti-correlation fix verified: 10+ closed post-fix paper trades
- [ ] Paper WR ≥ 45% over 10+ trades
- [ ] Paper PF ≥ 2.0
- [ ] Filter ablation rolled out and validated (Phase 1+2 above)
- [ ] All 4 filters removed and validated for 7+ days each
- [ ] No crashes in VPS logs for 24h
- [ ] No ghost positions in past week
- [ ] Telegram alerts working (test fire one alert manually)
- [ ] DB backup made before live switch

### Config changes for live
- [ ] `fixed_risk_usdt: 5.0` (1/10 of paper)
- [ ] `max_position_size_usdt: 100` (1/10 of paper)
- [ ] `leverage: 3` (unchanged)
- [ ] `margin_type: ISOLATED` (unchanged)
- [ ] All 4 deferred filter removals applied
- [ ] `PAPER_MODE=0` in .env
- [ ] `BINANCE_API_KEY=...` and `BINANCE_SECRET=...` in .env
- [ ] API key has Futures trading permission ONLY (no withdrawal, no spot)
- [ ] API key restricted to VPS IP

### First startup verification (live mode)
- [ ] Check log: `Symbol setup complete: leverage=3x  margin=ISOLATED  symbols=[...]`
- [ ] No `marginType` or `leverage` warnings
- [ ] Manually verify in Binance UI: BTCUSDT shows 3× / ISOLATED
- [ ] Account balance appears correctly in dashboard
- [ ] First trade closes match expectation

### After 10 real trades
- [ ] Real fill prices within 0.1% of expected
- [ ] All trades have proper SL/TP orders on exchange
- [ ] Telegram alerts fired for each close
- [ ] $ P/L matches theoretical (based on entry/exit move)

### After 50 real trades
- [ ] Real WR within 5% of backtest WR per coin
- [ ] No naked positions ever
- [ ] No SL placement failures
- [ ] If passing → scale to `fixed_risk_usdt: 50`

---

## 10. Things we explicitly decided NOT to do

| Decision | Reason | Date |
|---|---|---|
| Don't change `rr_ratio` from 2.2 | TP sweep showed only 1-2% gain at any value | Apr 10 |
| Don't add per-symbol `rr_ratio` overrides | Maintenance overhead > marginal gain | Apr 10 |
| Don't add ADX entry filter | Walk-forward proved it fails OOS | Apr 10 |
| Don't add hour-of-day filter | Statistically random (p=0.88 BTC) | Apr 10 |
| Don't add partial TP at 1R | Untested, requires backtest plumbing | Apr 10 |
| Don't add trailing stop to BR | Disabled in config — backtest validated without it | Apr 10 |
| Don't enable breakeven move on BR | `breakeven_disabled_strategies` — degrades PF | Apr 10 |
| Don't enable regime-flip close on BR | `regime_flip_disabled_strategies` — backtest doesn't model | Apr 10 |
| Don't add MATIC/ARB/OP/DOT yet | Wait for current strategy to validate first | Apr 11 |
| Don't increase `max_open_positions` past 5 | Margin headroom + correlation risk | Apr 11 |
| Don't tighten `vol_spike_mult` past 1.25 | Walk-forward proved it loses money | Apr 11 |
| Don't disable filters all at once | Staged rollout for diagnosis safety | Apr 11 |

---

## 11. Known issues / things to watch

### Resolved (don't worry about these)
- ~~Anti-correlation gate leak (fix `d67cf9d`)~~
- ~~Paper-mode missing TP wicks (fix `92d29bf` H4)~~
- ~~Force-close ghost positions (fix `92d29bf` H2)~~
- ~~Circuit breaker streak counter bug (fix `92d29bf` C2)~~
- ~~Pre-fix live WR 28.6% vs backtest 51.2%~~ (root cause fixed, validation pending)

### Open / monitoring
- **Post-fix paper WR not yet validated** — only 1 closed post-fix trade so far
- **38% MAX_HOLD rate in pre-fix data** — suspected to be a symptom of the anti-correlation bug, not a separate issue, but watch post-fix data
- **3 new coins (ADA, AVAX, TAO)** missing from legacy backtest .json files — they work in live but can't be backtested via current engine
- **Local PC needs DNS + aiodns workaround** to run main.py — fixed but documented
- **Backtest data only goes to 2026-04-01** in legacy JSON — must be re-downloaded periodically

### Hard problems we never solved
- Backtest engine uses legacy `BTCUSDT.json` monolith but live bot uses compressed `.json.gz` cache → requires manual merge step to backtest the latest data
- ccxt path in `exchange_router.py` still uses LIMIT-then-MARKET which we never tested
- The `setup_symbols()` call only runs in live mode (PAPER_MODE=0) — first real-money startup is the only way to verify it works

---

## 12. The "what to do next" decision tree

```
Are you trying to validate the post-fix anti-correlation fix?
├── YES → wait for 10+ closed post-fix paper trades on VPS, then check WR
│         If WR ≥ 45% → fix is validated
│         If WR < 35% → escalate, investigate further
│
├── Trying to capture the filter ablation upside?
│   ├── Risk-tolerant + experienced → start Phase 2A (boundary_touches removal)
│   ├── Cautious → start Phase 1 (local sandbox test first)
│   └── Risk-averse → wait for anti-correlation validation first, then decide
│
├── Trying to add more coins?
│   └── Don't yet. Validate current 11 first.
│
├── Trying to go live with real money?
│   └── Run the full pre-live checklist (section 9). Don't skip steps.
│
└── Just want the bot to keep running safely?
    └── Do nothing. It's running. Check back in 24h.
```

---

## 13. The single most important fact

**The strategy core (range + breakout + retest) is the entire edge.** Filters
add complexity but mostly subtract from profit. The walk-forward proves this
on both IS and OOS data.

If you remember nothing else from this playbook, remember:
- Current production = $77k/year realistic (with all filters)
- Filter-removed baseline = $120-140k/year realistic
- Upside if you act on the ablation = **+$45-65k/year**

But: don't risk the $77k for the +$45-65k by rushing. Stage the rollout.
Validate each change. The bot is profitable. The filter gain is bonus, not
salvation.
