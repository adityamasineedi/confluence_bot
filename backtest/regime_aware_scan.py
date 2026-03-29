"""Regime-aware strategy scanner.

For each symbol × strategy combination:
1. Run the backtest over the full period
2. Break results down by regime (TREND / RANGE / CRASH / PUMP / BREAKOUT)
3. Show which strategy wins in which regime for each coin
4. Output recommended strategy_routing config

Usage:
    python -m backtest.regime_aware_scan
    python -m backtest.regime_aware_scan --symbol BTCUSDT,SOLUSDT
    python -m backtest.regime_aware_scan --strategies microrange,sweep,ema_pullback
"""
import argparse
import bisect
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("regime_scan")

SYMBOLS = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT",
    "XRPUSDT", "LINKUSDT", "DOGEUSDT", "SUIUSDT",
]

STRATEGIES = [
    "microrange", "ema_pullback", "sweep",
    "zone", "fvg", "bos", "vwap_band", "leadlag",
]

FROM_DATE = "2025-06-01"
TO_DATE   = "2025-12-31"
CAPITAL   = 1000.0
RISK_PCT  = 0.01
REGIMES   = ["TREND", "RANGE", "BREAKOUT", "CRASH", "PUMP"]

_MIN_PF     = 1.15
_MIN_WR     = 0.25
_MIN_TRADES = 10


# ── Pure helpers (no cache / no numpy dependency) ─────────────────────────────

def _date_to_ms(date_str: str) -> int:
    return int(datetime.strptime(date_str, "%Y-%m-%d")
               .replace(tzinfo=timezone.utc).timestamp() * 1000)


def _ema_final(closes: list[float], period: int) -> float:
    """Return the final EMA value over a list of closes."""
    if len(closes) < period:
        return 0.0
    k   = 2.0 / (period + 1)
    ema = sum(closes[:period]) / period
    for c in closes[period:]:
        ema = c * k + ema * (1.0 - k)
    return ema


def _wilder_smooth(values: list[float], period: int) -> list[float]:
    out = [0.0] * len(values)
    if len(values) < period:
        return out
    out[period - 1] = sum(values[:period]) / period
    for i in range(period, len(values)):
        out[i] = (out[i - 1] * (period - 1) + values[i]) / period
    return out


def _adx_from_bars(bars: list[dict], period: int = 14) -> float:
    """Compute ADX from a list of OHLCV dicts. Returns 0.0 on insufficient data."""
    n = len(bars)
    if n < period * 2 + 1:
        return 0.0
    tr_v, pdm_v, mdm_v = [], [], []
    for i in range(1, n):
        h, l, pc = bars[i]["h"], bars[i]["l"], bars[i - 1]["c"]
        up = bars[i]["h"] - bars[i - 1]["h"]
        dn = bars[i - 1]["l"] - bars[i]["l"]
        tr_v.append(max(h - l, abs(h - pc), abs(l - pc)))
        pdm_v.append(up if up > dn and up > 0 else 0.0)
        mdm_v.append(dn if dn > up and dn > 0 else 0.0)
    s_tr  = _wilder_smooth(tr_v,  period)
    s_pdm = _wilder_smooth(pdm_v, period)
    s_mdm = _wilder_smooth(mdm_v, period)
    dx_v  = []
    for i in range(period - 1, len(s_tr)):
        atr = s_tr[i]
        if atr == 0.0:
            dx_v.append(0.0)
            continue
        pdi   = 100.0 * s_pdm[i] / atr
        mdi   = 100.0 * s_mdm[i] / atr
        denom = pdi + mdi
        dx_v.append(100.0 * abs(pdi - mdi) / denom if denom > 0 else 0.0)
    s_adx    = _wilder_smooth(dx_v, period)
    last_atr = s_tr[-1]
    if last_atr == 0.0 or not s_adx:
        return 0.0
    return s_adx[-1]


# ── Regime timeline builder ───────────────────────────────────────────────────

def build_regime_timeline(sym: str, ohlcv: dict) -> tuple[list[int], dict[int, str]]:
    """Replay regime classification over all 4H bars.

    Returns (sorted_ts_list, {ts_ms: regime_str}) for O(log n) trade lookups.
    Uses the same ADX hysteresis + crash/pump logic as core/regime_detector.py
    but as a pure function over OHLCV dicts — no cache dependency.
    """
    import yaml
    cfg_path = os.path.join(os.path.dirname(__file__), "..", "config.yaml")
    with open(cfg_path) as f:
        cfg = yaml.safe_load(f)
    rcfg = cfg.get("regime", {})

    adx_enter  = float(rcfg.get("adx_range_threshold",  20.0))
    adx_exit   = float(rcfg.get("adx_trend_threshold",   25.0))
    range_max  = float(rcfg.get("range_size_max_pct",     0.12))
    crash_thr  = float(rcfg.get("crash_weekly_drop",     -0.12))
    pump_thr   = float(rcfg.get("pump_weekly_gain",        0.12))
    ema_period = int(rcfg.get("ema_crash_period",           50))
    min_dwell  = int(rcfg.get("regime_min_dwell_bars",       3))
    bo_margin  = float(rcfg.get("breakout_margin_pct",    0.003))
    bo_window  = 3
    adx_period = 14
    adx_needed = adx_period * 2 + 1

    bars_4h = ohlcv.get(f"{sym}:4h", [])
    bars_1d = ohlcv.get(f"{sym}:1d", [])

    timeline: dict[int, str] = {}
    adx_hist: list[float]    = []
    in_range        = False
    dwell           = min_dwell   # start with dwell met so detection fires immediately
    bo_countdown    = 0
    bo_bounds: tuple[float, float] | None = None
    pump_was_active = False

    for bar_idx, bar in enumerate(bars_4h):
        bar_ts = bar["ts"]

        # 1D bars visible at this 4H timestamp
        closes_1d = [b["c"] for b in bars_1d if b["ts"] <= bar_ts]

        # ── PUMP ───────────────────────────────────────────────────────────────
        is_pumping = False
        if len(closes_1d) >= ema_period + 8:
            ema50      = _ema_final(closes_1d, ema_period)
            price_1d   = closes_1d[-1]
            change_7d  = (price_1d - closes_1d[-8]) / closes_1d[-8]
            recent_max = max(closes_1d[-5:-1])
            is_pumping = (
                price_1d > ema50
                and change_7d > pump_thr
                and price_1d > recent_max
            )
        if is_pumping and not pump_was_active:
            pump_was_active = True
            timeline[bar_ts] = "PUMP"
            continue
        pump_was_active = is_pumping

        # ── CRASH ──────────────────────────────────────────────────────────────
        if len(closes_1d) >= ema_period + 8:
            ema50      = _ema_final(closes_1d, ema_period)
            price_1d   = closes_1d[-1]
            change_7d  = (price_1d - closes_1d[-8]) / closes_1d[-8]
            recent_min = min(closes_1d[-5:-1])
            if price_1d < ema50 and change_7d < crash_thr and price_1d < recent_min:
                timeline[bar_ts] = "CRASH"
                continue

        # ── ADX ────────────────────────────────────────────────────────────────
        slice_4h = bars_4h[max(0, bar_idx - adx_needed + 1): bar_idx + 1]
        adx = _adx_from_bars(slice_4h, adx_period) if len(slice_4h) >= adx_needed else 0.0

        # ── ADX hysteresis ─────────────────────────────────────────────────────
        was_ranging = in_range
        adx_hist.append(adx)
        if len(adx_hist) > 3:
            adx_hist = adx_hist[-3:]

        dwell += 1
        if dwell >= min_dwell:
            if not was_ranging:
                if len(adx_hist) == 3 and all(v < adx_enter for v in adx_hist):
                    in_range = True
                    dwell    = 0
            else:
                if len(adx_hist) >= 2 and sum(v > adx_exit for v in adx_hist) >= 2:
                    in_range = False
                    dwell    = 0

        # ── Range exit → arm breakout window ──────────────────────────────────
        if was_ranging and not in_range:
            rng_slice  = bars_4h[max(0, bar_idx - 19): bar_idx + 1]
            bo_countdown = bo_window
            bo_bounds    = (
                max(b["h"] for b in rng_slice),
                min(b["l"] for b in rng_slice),
            )

        # ── RANGE ──────────────────────────────────────────────────────────────
        if in_range:
            rng_slice = bars_4h[max(0, bar_idx - 19): bar_idx + 1]
            rng_high  = max(b["h"] for b in rng_slice)
            rng_low   = min(b["l"] for b in rng_slice)
            mid       = (rng_high + rng_low) / 2.0
            if mid > 0 and (rng_high - rng_low) / mid <= range_max:
                timeline[bar_ts] = "RANGE"
                continue

        # ── BREAKOUT ───────────────────────────────────────────────────────────
        if bo_countdown > 0 and bo_bounds:
            bo_countdown -= 1
            price = bar["c"]
            hi, lo = bo_bounds
            if price > hi * (1.0 + bo_margin) or price < lo * (1.0 - bo_margin):
                timeline[bar_ts] = "BREAKOUT"
                continue

        timeline[bar_ts] = "TREND"

    sorted_ts = sorted(timeline.keys())
    return sorted_ts, timeline


def lookup_regime(trade_ts: int, sorted_ts: list[int], timeline: dict[int, str]) -> str:
    """O(log n) regime lookup — returns the regime of the last 4H bar before trade_ts."""
    if not sorted_ts:
        return "TREND"
    idx = bisect.bisect_right(sorted_ts, trade_ts) - 1
    if idx < 0:
        return "TREND"
    return timeline[sorted_ts[idx]]


# ── Engine runner ─────────────────────────────────────────────────────────────

def _run_engine(
    strategy: str,
    sym:      str,
    ohlcv:    dict,
    capital:  float,
    risk_pct: float,
) -> list[dict]:
    if strategy == "microrange":
        from backtest import microrange_engine as eng
    elif strategy == "ema_pullback":
        from backtest import ema_pullback_engine as eng
    elif strategy == "leadlag":
        from backtest import leadlag_engine as eng
    elif strategy == "sweep":
        from backtest import sweep_engine as eng
    elif strategy == "zone":
        from backtest import zone_engine as eng
    elif strategy == "fvg":
        from backtest import fvg_engine as eng
    elif strategy == "bos":
        from backtest import bos_engine as eng
    elif strategy == "vwap_band":
        from backtest import vwap_band_engine as eng
    else:
        raise ValueError(f"Unknown strategy: {strategy}")

    sym_ohlcv = {k: v for k, v in ohlcv.items() if k.startswith(f"{sym}:")}
    if strategy == "leadlag" and sym != "BTCUSDT":
        sym_ohlcv.update({k: v for k, v in ohlcv.items() if k.startswith("BTCUSDT:")})
        symbols = ["BTCUSDT", sym]
    else:
        symbols = [sym]

    return eng.run(symbols=symbols, ohlcv=sym_ohlcv,
                   starting_capital=capital, risk_pct=risk_pct)


# ── Stats helpers ─────────────────────────────────────────────────────────────

def _bucket_stats(trades: list[dict], capital: float) -> dict:
    n = len(trades)
    if n < _MIN_TRADES:
        return {"status": "insufficient", "trade_count": n}

    net_win   = sum(t["pnl"] for t in trades if t["pnl"] > 0)
    net_loss  = abs(sum(t["pnl"] for t in trades if t["pnl"] < 0))
    total_pnl = sum(t["pnl"] for t in trades)
    wins      = sum(1 for t in trades if t["outcome"] == "WIN")

    pf  = (net_win / net_loss) if net_loss > 0 else float("inf")
    wr  = wins / n
    ret = total_pnl / capital * 100

    result = {
        "status":        "ok",
        "trade_count":   n,
        "win_rate":      round(wr, 3),
        "profit_factor": round(min(pf, 99.9), 3),
        "return_pct":    round(ret, 1),
        "return_abs":    round(total_pnl, 2),
    }

    # Cost breakdown — only when engines supply gross_pnl fields
    has_costs = any("gross_pnl" in t for t in trades)
    if has_costs:
        gross_vals = [t.get("gross_pnl", t["pnl"]) for t in trades]
        gross_pnl  = sum(gross_vals)
        gross_win_ = sum(v for v in gross_vals if v > 0)
        gross_loss_= abs(sum(v for v in gross_vals if v < 0))
        fees       = sum(t.get("cost_fee",  0.0) for t in trades)
        slip       = sum(t.get("cost_slip", 0.0) for t in trades)
        fund       = sum(t.get("cost_fund", 0.0) for t in trades)
        total_cost = fees + slip + fund
        gross_pf   = (gross_win_ / gross_loss_) if gross_loss_ > 0 else float("inf")
        # Break-even PF: the gross PF you need to cover costs
        be_pf      = 1.0 + (total_cost / gross_loss_) if gross_loss_ > 0 else 1.0
        result.update({
            "gross_pnl":     round(gross_pnl,  2),
            "cost_fee":      round(fees,        2),
            "cost_slip":     round(slip,        2),
            "cost_fund":     round(fund,        2),
            "total_cost":    round(total_cost,  2),
            "gross_pf":      round(min(gross_pf, 99.9), 3),
            "break_even_pf": round(be_pf, 3),
        })

    return result


def _keep(stats: dict) -> bool:
    return (
        stats["status"] == "ok"
        and stats["profit_factor"] >= _MIN_PF
        and stats["win_rate"] >= _MIN_WR
        and stats["return_abs"] > 0
    )


def _label(stats: dict) -> str:
    if stats["status"] != "ok":
        return "—"
    if _keep(stats):
        return "← GOOD"
    if stats["return_abs"] > 0:
        return "← OK"
    return "← BAD"


# ── Printers ──────────────────────────────────────────────────────────────────

def _print_breakdown(
    sym:       str,
    strategy:  str,
    by_regime: dict[str, list[dict]],
    capital:   float,
    routing:   dict,
) -> None:
    print(f"\n{sym} × {strategy}")
    sym_routing = routing.get(sym, routing.get("_default", {}))
    for regime in REGIMES:
        is_routed = strategy in sym_routing.get(regime, [])
        trades    = by_regime.get(regime, [])
        if not trades:
            note = "(regime gate: not routed)" if not is_routed else "(no signals fired)"
            print(f"  {regime:<10}   0 trades  {note}")
            continue
        s = _bucket_stats(trades, capital)
        if s["status"] != "ok":
            n    = s["trade_count"]
            note = "" if is_routed else "  [not in routing]"
            print(f"  {regime:<10}  {n:3d} trades  — (insufficient data){note}")
            continue
        route_note = "" if is_routed else "  [not in routing]"

        # Net return label — append gross when cost data available
        if "gross_pnl" in s:
            ret_str = f"net {s['return_abs']:+.1f}$  gross {s['gross_pnl']:+.1f}$"
        else:
            ret_str = f"return {s['return_abs']:+.1f}$"

        print(
            f"  {regime:<10}  {s['trade_count']:3d} trades"
            f"  WR {s['win_rate']*100:.0f}%"
            f"  PF {s['profit_factor']:.2f}"
            f"  {ret_str}"
            f"  {_label(s)}{route_note}"
        )

        # Fee breakdown sub-line
        if "total_cost" in s and s["total_cost"] > 0:
            gross = s["gross_pnl"]
            cost  = s["total_cost"]
            drag  = (cost / abs(gross) * 100) if gross != 0 else 0.0
            be_warn = ""
            if s["gross_pf"] < s["break_even_pf"]:
                be_warn = f"  ⚠ gross PF {s['gross_pf']:.2f} < break-even PF {s['break_even_pf']:.2f}"
            print(
                f"  {'':10}   ${s['cost_fee']:.2f} fees"
                f"  ${s['cost_slip']:.2f} slip"
                f"  ${s['cost_fund']:.2f} fund"
                f"  = ${cost:.2f} total ({drag:.1f}% of gross)"
                f"{be_warn}"
            )


def _best_strategy(sym_data: dict, regime: str) -> tuple[str, dict] | tuple[None, None]:
    """Return (strategy, stats) for the best-performing strategy in this regime."""
    best_strat = None
    best_stats = None
    for strategy in STRATEGIES:
        stats = sym_data.get(strategy, {}).get(regime)
        if not stats or stats["status"] != "ok":
            continue
        if best_stats is None or stats["profit_factor"] > best_stats["profit_factor"]:
            best_strat = strategy
            best_stats = stats
    return best_strat, best_stats


def _regime_badge(stats: dict | None) -> str:
    if stats is None:
        return "?"
    if stats["status"] != "ok":
        return "?"
    if _keep(stats):
        return f"{stats.get('_strategy', '')}✅"
    if stats["return_abs"] > 0:
        return f"{stats.get('_strategy', '')}⚠️"
    return f"{stats.get('_strategy', '')}❌"


def _print_best_strategy_matrix(all_results: dict, symbols: list[str]) -> None:
    print("\n" + "=" * 78)
    print("TABLE 2: Best strategy per symbol × regime")
    header = f"{'Symbol':<10}" + "".join(f"  {r:<14}" for r in REGIMES)
    print(header)
    print("-" * len(header))
    for sym in symbols:
        sym_data = all_results.get(sym, {})
        row = f"{sym:<10}"
        for regime in REGIMES:
            best_strat, best_stats = _best_strategy(sym_data, regime)
            if best_strat is None:
                cell = "?"
            elif best_stats["status"] != "ok":
                cell = "?"
            elif _keep(best_stats):
                cell = f"{best_strat}✅"
            elif best_stats["return_abs"] > 0:
                cell = f"{best_strat}⚠️"
            else:
                cell = f"{best_strat}❌"
            row += f"  {cell:<14}"
        print(row)
    print()
    print("Legend: ✅ PF≥1.15 WR≥25%  ⚠️ profitable but weak  ❌ losing  ? untested/insufficient")


def _print_recommended_routing(all_results: dict, symbols: list[str]) -> None:
    print("\n" + "=" * 68)
    print("TABLE 3: RECOMMENDED strategy_routing — paste into config.yaml:")
    print()
    print("strategy_routing:")
    for sym in symbols:
        print(f"  {sym}:")
        sym_data = all_results.get(sym, {})
        for regime in REGIMES:
            keepers      = []
            comments     = []
            untested     = []
            for strategy in STRATEGIES:
                if strategy == "leadlag":
                    continue   # always added as fallback at end
                stats = sym_data.get(strategy, {}).get(regime)
                if stats is None or stats["status"] != "ok":
                    untested.append(strategy)
                    continue
                if _keep(stats):
                    keepers.append(strategy)
                    comments.append(
                        f"{strategy} PF{stats['profit_factor']:.2f}"
                        f" WR{stats['win_rate']*100:.0f}%"
                    )
            # Always include leadlag as fallback
            keepers.append("leadlag")

            line = f"[{', '.join(keepers)}]"

            comment_parts = []
            if comments:
                comment_parts.append(", ".join(comments))
            if untested:
                comment_parts.append(f"{untested[0]} needs test" if len(untested) == 1
                                     else f"{len(untested)} strategies need test")
            comment = f"  # {'; '.join(comment_parts)}" if comment_parts else ""

            print(f"    {regime:<10}: {line}{comment}")
    print()


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Regime-aware strategy scanner")
    parser.add_argument("--symbol", "--symbols", dest="symbol",
                        default="",        help="Comma-separated symbols to scan (default: all)")
    parser.add_argument("--strategies", default="",        help="Comma-separated strategies")
    parser.add_argument("--from",       dest="from_date",  default=FROM_DATE)
    parser.add_argument("--to",         dest="to_date",    default=TO_DATE)
    parser.add_argument("--capital",    type=float,        default=CAPITAL)
    parser.add_argument("--risk-pct",   type=float,        default=RISK_PCT, dest="risk_pct")
    args = parser.parse_args()

    symbols    = [s.strip().upper() for s in args.symbol.split(",")     if s.strip()] or SYMBOLS
    strategies = [s.strip()         for s in args.strategies.split(",") if s.strip()] or STRATEGIES
    from_ms    = _date_to_ms(args.from_date)
    to_ms      = _date_to_ms(args.to_date) + 86_400_000

    # Load strategy_routing from config for routing-gate display
    import yaml as _yaml
    _cfg_path = os.path.join(os.path.dirname(__file__), "..", "config.yaml")
    with open(_cfg_path) as _f:
        _cfg = _yaml.safe_load(_f)
    routing: dict = _cfg.get("strategy_routing", {})

    total_combos = len(symbols) * len(strategies)
    combo_idx    = 0

    print(f"\nRegime-Aware Strategy Scanner")
    print(f"  Period    : {args.from_date} → {args.to_date}")
    print(f"  Symbols   : {', '.join(symbols)}")
    print(f"  Strategies: {', '.join(strategies)}")
    print(f"  Capital   : ${args.capital:.0f}  |  Risk: {args.risk_pct*100:.1f}%")
    print(f"  Thresholds: PF ≥ {_MIN_PF}  WR ≥ {_MIN_WR*100:.0f}%  trades ≥ {_MIN_TRADES}")
    print()

    from backtest.fetcher import fetch_period_sync

    need_btc_anchor = "leadlag" in strategies

    # Pre-fetch BTC once when it's not in the symbols list but leadlag needs it.
    # When BTCUSDT IS in symbols it will be captured during the normal loop below.
    btc_ohlcv: dict = {}
    if need_btc_anchor and "BTCUSDT" not in symbols:
        print("Pre-fetching BTCUSDT for leadlag anchor...")
        btc_data  = fetch_period_sync(["BTCUSDT"], from_ms, to_ms, warmup_days=60)
        btc_ohlcv = btc_data["ohlcv"]

    # all_results[sym][strategy][regime] = bucket_stats_dict
    all_results: dict = {}
    json_out:    dict = {}

    for sym_idx, sym in enumerate(symbols):
        if sym_idx > 0:
            time.sleep(2)   # rate-limit gap between symbol fetches

        print(f"\nFetching {sym}...")
        try:
            sym_data = fetch_period_sync([sym], from_ms, to_ms, warmup_days=60)
        except Exception as exc:
            log.warning("Fetch failed for %s: %s — skipping", sym, exc)
            print(f"  FETCH ERROR: {exc} — skipping")
            continue
        ohlcv = sym_data["ohlcv"]

        # When BTCUSDT is fetched as part of the normal loop, save it for later alts
        if sym == "BTCUSDT" and need_btc_anchor:
            btc_ohlcv = ohlcv

        # Merge BTC data into every alt's ohlcv so leadlag engine has its anchor
        if btc_ohlcv and sym != "BTCUSDT":
            ohlcv = {**ohlcv, **btc_ohlcv}

        print(f"  Building regime timeline...")
        sorted_ts, timeline = build_regime_timeline(sym, ohlcv)

        regime_counts: dict[str, int] = {}
        for r in timeline.values():
            regime_counts[r] = regime_counts.get(r, 0) + 1
        dist_str = "  ".join(f"{r}:{n}" for r, n in sorted(regime_counts.items()))
        print(f"  4H regime distribution: {dist_str}")

        all_results[sym] = {}
        json_out[sym]    = {}

        # Funding totals per strategy for the end-of-symbol summary
        sym_funding_totals: dict[str, float] = {}

        for strategy in strategies:
            combo_idx += 1
            print(f"  [{combo_idx}/{total_combos}] {sym} × {strategy}...", end="", flush=True)

            try:
                trades = _run_engine(strategy, sym, ohlcv, args.capital, args.risk_pct)
            except Exception as exc:
                log.warning("Engine error %s × %s: %s", sym, strategy, exc)
                print(f" ERROR: {exc} — skipping")
                all_results[sym][strategy] = {}
                json_out[sym][strategy]    = {}
                continue

            # Bucket trades by regime at entry timestamp using the 4H timeline
            by_regime: dict[str, list[dict]] = {r: [] for r in REGIMES}
            for trade in trades:
                regime = lookup_regime(trade.get("entry_ts", 0), sorted_ts, timeline)
                by_regime.setdefault(regime, []).append(trade)

            print(f" {len(trades)} trades")

            # Accumulate per-symbol funding paid (display only — PnL already net)
            strat_fund = sum(t.get("cost_fund", 0.0) for t in trades)
            if strat_fund > 0:
                sym_funding_totals[strategy] = round(strat_fund, 4)

            all_results[sym][strategy] = {}
            json_out[sym][strategy]    = {}

            for regime in REGIMES:
                bucket = by_regime.get(regime, [])
                stats  = _bucket_stats(bucket, args.capital)
                all_results[sym][strategy][regime] = stats
                json_out[sym][strategy][regime]    = stats   # stats only — no per-trade records

            _print_breakdown(sym, strategy, by_regime, args.capital, routing)

        # ── Per-symbol funding summary ─────────────────────────────────────────
        if sym_funding_totals:
            total_fund = sum(sym_funding_totals.values())
            strat_parts = "  ".join(
                f"{s}: ${v:.2f}" for s, v in sorted(sym_funding_totals.items())
            )
            print(f"\n  {sym} funding paid (across all strategies): ${total_fund:.2f}")
            print(f"    by strategy: {strat_parts}")

    _print_best_strategy_matrix(all_results, symbols)
    _print_recommended_routing(all_results, symbols)

    out_path = os.path.join(os.path.dirname(__file__), "regime_scan_results.json")
    with open(out_path, "w") as f:
        json.dump(json_out, f, indent=2)
    print(f"Results saved to {out_path}")


if __name__ == "__main__":
    main()
