"""
verify_htf_local.py
--------------------
Standalone script — no bot running needed.
Fetches real Binance bars and replicates every HTF gate
used by breakout_retest in production.

Run:  python verify_htf_local.py
"""

import asyncio
import time
import requests

# ── Config ────────────────────────────────────────────────────────────────────
SYMBOLS   = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT",
             "XRPUSDT", "LINKUSDT", "DOGEUSDT", "SUIUSDT"]
BTC       = "BTCUSDT"
BASE_URL  = "https://fapi.binance.com"        # Futures endpoint

# ── Binance fetch helper ───────────────────────────────────────────────────────
def fetch_klines(symbol: str, interval: str, limit: int) -> list[dict]:
    url    = f"{BASE_URL}/fapi/v1/klines"
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    r      = requests.get(url, params=params, timeout=10)
    r.raise_for_status()
    bars = []
    for row in r.json():
        bars.append({
            "ts": row[0],
            "o": float(row[1]),
            "h": float(row[2]),
            "l": float(row[3]),
            "c": float(row[4]),
            "v": float(row[5]),
        })
    return bars

# ── EMA helper ────────────────────────────────────────────────────────────────
def ema(closes: list[float], period: int) -> float:
    if len(closes) < period:
        return 0.0
    k   = 2.0 / (period + 1)
    val = sum(closes[:period]) / period
    for c in closes[period:]:
        val = c * k + val * (1.0 - k)
    return val

# ── Test 1: Direction router — BTC 4H EMA200 (needs 210 bars) ────────────────
def test_direction_router_ema200():
    print("\n" + "="*60)
    print("TEST 1 — direction_router: BTC 4H EMA200 (210 bars)")
    print("="*60)

    bars = fetch_klines(BTC, "4h", 220)
    print(f"  Bars fetched  : {len(bars)}")

    if len(bars) < 210:
        print(f"  ❌ CRITICAL: only {len(bars)} bars — need 210")
        print(f"     direction_router returns NONE for every symbol")
        print(f"     This means htf_bull=False htf_bear=False always")
        return None, None, 0.0, 0.0

    closes = [b["c"] for b in bars]
    ema200 = ema(closes, 200)
    last   = closes[-1]
    above  = last > ema200
    gap_pct = (last - ema200) / ema200 * 100

    print(f"  BTC last close: ${last:,.2f}")
    print(f"  4H EMA200     : ${ema200:,.2f}")
    print(f"  Gap           : {gap_pct:+.2f}%")
    print(f"  BTC above EMA : {'✅ YES → allows LONG' if above else '❌ NO  → should block LONG'}")

    # Weekly HH check
    weekly = fetch_klines(BTC, "1w", 4)
    if len(weekly) >= 2:
        hh = weekly[-1]["c"] > weekly[-2]["c"]
        print(f"  Weekly HH     : {'✅ intact (last week close > prior)' if hh else '❌ BROKEN (lower close this week)'}")
    else:
        hh = above
        print(f"  Weekly HH     : ⚠️  insufficient weekly bars — defaulting to EMA result")

    funding_r = fetch_funding(BTC)
    funding_ok_long = funding_r is None or funding_r < 0.0003
    print(f"  Funding rate  : {funding_r}")
    print(f"  Funding ok    : {'✅' if funding_ok_long else '❌ overheated'}")

    # Final direction
    long_ok  = above and hh and funding_ok_long
    short_ok = not above and not hh
    direction = "LONG" if long_ok else ("SHORT" if short_ok else "NONE")
    print(f"\n  ➡️  direction_router result: {direction}")
    if direction == "NONE":
        print(f"     ⚠️  NONE = bot will not enter any trade via direction_router")

    return above, hh, ema200, last

# ── Test 2: Weekly trend gate (weekly_allows_long / weekly_allows_short) ─────
def test_weekly_trend_gate():
    print("\n" + "="*60)
    print("TEST 2 — weekly_trend_gate: BTC 10W EMA")
    print("="*60)

    bars = fetch_klines(BTC, "1w", 20)
    print(f"  Weekly bars   : {len(bars)}")

    if len(bars) < 10:
        print(f"  ⚠️  <10 bars — gate returns True for everything (no block)")
        return

    closes    = [b["c"] for b in bars]
    ema10w    = ema(closes, 10)
    last      = closes[-1]
    above_ema = last > ema10w
    gap_pct   = (last - ema10w) / ema10w * 100

    print(f"  BTC weekly close: ${last:,.2f}")
    print(f"  10W EMA         : ${ema10w:,.2f}")
    print(f"  Gap             : {gap_pct:+.2f}%")
    print(f"  weekly_allows_long  → {'✅ True' if above_ema else '❌ False (BLOCKS long)'}")
    print(f"  weekly_allows_short → {'✅ True' if not above_ema else '❌ False (BLOCKS short)'}")

    if above_ema:
        print(f"\n  ➡️  Gate allows LONGs, blocks SHORTs")
    else:
        print(f"\n  ➡️  Gate allows SHORTs, blocks LONGs")

# ── Test 3: Breakout_retest scorer — internal htf_bull / htf_bear ─────────────
def test_br_scorer_htf():
    print("\n" + "="*60)
    print("TEST 3 — breakout_retest scorer: internal htf_bull/htf_bear (21-period 4H EMA)")
    print("="*60)
    print("  (scorer uses 21-bar 4H EMA, not EMA200 — different from direction_router)\n")

    for sym in SYMBOLS:
        bars = fetch_klines(sym, "4h", 30)
        if len(bars) < 21:
            print(f"  {sym:<12} ❌ only {len(bars)} 4H bars — htf defaults True")
            continue

        closes  = [b["c"] for b in bars]
        k       = 2.0 / 22
        ema_val = sum(closes[:21]) / 21
        for c in closes[21:]:
            ema_val = c * k + ema_val * (1.0 - k)

        last      = closes[-1]
        htf_bull  = last > ema_val
        htf_bear  = last < ema_val
        gap_pct   = (last - ema_val) / ema_val * 100

        bull_str  = "✅ LONG ok" if htf_bull else "❌ LONG blocked"
        bear_str  = "✅ SHORT ok" if htf_bear else "❌ SHORT blocked"
        print(f"  {sym:<12} price=${last:>10,.4f}  ema21={ema_val:>10,.4f}  {gap_pct:>+6.2f}%  |  {bull_str}  |  {bear_str}")

# ── Test 4: Cache bar count check ─────────────────────────────────────────────
def test_bar_counts():
    print("\n" + "="*60)
    print("TEST 4 — Are there enough bars for each gate?")
    print("="*60)

    requirements = {
        "direction_router 4H EMA200": ("4h",  210, BTC),
        "direction_router weekly HH":  ("1w",  4,   BTC),
        "weekly_trend_gate 10W EMA":   ("1w",  15,  BTC),
        "br_scorer htf 4H EMA21":      ("4h",  25,  BTC),
    }

    for name, (tf, need, sym) in requirements.items():
        bars = fetch_klines(sym, tf, need + 5)
        got  = len(bars)
        ok   = got >= need
        status = "✅ OK" if ok else f"❌ ONLY {got} — NEED {need}"
        print(f"  {name:<38} need={need:>4}  got={got:>4}  {status}")

# ── Test 5: Spot the exact bug ────────────────────────────────────────────────
def test_bug_diagnosis():
    print("\n" + "="*60)
    print("TEST 5 — Bug diagnosis: why only LONGs?")
    print("="*60)

    # Fetch BTC 4H
    bars_4h  = fetch_klines(BTC, "4h", 220)
    bars_1w  = fetch_klines(BTC, "1w", 20)

    n_4h = len(bars_4h)
    n_1w = len(bars_1w)

    print(f"\n  BTC 4H bars available : {n_4h}")
    print(f"  BTC 1W bars available : {n_1w}")

    # Check direction_router path
    if n_4h < 210:
        print(f"\n  🔴 ROOT CAUSE FOUND:")
        print(f"     direction_router needs 210 × 4H bars, cache only has {n_4h}")
        print(f"     Code returns NONE when len < 210 → falls through to default LONG")
        print(f"     Fix: check what get_closes('BTCUSDT', window=210, tf='4h') returns in live cache")
    else:
        closes = [b["c"] for b in bars_4h]
        ema200 = ema(closes, 200)
        last   = closes[-1]
        above  = last > ema200

        wcloses = [b["c"] for b in bars_1w]
        hh      = wcloses[-1] > wcloses[-2] if len(wcloses) >= 2 else True

        print(f"\n  BTC vs 4H EMA200: ${last:,.0f} vs ${ema200:,.0f} → {'ABOVE' if above else 'BELOW'}")
        print(f"  Weekly HH intact: {hh}")

        long_ok  = above and hh
        short_ok = not above and not hh

        if long_ok:
            print(f"\n  ✅ Both gates pass LONG — bot correctly takes LONGs right now")
        elif short_ok:
            print(f"\n  🔴 ROOT CAUSE FOUND:")
            print(f"     Both gates say SHORT, but bot fires LONGs")
            print(f"     Check: is breakout_retest in weekly_trend_gate apply_to list in config.yaml?")
            print(f"     If not, weekly_allows_long() returns True always → SHORT gate bypassed")
        else:
            print(f"\n  🟡 Mixed signals — direction_router returns NONE")
            print(f"     Bot should not enter at all, but is entering LONGs")
            print(f"     Check: breakout_retest scorer uses its own htf (21 EMA), not direction_router")
            print(f"     The scorer's htf_bull may be True even when direction_router says NONE")

# ── Funding rate helper ────────────────────────────────────────────────────────
def fetch_funding(symbol: str):
    try:
        url = f"{BASE_URL}/fapi/v1/premiumIndex"
        r   = requests.get(url, params={"symbol": symbol}, timeout=5)
        r.raise_for_status()
        return float(r.json().get("lastFundingRate", 0))
    except Exception:
        return None

# ── Main ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("\n🔍 HTF FILTER LOCAL VERIFICATION")
    print(f"   Running against Binance Futures (fapi)")
    print(f"   Time: {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}")

    test_bar_counts()
    test_direction_router_ema200()
    test_weekly_trend_gate()
    test_br_scorer_htf()
    test_bug_diagnosis()

    print("\n" + "="*60)
    print("DONE — share the output here for next diagnosis step")
    print("="*60)

    # Add this function to verify_htf_local.py
def test_historical_htf(target_date_str: str = "2026-04-04"):
    """
    Reconstructs what htf_bull/htf_bear and weekly gate
    would have returned on a specific past date.
    Fetches 500 bars to ensure enough history before target date.
    """
    import datetime
    print("\n" + "="*60)
    print(f"TEST 6 — Historical HTF state on {target_date_str}")
    print("="*60)

    # Convert target date to millisecond timestamp
    target_dt  = datetime.datetime.strptime(target_date_str, "%Y-%m-%d")
    target_ms  = int(target_dt.timestamp() * 1000)

    # Fetch 500 4H bars ending at target date
    url    = f"{BASE_URL}/fapi/v1/klines"
    params = {"symbol": "BTCUSDT", "interval": "4h",
              "endTime": target_ms, "limit": 220}
    r      = requests.get(url, params=params, timeout=10)
    bars_4h = [{"c": float(x[4])} for x in r.json()]

    # Fetch weekly bars ending at target date
    params2 = {"symbol": "BTCUSDT", "interval": "1w",
               "endTime": target_ms, "limit": 20}
    r2      = requests.get(url, params=params2, timeout=10)
    bars_1w = [{"c": float(x[4])} for x in r2.json()]

    closes_4h = [b["c"] for b in bars_4h]
    closes_1w = [b["c"] for b in bars_1w]

    ema200  = ema(closes_4h, 200) if len(closes_4h) >= 200 else 0
    ema21   = ema(closes_4h, 21)  if len(closes_4h) >= 21  else 0
    ema10w  = ema(closes_1w, 10)  if len(closes_1w) >= 10  else 0

    last_4h = closes_4h[-1]
    last_1w = closes_1w[-1]
    hh      = closes_1w[-1] > closes_1w[-2] if len(closes_1w) >= 2 else True

    print(f"  BTC 4H close on {target_date_str} : ${last_4h:,.2f}")
    print(f"  4H EMA200                         : ${ema200:,.2f}  → {'ABOVE ✅' if last_4h > ema200 else 'BELOW ❌'}")
    print(f"  4H EMA21 (br scorer htf)           : ${ema21:,.2f}   → htf_bull={'✅' if last_4h > ema21 else '❌'}  htf_bear={'✅' if last_4h < ema21 else '❌'}")
    print(f"  Weekly close                       : ${last_1w:,.2f}")
    print(f"  10W EMA                            : ${ema10w:,.2f}  → weekly_allows_long={'✅' if last_1w > ema10w else '❌ BLOCKED'}  weekly_allows_short={'✅' if last_1w < ema10w else '❌ BLOCKED'}")
    print(f"  Weekly HH intact                   : {hh}")

    long_ok  = (last_4h > ema200) and hh
    short_ok = (last_4h < ema200) and not hh
    direction = "LONG" if long_ok else ("SHORT" if short_ok else "NONE")
    print(f"\n  ➡️  direction_router on {target_date_str}: {direction}")
    print(f"  ➡️  br_scorer htf_bull={last_4h > ema21}  htf_bear={last_4h < ema21}")
    print(f"  ➡️  weekly_allows_long={last_1w > ema10w}  weekly_allows_short={last_1w < ema10w}")

# Then at bottom of __main__ add:
# test_historical_htf("2026-04-04")
# test_historical_htf("2026-04-06")
# test_historical_htf("2026-03-30")

# ── Add this function ─────────────────────────────────────────────────────────
def test_historical_htf(target_date_str: str):
    import datetime
    print(f"\n{'='*60}")
    print(f"HISTORICAL — {target_date_str}")
    print(f"{'='*60}")

    target_ms = int(datetime.datetime.strptime(
        target_date_str, "%Y-%m-%d").timestamp() * 1000)

    url = f"{BASE_URL}/fapi/v1/klines"

    # 4H bars ending at target date
    r4  = requests.get(url, params={"symbol":"BTCUSDT","interval":"4h",
                                     "endTime":target_ms,"limit":220}, timeout=10)
    b4  = [{"c": float(x[4])} for x in r4.json()]

    # 1W bars ending at target date
    rw  = requests.get(url, params={"symbol":"BTCUSDT","interval":"1w",
                                     "endTime":target_ms,"limit":20}, timeout=10)
    bw  = [{"c": float(x[4])} for x in rw.json()]

    c4  = [b["c"] for b in b4]
    cw  = [b["c"] for b in bw]

    e200 = ema(c4, 200) if len(c4) >= 200 else 0.0
    e21  = ema(c4, 21)  if len(c4) >= 21  else 0.0
    e10w = ema(cw, 10)  if len(cw) >= 10  else 0.0

    last4  = c4[-1]
    lastw  = cw[-1]
    hh     = cw[-1] > cw[-2] if len(cw) >= 2 else True

    above_e200 = last4 > e200
    above_e21  = last4 > e21
    above_e10w = lastw > e10w

    print(f"  BTC 4H close     : ${last4:>10,.2f}")
    print(f"  4H EMA200        : ${e200:>10,.2f}  BTC {'ABOVE ✅' if above_e200 else 'BELOW ❌'}")
    print(f"  4H EMA21         : ${e21:>10,.2f}  htf_bull={above_e21}  htf_bear={not above_e21}")
    print(f"  Weekly close     : ${lastw:>10,.2f}")
    print(f"  10W EMA          : ${e10w:>10,.2f}  allows_long={above_e10w}  allows_short={not above_e10w}")
    print(f"  Weekly HH intact : {hh}")

    dr = "LONG" if (above_e200 and hh) else ("SHORT" if (not above_e200 and not hh) else "NONE")
    print(f"\n  direction_router  → {dr}")
    print(f"  br htf_bull       → {above_e21}  {'✅ LONG fires' if above_e21 else '❌ LONG blocked'}")
    print(f"  weekly_allows_long→ {above_e10w}  {'✅' if above_e10w else '❌ BLOCKED'}")

    if above_e21 and not above_e10w:
        print(f"\n  🔴 CONFLICT: br_scorer htf_bull=True BUT weekly gate blocks LONG")
        print(f"     Check config.yaml → weekly_trend_gate → apply_to list")
        print(f"     If 'breakout_retest' is NOT in apply_to, weekly gate is bypassed entirely")
    elif above_e21 and above_e10w:
        print(f"\n  ✅ Both allow LONG — LONGs on this date were correct per all gates")
    elif not above_e21:
        print(f"\n  🔴 htf_bull=False on this date — LONGs should have been BLOCKED")
        print(f"     But bot fired LONGs anyway → scorer bug or cache stale data")


# ── Replace the bottom of __main__ with this ─────────────────────────────────
if __name__ == "__main__":
    print("\n🔍 HTF FILTER LOCAL VERIFICATION")
    print(f"   Time: {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}")

    test_bar_counts()
    test_direction_router_ema200()
    test_weekly_trend_gate()
    test_br_scorer_htf()
    test_bug_diagnosis()

    # Historical check — the suspicious dates from live trade log
    print("\n\n📅 HISTORICAL HTF STATES (dates where LONGs fired into down moves)")
    for d in ["2026-03-28", "2026-03-30", "2026-04-03", "2026-04-06"]:
        test_historical_htf(d)

    print(f"\n{'='*60}")
    print("DONE — paste full output here")
    print(f"{'='*60}")