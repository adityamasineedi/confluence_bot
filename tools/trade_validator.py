"""
tools/trade_validator.py
Quantitative trade-by-trade validation of live VPS trades against
actual market data.  Implements the 10-step audit framework.

For each trade:
  1. Reconstruct market context (50 bars before entry)
  2. Validate entry logic (breakout + retest)
  3. Check fake breakout conditions
  4. Check HTF alignment
  5. Check risk-reward
  6. Check volatility / session
  7. Diagnose loss reason
  8. Compute ideal entry vs actual
  9. Output validation table
 10. Summary metrics
"""
import os, sys, statistics
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from backtest.data_store import load_bars


# ── Trade data (from VPS dashboard paste) ────────────────────────────────────

TRADES = [
    {"ts": "2026-04-12T06:32:36", "sym": "SUIUSDT",  "dir": "LONG",  "entry": 0.9428,   "sl": 0.9334,  "tp": 0.9635,  "pnl": -10.97, "strat": "BR"},
    {"ts": "2026-04-12T06:32:05", "sym": "ADAUSDT",  "dir": "LONG",  "entry": 0.2502,   "sl": 0.2477,  "tp": 0.2557,  "pnl": -10.99, "strat": "BR"},
    {"ts": "2026-04-12T05:41:59", "sym": "ETHUSDT",  "dir": "LONG",  "entry": 2285.37,  "sl": 2262.52, "tp": 2335.65, "pnl": -11.00, "strat": "BR"},
    {"ts": "2026-04-12T03:39:48", "sym": "ETHUSDT",  "dir": "LONG",  "entry": 2303.30,  "sl": 2280.27, "tp": 2353.97, "pnl": -10.99, "strat": "BR"},
    {"ts": "2026-04-12T00:37:00", "sym": "DOGEUSDT", "dir": "LONG",  "entry": 0.0943,   "sl": 0.0933,  "tp": 0.0963,  "pnl": -10.97, "strat": "BR"},
    {"ts": "2026-04-12T00:31:35", "sym": "BTCUSDT",  "dir": "LONG",  "entry": 73509.4,  "sl": 72994.5, "tp": 74539.2, "pnl": -8.23,  "strat": "FVG"},
    {"ts": "2026-04-11T22:10:16", "sym": "LINKUSDT", "dir": "LONG",  "entry": 9.0530,   "sl": 8.9620,  "tp": 9.2520,  "pnl": 20.98,  "strat": "BR"},
    {"ts": "2026-04-11T21:44:43", "sym": "ADAUSDT",  "dir": "LONG",  "entry": 0.2487,   "sl": 0.2462,  "tp": 0.2542,  "pnl": 21.10,  "strat": "BR"},
    {"ts": "2026-04-11T21:30:11", "sym": "SUIUSDT",  "dir": "LONG",  "entry": 0.9356,   "sl": 0.9262,  "tp": 0.9562,  "pnl": 21.01,  "strat": "BR"},
    {"ts": "2026-04-11T18:41:48", "sym": "LINKUSDT", "dir": "SHORT", "entry": 8.9510,   "sl": 9.0410,  "tp": 8.7540,  "pnl": -11.06, "strat": "BR"},
    {"ts": "2026-04-11T18:41:47", "sym": "ETHUSDT",  "dir": "SHORT", "entry": 2232.52,  "sl": 2254.85, "tp": 2183.40, "pnl": -11.01, "strat": "BR"},
    {"ts": "2026-04-11T15:30:57", "sym": "SUIUSDT",  "dir": "LONG",  "entry": 0.9361,   "sl": 0.9267,  "tp": 0.9567,  "pnl": -11.03, "strat": "BR"},
    {"ts": "2026-04-11T14:50:53", "sym": "BTCUSDT",  "dir": "LONG",  "entry": 72777.9,  "sl": 72050.1, "tp": 74379.0, "pnl": -2.53,  "strat": "BR"},
    {"ts": "2026-04-11T14:49:52", "sym": "BNBUSDT",  "dir": "LONG",  "entry": 607.18,   "sl": 601.11,  "tp": 620.54,  "pnl": -4.12,  "strat": "BR"},
    {"ts": "2026-04-11T12:12:06", "sym": "TAOUSDT",  "dir": "SHORT", "entry": 261.26,   "sl": 263.97,  "tp": 255.30,  "pnl": -11.39, "strat": "BR"},
    {"ts": "2026-04-11T11:23:57", "sym": "DOGEUSDT", "dir": "SHORT", "entry": 0.0931,   "sl": 0.0940,  "tp": 0.0910,  "pnl": 2.12,   "strat": "BR"},
    {"ts": "2026-04-11T10:45:53", "sym": "AVAXUSDT", "dir": "SHORT", "entry": 9.2580,   "sl": 9.3506,  "tp": 9.0543,  "pnl": -4.03,  "strat": "BR"},
]


def _ts_ms(iso: str) -> int:
    dt = datetime.fromisoformat(iso).replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def _utc_hour(iso: str) -> int:
    return datetime.fromisoformat(iso).hour


def _session(hour: int) -> str:
    if 0 <= hour < 8:   return "ASIA"
    if 8 <= hour < 14:  return "LONDON"
    if 14 <= hour < 22: return "NY"
    return "LATE_NY"


def _load_context(sym: str, entry_ts_ms: int, tf: str = "5m", before: int = 60, after: int = 5):
    """Load bars around the entry timestamp."""
    ms_per_bar = {"1m": 60000, "5m": 300000, "15m": 900000, "1h": 3600000, "4h": 14400000}
    bar_ms = ms_per_bar.get(tf, 300000)
    start = entry_ts_ms - before * bar_ms
    end   = entry_ts_ms + after * bar_ms
    return load_bars(sym, tf, start, end)


def _ema(closes: list[float], period: int) -> float:
    if len(closes) < period:
        return 0.0
    k = 2.0 / (period + 1)
    e = sum(closes[:period]) / period
    for c in closes[period:]:
        e = c * k + e * (1 - k)
    return e


def _atr(bars: list[dict], period: int = 14) -> float:
    if len(bars) < period + 1:
        return 0.0
    trs = []
    for i in range(1, len(bars)):
        h, l, pc = bars[i]["h"], bars[i]["l"], bars[i - 1]["c"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(trs[-period:]) / period if trs else 0.0


def _vol_ratio(bars: list[dict], idx: int, period: int = 20) -> float:
    if idx < period:
        return 0.0
    avg = sum(b["v"] for b in bars[idx - period:idx]) / period
    return (bars[idx]["v"] / avg) if avg > 0 else 0.0


def _find_range(bars_5m: list[dict], entry_idx: int, lookback: int = 8):
    """Look for a tight range in the lookback bars before entry_idx."""
    if entry_idx < lookback + 2:
        return None
    window = bars_5m[entry_idx - lookback:entry_idx]
    rng_h = max(b["h"] for b in window)
    rng_l = min(b["l"] for b in window)
    mid = (rng_h + rng_l) / 2
    if mid <= 0:
        return None
    width_pct = (rng_h - rng_l) / mid * 100
    return {"high": rng_h, "low": rng_l, "width_pct": width_pct}


def _check_breakout(bars_5m: list[dict], entry_idx: int, direction: str, rng: dict):
    """Check if a breakout happened in the bars leading up to entry."""
    if rng is None:
        return {"valid": False, "reason": "NO_RANGE"}
    # Scan backwards from entry to find a bar that closed beyond the range
    for i in range(entry_idx - 1, max(entry_idx - 12, 0), -1):
        b = bars_5m[i]
        if direction == "LONG" and b["c"] > rng["high"]:
            body = abs(b["c"] - b["o"])
            wick = b["h"] - max(b["c"], b["o"])
            bar_range = b["h"] - b["l"]
            vol_r = _vol_ratio(bars_5m, i) if i >= 20 else 0.0
            fake = wick > body and bar_range > 0
            return {
                "valid": True,
                "bar_idx": i,
                "vol_ratio": round(vol_r, 2),
                "body_pct": round(body / bar_range * 100, 1) if bar_range > 0 else 0,
                "fake_signal": fake,
                "type": "FAKE_BREAKOUT" if fake else "TRUE_BREAKOUT",
            }
        if direction == "SHORT" and b["c"] < rng["low"]:
            body = abs(b["c"] - b["o"])
            wick = min(b["c"], b["o"]) - b["l"]
            bar_range = b["h"] - b["l"]
            vol_r = _vol_ratio(bars_5m, i) if i >= 20 else 0.0
            fake = wick > body and bar_range > 0
            return {
                "valid": True,
                "bar_idx": i,
                "vol_ratio": round(vol_r, 2),
                "body_pct": round(body / bar_range * 100, 1) if bar_range > 0 else 0,
                "fake_signal": fake,
                "type": "FAKE_BREAKOUT" if fake else "TRUE_BREAKOUT",
            }
    return {"valid": False, "reason": "NO_BREAKOUT_FOUND"}


def _check_retest(bars_5m: list[dict], breakout_idx: int, entry_idx: int,
                  direction: str, flip_level: float):
    """Check if a valid retest happened between breakout and entry."""
    if breakout_idx is None or entry_idx <= breakout_idx:
        return {"valid": False, "reason": "NO_RETEST_WINDOW"}
    for i in range(breakout_idx + 1, min(entry_idx + 1, len(bars_5m))):
        b = bars_5m[i]
        if direction == "LONG":
            touched = b["l"] <= flip_level * 1.003
            confirmed = b["c"] > flip_level
            if touched and confirmed:
                body = abs(b["c"] - b["o"])
                bar_range = b["h"] - b["l"]
                body_ratio = body / bar_range if bar_range > 0 else 0
                return {
                    "valid": True,
                    "bar_idx": i,
                    "body_ratio": round(body_ratio, 2),
                    "quality": "STRONG" if body_ratio >= 0.4 else "WEAK_WICK",
                }
        else:
            touched = b["h"] >= flip_level * 0.997
            confirmed = b["c"] < flip_level
            if touched and confirmed:
                body = abs(b["c"] - b["o"])
                bar_range = b["h"] - b["l"]
                body_ratio = body / bar_range if bar_range > 0 else 0
                return {
                    "valid": True,
                    "bar_idx": i,
                    "body_ratio": round(body_ratio, 2),
                    "quality": "STRONG" if body_ratio >= 0.4 else "WEAK_WICK",
                }
    return {"valid": False, "reason": "NO_RETEST_TOUCH"}


def _htf_trend(sym: str, entry_ts_ms: int):
    """Check 1H and 4H trend at entry time."""
    bars_1h = _load_context(sym, entry_ts_ms, "1h", before=25, after=0)
    bars_4h = _load_context(sym, entry_ts_ms, "4h", before=25, after=0)

    result = {"ema20_1h": None, "ema20_4h": None, "trend_1h": "UNKNOWN", "trend_4h": "UNKNOWN"}

    if bars_1h and len(bars_1h) >= 21:
        closes = [b["c"] for b in bars_1h]
        ema20 = _ema(closes, 20)
        result["ema20_1h"] = round(ema20, 4)
        result["trend_1h"] = "BULL" if closes[-1] > ema20 else "BEAR"

    if bars_4h and len(bars_4h) >= 21:
        closes = [b["c"] for b in bars_4h]
        ema20 = _ema(closes, 20)
        result["ema20_4h"] = round(ema20, 4)
        result["trend_4h"] = "BULL" if closes[-1] > ema20 else "BEAR"

    return result


def _compute_rr(entry: float, sl: float, tp: float, direction: str) -> float:
    if direction == "LONG":
        risk = entry - sl
        reward = tp - entry
    else:
        risk = sl - entry
        reward = entry - tp
    if risk <= 0:
        return 0.0
    return round(reward / risk, 2)


def validate_trade(trade: dict) -> dict:
    """Full 10-step validation of a single trade."""
    sym       = trade["sym"]
    direction = trade["dir"]
    entry     = trade["entry"]
    sl        = trade["sl"]
    tp        = trade["tp"]
    pnl       = trade["pnl"]
    strat     = trade["strat"]
    ts_iso    = trade["ts"]
    ts_ms     = _ts_ms(ts_iso)
    hour      = _utc_hour(ts_iso)
    session   = _session(hour)
    is_win    = pnl > 0

    result = {
        "ts":        ts_iso[:16],
        "sym":       sym,
        "dir":       direction,
        "strat":     strat,
        "pnl":       pnl,
        "outcome":   "WIN" if is_win else "LOSS",
        "session":   session,
        "hour_utc":  hour,
    }

    # Load 5m context
    bars_5m = _load_context(sym, ts_ms, "5m", before=60, after=5)
    if not bars_5m or len(bars_5m) < 30:
        result["valid"] = "INSUFFICIENT_DATA"
        result["errors"] = ["No 5m data around entry time"]
        return result

    # Find entry bar index
    entry_idx = None
    for i, b in enumerate(bars_5m):
        if b["ts"] >= ts_ms:
            entry_idx = i
            break
    if entry_idx is None:
        entry_idx = len(bars_5m) - 1

    # Step 1: Market context
    rng = _find_range(bars_5m, entry_idx, lookback=8)
    result["range"] = rng

    # ATR at entry
    atr_val = _atr(bars_5m[:entry_idx + 1])
    sl_dist = abs(entry - sl)
    sl_pct  = sl_dist / entry * 100 if entry > 0 else 0
    result["atr_5m"] = round(atr_val, 6)
    result["sl_dist_pct"] = round(sl_pct, 2)

    # Step 2: Validate entry (BR only)
    errors = []
    if strat == "BR":
        if rng is None:
            errors.append("NO_RANGE")
            result["breakout"] = {"valid": False}
            result["retest"] = {"valid": False}
        else:
            flip_level = rng["high"] if direction == "LONG" else rng["low"]

            # Check breakout
            bo = _check_breakout(bars_5m, entry_idx, direction, rng)
            result["breakout"] = bo

            if not bo["valid"]:
                errors.append(bo.get("reason", "NO_BREAKOUT"))
            elif bo.get("fake_signal"):
                errors.append("FAKE_BREAKOUT")

            # Step 3: Check retest
            bo_idx = bo.get("bar_idx")
            rt = _check_retest(bars_5m, bo_idx, entry_idx, direction, flip_level)
            result["retest"] = rt

            if not rt["valid"]:
                errors.append(rt.get("reason", "NO_RETEST"))
            elif rt.get("quality") == "WEAK_WICK":
                errors.append("WEAK_RETEST")

            # Volume at breakout
            vol_r = bo.get("vol_ratio", 0)
            if vol_r < 1.25:
                errors.append("LOW_VOLUME")
            result["breakout_vol_ratio"] = vol_r

    elif strat == "FVG":
        result["breakout"] = {"valid": "N/A (FVG)"}
        result["retest"] = {"valid": "N/A (FVG)"}

    # Step 4: HTF alignment
    htf = _htf_trend(sym, ts_ms)
    result["htf"] = htf

    if direction == "LONG" and htf["trend_1h"] == "BEAR":
        errors.append("COUNTER_TREND_1H")
    if direction == "SHORT" and htf["trend_1h"] == "BULL":
        errors.append("COUNTER_TREND_1H")
    if direction == "LONG" and htf["trend_4h"] == "BEAR":
        errors.append("COUNTER_TREND_4H")
    if direction == "SHORT" and htf["trend_4h"] == "BULL":
        errors.append("COUNTER_TREND_4H")

    # Step 5: Risk-Reward
    rr = _compute_rr(entry, sl, tp, direction)
    result["rr_actual"] = rr
    if rr < 1.5:
        errors.append("BAD_RR")

    # Step 6: Volatility / session
    if session == "ASIA" and atr_val > 0:
        avg_bars = bars_5m[max(0, entry_idx - 50):entry_idx]
        if avg_bars:
            avg_atr = _atr(avg_bars)
            if avg_atr > 0 and atr_val > avg_atr * 2:
                errors.append("ATR_SPIKE")

    # Step 7: Determine validity
    if not errors:
        result["valid"] = "VALID"
    else:
        result["valid"] = "INVALID"
    result["errors"] = errors

    # Step 8: Ideal entry vs actual
    if strat == "BR" and rng:
        ideal = rng["high"] if direction == "LONG" else rng["low"]
        entry_error = abs(entry - ideal)
        entry_error_pct = entry_error / entry * 100 if entry > 0 else 0
        result["ideal_entry"] = round(ideal, 6)
        result["entry_error_pct"] = round(entry_error_pct, 3)
        if entry_error_pct > 0.3:
            errors.append("POOR_EXECUTION")

    # Step 9: Loss diagnosis
    if not is_win:
        if "FAKE_BREAKOUT" in errors:
            result["loss_reason"] = "FAKE_BREAKOUT"
        elif "COUNTER_TREND_1H" in errors or "COUNTER_TREND_4H" in errors:
            result["loss_reason"] = "COUNTER_TREND"
        elif "NO_RETEST" in errors or "NO_RETEST_TOUCH" in errors:
            result["loss_reason"] = "NO_RETEST"
        elif "WEAK_RETEST" in errors:
            result["loss_reason"] = "WEAK_RETEST"
        elif "LOW_VOLUME" in errors:
            result["loss_reason"] = "LOW_VOLUME"
        elif "BAD_RR" in errors:
            result["loss_reason"] = "BAD_RR"
        elif "NO_RANGE" in errors:
            result["loss_reason"] = "NO_VALID_SETUP"
        else:
            result["loss_reason"] = "VALID_LOSS"  # strategy worked correctly, market just went against
    else:
        result["loss_reason"] = "N/A (WIN)"

    return result


def main():
    print(f"\n{'='*100}")
    print(f"  TRADE-BY-TRADE VALIDATION — VPS Live Trades (Apr 11-12, 2026)")
    print(f"{'='*100}")

    results = []
    for t in TRADES:
        r = validate_trade(t)
        results.append(r)

    # ── Step 9: Output table ──
    print(f"\n{'='*100}")
    print(f"  {'#':<3} {'Time':<17} {'Sym':<10} {'Dir':<6} {'PnL':>8}  {'RR':>5} "
          f"{'HTF1H':>6} {'BO':>6} {'Retest':>8} {'SL%':>5}  "
          f"{'Valid':<12} {'Loss Reason':<20} {'Errors'}")
    print(f"  {'-'*3} {'-'*17} {'-'*10} {'-'*6} {'-'*8}  {'-'*5} "
          f"{'-'*6} {'-'*6} {'-'*8} {'-'*5}  "
          f"{'-'*12} {'-'*20} {'-'*30}")

    for i, r in enumerate(results, 1):
        bo_str = "N/A"
        rt_str = "N/A"
        if isinstance(r.get("breakout"), dict):
            if r["breakout"].get("valid") is True:
                bo_str = r["breakout"].get("type", "OK")[:6]
            elif r["breakout"].get("valid") == "N/A (FVG)":
                bo_str = "FVG"
            else:
                bo_str = "NONE"
        if isinstance(r.get("retest"), dict):
            if r["retest"].get("valid") is True:
                rt_str = r["retest"].get("quality", "OK")[:8]
            elif r["retest"].get("valid") == "N/A (FVG)":
                rt_str = "FVG"
            else:
                rt_str = "NONE"

        htf_1h = r.get("htf", {}).get("trend_1h", "?")[:4]
        rr     = r.get("rr_actual", 0)
        sl_pct = r.get("sl_dist_pct", 0)
        errs   = ", ".join(r.get("errors", []))[:40]

        print(f"  {i:<3} {r['ts']:<17} {r['sym']:<10} {r['dir']:<6} "
              f"{'%+.2f' % r['pnl']:>8}  {rr:>5.2f} "
              f"{htf_1h:>6} {bo_str:>6} {rt_str:>8} {sl_pct:>5.2f}  "
              f"{r['valid']:<12} {r.get('loss_reason',''):>20}  {errs}")

    # ── Step 10: Summary metrics ──
    valid_count   = sum(1 for r in results if r["valid"] == "VALID")
    invalid_count = sum(1 for r in results if r["valid"] == "INVALID")
    nodata_count  = sum(1 for r in results if r["valid"] == "INSUFFICIENT_DATA")
    total         = len(results)

    losses = [r for r in results if r["pnl"] < 0 and r["valid"] != "INSUFFICIENT_DATA"]
    wins   = [r for r in results if r["pnl"] > 0 and r["valid"] != "INSUFFICIENT_DATA"]

    # Loss reason frequency
    loss_reasons = {}
    for r in losses:
        reason = r.get("loss_reason", "UNKNOWN")
        loss_reasons[reason] = loss_reasons.get(reason, 0) + 1

    # Error frequency
    all_errors = {}
    for r in results:
        for e in r.get("errors", []):
            all_errors[e] = all_errors.get(e, 0) + 1

    # Entry errors
    entry_errors = [r["entry_error_pct"] for r in results
                    if "entry_error_pct" in r and r["valid"] != "INSUFFICIENT_DATA"]

    # Fake breakout count
    fake_bo = sum(1 for r in results
                  if isinstance(r.get("breakout"), dict)
                  and r["breakout"].get("fake_signal"))

    # Counter trend count
    counter = sum(1 for r in results
                  if any("COUNTER_TREND" in e for e in r.get("errors", [])))

    print(f"\n{'='*100}")
    print(f"  SUMMARY")
    print(f"{'='*100}")
    print(f"  Total trades:          {total}")
    print(f"  With data:             {total - nodata_count}")
    print(f"  Valid entries:         {valid_count} ({valid_count/(total-nodata_count)*100:.0f}%)" if total - nodata_count > 0 else "")
    print(f"  Invalid entries:       {invalid_count} ({invalid_count/(total-nodata_count)*100:.0f}%)" if total - nodata_count > 0 else "")
    print(f"  Insufficient data:     {nodata_count}")
    print(f"  Fake breakouts:        {fake_bo}")
    print(f"  Counter-trend entries: {counter}")
    if entry_errors:
        print(f"  Avg entry error:       {statistics.mean(entry_errors):.3f}%")
    print(f"\n  Loss reason breakdown:")
    for reason, count in sorted(loss_reasons.items(), key=lambda x: -x[1]):
        print(f"    {reason:<25} {count}")
    print(f"\n  Error frequency (across all trades):")
    for err, count in sorted(all_errors.items(), key=lambda x: -x[1]):
        print(f"    {err:<25} {count}")

    # Hypothetical WR if bad trades were filtered
    valid_trades = [r for r in results if r["valid"] == "VALID"]
    if valid_trades:
        valid_wins = sum(1 for r in valid_trades if r["pnl"] > 0)
        print(f"\n  Hypothetical WR (valid trades only): "
              f"{valid_wins}/{len(valid_trades)} = "
              f"{valid_wins/len(valid_trades)*100:.1f}%")
        print(f"  Current WR (all trades):             "
              f"{len(wins)}/{total - nodata_count} = "
              f"{len(wins)/(total-nodata_count)*100:.1f}%" if total - nodata_count > 0 else "")

    print(f"\n{'='*100}")
    print(f"  TOP 3 CAUSES OF LOSSES")
    print(f"{'='*100}")
    top3 = sorted(loss_reasons.items(), key=lambda x: -x[1])[:3]
    for i, (reason, count) in enumerate(top3, 1):
        print(f"  {i}. {reason} ({count} trades)")
    print(f"\n{'='*100}\n")


if __name__ == "__main__":
    main()
