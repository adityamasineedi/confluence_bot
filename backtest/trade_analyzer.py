"""Trade analyzer -- post-hoc diagnosis of winning/losing trades from backtest engines.

Usage (standalone CLI):
    python -m backtest.trade_analyzer --strategy ema_pullback --symbol XRPUSDT
    python -m backtest.trade_analyzer --strategy fvg          --symbol BTCUSDT --regime TREND
    python -m backtest.trade_analyzer --strategy vwap_band    --symbol LINKUSDT

Usage (via run.py):
    python -m backtest.run --strategy ema_pullback --symbol XRPUSDT --analyze
    python -m backtest.run --strategy fvg          --symbol BTCUSDT --regime TREND --analyze
"""
from __future__ import annotations

import argparse
import bisect
import logging
import os
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from statistics import mean

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("trade_analyzer")


# -----------------------------------------------------------------------------
# TradeContext -- enriched view of one closed trade
# -----------------------------------------------------------------------------

@dataclass
class TradeContext:
    # Entry conditions
    entry_bar_idx:    int
    entry_ts:         int        # unix ms
    direction:        str        # LONG / SHORT
    regime:           str        # TREND / RANGE / BREAKOUT / PUMP / CRASH / etc.
    outcome:          str        # SL / TP / TIMEOUT

    # Price context at entry
    entry_price:      float
    sl_price:         float
    tp_price:         float
    sl_dist_pct:      float      # abs(entry - sl) / entry x 100
    rr_ratio:         float      # abs(tp - entry) / abs(entry - sl)

    # Market context at entry
    atr_pct:          float      # ATR(14) as % of price -- volatility proxy
    vol_ratio:        float      # entry bar volume / 20-bar avg
    rsi:              float      # 14-period RSI at entry
    btc_direction:    str        # 'up' / 'down' / 'flat'
    session:          str        # 'asia' / 'london' / 'newyork' / 'off'
    day_of_week:      str        # 'Mon' ... 'Sun'

    # How the loss happened
    bars_to_sl:       int        # bars between entry and exit (SL or otherwise)
    max_adverse_pct:  float      # worst price move against trade direction before exit
    max_favor_pct:    float      # best move in trade direction before exit
    sl_was_noise:     bool       # price recovered past entry within 3 bars of SL

    # Signal quality (strategy-specific)
    signals:          dict  = field(default_factory=dict)
    signal_count:     int   = 0
    score:            float = 0.0


# -----------------------------------------------------------------------------
# Pure math helpers
# -----------------------------------------------------------------------------

def _ema(closes: list[float], period: int) -> float:
    if len(closes) < period:
        return 0.0
    k   = 2.0 / (period + 1)
    ema = sum(closes[:period]) / period
    for c in closes[period:]:
        ema = c * k + ema * (1.0 - k)
    return ema


def _rsi(closes: list[float], period: int = 14) -> float:
    if len(closes) < period + 1:
        return 50.0
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    ag = sum(gains[-period:]) / period
    al = sum(losses[-period:]) / period or 1e-9
    return 100.0 - 100.0 / (1.0 + ag / al)


def _atr(bars: list[dict], period: int = 14) -> float:
    if len(bars) < period + 1:
        return 0.0
    trs = []
    for i in range(1, len(bars)):
        h, l, pc = bars[i]["h"], bars[i]["l"], bars[i - 1]["c"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(trs[-period:]) / min(period, len(trs)) if trs else 0.0


def _find_idx(bars: list[dict], ts_ms: int) -> int:
    """Binary-search for the last bar with ts <= ts_ms. Returns 0 on miss."""
    if not bars:
        return 0
    lo, hi = 0, len(bars) - 1
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if bars[mid]["ts"] <= ts_ms:
            lo = mid
        else:
            hi = mid - 1
    return lo


def _session(ts_ms: int) -> str:
    hour = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).hour
    if 1 <= hour < 8:
        return "asia"
    if 8 <= hour < 13:
        return "london"
    if 13 <= hour < 21:
        return "newyork"
    return "off"


def _day_of_week(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%a")


def _btc_dir(btc_1h_bars: list[dict], entry_ts: int) -> str:
    """EMA20 slope of BTC on 1h bars at entry_ts -> 'up' / 'down' / 'flat'."""
    if not btc_1h_bars:
        return "flat"
    idx = _find_idx(btc_1h_bars, entry_ts)
    sl  = btc_1h_bars[max(0, idx - 24): idx + 1]
    if len(sl) < 21:
        return "flat"
    closes = [b["c"] for b in sl]
    now_  = _ema(closes[-21:],        20)
    prev_ = _ema(closes[-22:-1], 20) if len(closes) >= 22 else now_
    if prev_ <= 0:
        return "flat"
    slope = (now_ - prev_) / prev_
    if slope >  0.0002:
        return "up"
    if slope < -0.0002:
        return "down"
    return "flat"


# -----------------------------------------------------------------------------
# Strategy-specific signal extraction
# -----------------------------------------------------------------------------

def _signals_ema_pullback(trade: dict, bars_15m: list[dict], bars_4h: list[dict]) -> dict:
    entry_ts  = trade["entry_ts"]
    direction = trade["direction"]
    idx = _find_idx(bars_15m, entry_ts)
    sl  = bars_15m[max(0, idx - 54): idx + 1]
    if len(sl) < 52:
        return {}

    closes    = [b["c"] for b in sl]
    ema21     = _ema(closes, 21)
    ema50     = _ema(closes, 50)
    rsi_val   = _rsi(closes)

    # 4H EMA alignment
    htf_idx    = _find_idx(bars_4h, entry_ts)
    htf_sl     = bars_4h[max(0, htf_idx - 54): htf_idx + 1]
    c4h        = [b["c"] for b in htf_sl]
    ema21_4h   = _ema(c4h, 21) if len(c4h) >= 21 else 0.0
    ema50_4h   = _ema(c4h, 50) if len(c4h) >= 50 else ema21_4h
    htf_aligned = (ema21_4h > ema50_4h) if direction == "LONG" else (ema21_4h < ema50_4h)

    # Pullback depth and bounce body
    prev_bar        = sl[-2] if len(sl) >= 2 else sl[-1]
    touch_depth_pct = (closes[-2] - ema21) / ema21 * 100 if ema21 > 0 else 0.0
    bounce_body_pct = abs(closes[-1] - sl[-1]["o"]) / sl[-1]["o"] * 100 if sl[-1]["o"] > 0 else 0.0

    # Volume: pullback quiet, bounce spike
    vol_sl         = sl[-21:]
    avg_vol        = sum(b["v"] for b in vol_sl[:-1]) / max(len(vol_sl) - 1, 1)
    vol_quiet_pull = (avg_vol == 0) or prev_bar["v"] <= avg_vol * 1.2
    vol_spike_bnc  = sl[-1]["v"] > prev_bar["v"]

    rsi_in_range = (30.0 <= rsi_val <= 50.0) if direction == "LONG" else (50.0 <= rsi_val <= 70.0)

    return {
        "htf_ema_aligned":     htf_aligned,
        "ema21_above_ema50":   ema21 > ema50,
        "rsi_in_range":        rsi_in_range,
        "vol_quiet_pullback":  vol_quiet_pull,
        "vol_spike_bounce":    vol_spike_bnc,
        # float context (not used for per-signal WR)
        "ema21_ema50_gap_pct": round((ema21 - ema50) / ema50 * 100, 3) if ema50 > 0 else 0.0,
        "touch_depth_pct":     round(touch_depth_pct, 3),
        "bounce_body_pct":     round(bounce_body_pct, 3),
    }


def _signals_fvg(trade: dict, bars_1h: list[dict], bars_4h: list[dict]) -> dict:
    entry_ts  = trade["entry_ts"]
    direction = trade["direction"]
    entry     = trade["entry"]
    idx = _find_idx(bars_1h, entry_ts)
    sl  = bars_1h[max(0, idx - 54): idx + 1]
    if len(sl) < 20:
        return {}

    closes  = [b["c"] for b in sl]
    rsi_val = _rsi(closes)

    # 4H EMA21 alignment
    htf_idx   = _find_idx(bars_4h, entry_ts)
    htf_sl    = bars_4h[max(0, htf_idx - 25): htf_idx + 1]
    c4h       = [b["c"] for b in htf_sl]
    ema21_4h  = _ema(c4h, 21) if len(c4h) >= 21 else 0.0
    htf_aligned = (c4h[-1] > ema21_4h) if (direction == "LONG" and c4h) else \
                  (c4h[-1] < ema21_4h) if c4h else False

    # VWAP position (20-bar rolling)
    band_sl   = sl[-20:]
    vol_total = sum(b["v"] for b in band_sl)
    if vol_total > 0:
        vwap = sum((b["h"] + b["l"] + b["c"]) / 3 * b["v"] for b in band_sl) / vol_total
    else:
        vwap = entry
    vwap_position = "above" if entry >= vwap else "below"

    # Momentum toward fill
    momentum_into_fill = False
    if len(closes) >= 3:
        momentum_into_fill = (closes[-1] > closes[-3]) if direction == "LONG" \
                             else (closes[-1] < closes[-3])

    # Gap metadata -- stored by some engines, else 0
    gap_size_pct   = float(trade.get("gap_size_pct",   0.0))
    bars_since_gap = int(trade.get("bars_since_gap",   0))

    vwap_aligned = (vwap_position == "below") if direction == "LONG" \
                   else (vwap_position == "above")

    return {
        "htf_ema_aligned":    htf_aligned,
        "rsi_confirmed":      rsi_val <= 45 if direction == "LONG" else rsi_val >= 55,
        "vwap_aligned":       vwap_aligned,
        "momentum_into_fill": momentum_into_fill,
        # float context
        "gap_size_pct":       round(gap_size_pct, 4),
        "bars_since_gap":     bars_since_gap,
        "vwap_position":      vwap_position,   # string -- excluded from WR calc
    }


def _signals_vwap_band(trade: dict, bars_15m: list[dict], bars_4h: list[dict]) -> dict:
    entry_ts  = trade["entry_ts"]
    direction = trade["direction"]
    entry     = trade["entry"]
    idx = _find_idx(bars_15m, entry_ts)
    sl  = bars_15m[max(0, idx - 25): idx + 1]
    if len(sl) < 22:
        return {}

    closes  = [b["c"] for b in sl]
    rsi_val = _rsi(closes)

    # Recompute VWAP bands (20-bar)
    band_sl   = sl[-21:]
    vol_total = sum(b["v"] for b in band_sl)
    band_touch_depth_pct = 0.0
    vwap_distance_pct    = 0.0
    bands_expanding      = False
    prior_band_tests     = 0

    if vol_total > 0:
        vwap = sum((b["h"] + b["l"] + b["c"]) / 3 * b["v"] for b in band_sl) / vol_total
        var  = sum(b["v"] * (((b["h"] + b["l"] + b["c"]) / 3) - vwap) ** 2 for b in band_sl) / vol_total
        std  = var ** 0.5
        lower = vwap - 2.0 * std
        upper = vwap + 2.0 * std
        vwap_distance_pct = abs(entry - vwap) / vwap * 100 if vwap > 0 else 0.0
        band_ref = lower if direction == "LONG" else upper
        band_touch_depth_pct = abs(entry - band_ref) / band_ref * 100 if band_ref > 0 else 0.0
        # Bands expanding = std growing relative to ATR
        atr_val = _atr(band_sl[-10:])
        atr_pct = atr_val / entry * 100 if entry > 0 else 0.0
        bands_expanding = std / entry * 100 > atr_pct * 0.7 if atr_pct > 0 else False
        # Prior band tests (recent touches at the same band without fill)
        for b in band_sl[:-3]:
            if direction == "LONG" and b["l"] <= lower * 1.002:
                prior_band_tests += 1
            elif direction == "SHORT" and b["h"] >= upper * 0.998:
                prior_band_tests += 1

    # Momentum decelerating at touch (good for reversion)
    momentum_decelerating = False
    if len(closes) >= 3:
        if direction == "LONG":
            momentum_decelerating = closes[-1] >= closes[-3]   # price stopped falling
        else:
            momentum_decelerating = closes[-1] <= closes[-3]   # price stopped rising

    rsi_extreme = rsi_val <= 35 if direction == "LONG" else rsi_val >= 65

    return {
        "rsi_extreme":            rsi_extreme,
        "momentum_decelerating":  momentum_decelerating,
        "bands_not_expanding":    not bands_expanding,
        "prior_tests_few":        prior_band_tests <= 2,
        # float context
        "band_touch_depth_pct":   round(band_touch_depth_pct, 3),
        "vwap_distance_pct":      round(vwap_distance_pct, 3),
        "prior_band_tests":       prior_band_tests,
    }


def _signals_microrange(trade: dict, bars_5m: list[dict]) -> dict:
    entry_ts  = trade["entry_ts"]
    direction = trade["direction"]
    idx = _find_idx(bars_5m, entry_ts)
    sl  = bars_5m[max(0, idx - 34): idx + 1]
    if len(sl) < 15:
        return {}

    closes    = [b["c"] for b in sl]
    rsi_val   = _rsi(closes)
    entry_bar = sl[-1]

    # Approximate box from last 10 closed bars
    box_sl   = sl[-11:-1]
    box_high = max(b["h"] for b in box_sl) if box_sl else 0.0
    box_low  = min(b["l"] for b in box_sl) if box_sl else 0.0

    near_low  = box_low  > 0 and abs(entry_bar["c"] - box_low)  / box_low  <= 0.003
    near_high = box_high > 0 and abs(entry_bar["c"] - box_high) / box_high <= 0.003

    # Volume filter
    vol_sl  = sl[-21:]
    avg_vol = sum(b["v"] for b in vol_sl[:-1]) / max(len(vol_sl) - 1, 1)
    low_vol = (avg_vol == 0) or entry_bar["v"] <= avg_vol * 1.3

    rsi_ok       = rsi_val <= 40 if direction == "LONG" else rsi_val >= 60
    box_range_pct = (box_high - box_low) / box_low * 100 if box_low > 0 else 0.0

    return {
        "near_range_boundary": near_low if direction == "LONG" else near_high,
        "rsi_confirms":        rsi_ok,
        "low_volume":          low_vol,
        "box_detected":        True,   # by definition (engine only trades in detected box)
        # float context
        "box_range_pct":       round(box_range_pct, 3),
    }


# -----------------------------------------------------------------------------
# Common enrichment
# -----------------------------------------------------------------------------

def _enrich_common(
    trade:    dict,
    bars:     list[dict],
    btc_1h:   list[dict],
    signals:  dict,
) -> TradeContext:
    """Build a TradeContext from raw trade + bars data."""
    entry_ts  = trade["entry_ts"]
    exit_ts   = trade.get("exit_ts", entry_ts)
    direction = trade["direction"]
    entry     = trade["entry"]
    sl        = trade["sl"]
    tp        = trade.get("tp", entry)

    # Normalise outcome to SL / TP / TIMEOUT
    raw_outcome = trade.get("outcome", "LOSS")
    outcome = {"WIN": "TP", "LOSS": "SL"}.get(raw_outcome, raw_outcome)

    # Locate entry and exit in primary bars
    entry_idx = _find_idx(bars, entry_ts)
    exit_idx  = _find_idx(bars, exit_ts)

    # SL distance and RR
    sl_dist    = abs(entry - sl)
    sl_dist_pct = sl_dist / entry * 100 if entry > 0 else 0.0
    tp_dist    = abs(tp - entry)
    rr_ratio   = tp_dist / sl_dist if sl_dist > 0 else 0.0

    # ATR (last 14 bars before entry)
    atr_sl  = bars[max(0, entry_idx - 20): entry_idx + 1]
    atr_val = _atr(atr_sl)
    atr_pct = atr_val / entry * 100 if entry > 0 else 0.0

    # Volume ratio
    entry_bar = bars[entry_idx] if entry_idx < len(bars) else None
    if entry_bar:
        vol_sl  = bars[max(0, entry_idx - 20): entry_idx]
        avg_vol = sum(b["v"] for b in vol_sl) / len(vol_sl) if vol_sl else 0.0
        vol_ratio = entry_bar["v"] / avg_vol if avg_vol > 0 else 1.0
    else:
        vol_ratio = 1.0

    # RSI
    rsi_sl  = [b["c"] for b in bars[max(0, entry_idx - 20): entry_idx + 1]]
    rsi_val = _rsi(rsi_sl)

    # Market context
    btc_direction = _btc_dir(btc_1h, entry_ts)
    session       = _session(entry_ts)
    dow           = _day_of_week(entry_ts)

    # Forward excursion scan (entry_idx+1 ... exit_idx inclusive)
    fwd = bars[entry_idx + 1: exit_idx + 1]
    bars_in_trade = max(0, exit_idx - entry_idx)
    max_adverse   = 0.0
    max_favor     = 0.0

    for fb in fwd:
        if direction == "LONG":
            adverse = (entry - fb["l"]) / entry * 100 if entry > 0 else 0.0
            favor   = (fb["h"] - entry) / entry * 100 if entry > 0 else 0.0
        else:
            adverse = (fb["h"] - entry) / entry * 100 if entry > 0 else 0.0
            favor   = (entry - fb["l"]) / entry * 100 if entry > 0 else 0.0
        max_adverse = max(max_adverse, max(adverse, 0.0))
        max_favor   = max(max_favor,   max(favor,   0.0))

    # Noise-stop: did price recover past entry within 3 bars after SL?
    sl_was_noise = False
    if outcome == "SL":
        post_sl = bars[exit_idx + 1: exit_idx + 4]
        for pb in post_sl:
            if direction == "LONG" and pb["h"] >= entry:
                sl_was_noise = True
                break
            if direction == "SHORT" and pb["l"] <= entry:
                sl_was_noise = True
                break

    # Signal count (boolean signals only)
    sig_count = sum(1 for v in signals.values() if isinstance(v, bool) and v)

    return TradeContext(
        entry_bar_idx   = entry_idx,
        entry_ts        = entry_ts,
        direction       = direction,
        regime          = trade.get("regime", "UNKNOWN"),
        outcome         = outcome,
        entry_price     = entry,
        sl_price        = sl,
        tp_price        = tp,
        sl_dist_pct     = round(sl_dist_pct,  4),
        rr_ratio        = round(rr_ratio,      3),
        atr_pct         = round(atr_pct,       4),
        vol_ratio       = round(vol_ratio,     3),
        rsi             = round(rsi_val,       1),
        btc_direction   = btc_direction,
        session         = session,
        day_of_week     = dow,
        bars_to_sl      = bars_in_trade,
        max_adverse_pct = round(max_adverse,   4),
        max_favor_pct   = round(max_favor,     4),
        sl_was_noise    = sl_was_noise,
        signals         = signals,
        signal_count    = sig_count,
        score           = float(trade.get("score", 0.0)),
    )


# -----------------------------------------------------------------------------
# Failure pattern classifier
# -----------------------------------------------------------------------------

def classify_failure(ctx: TradeContext) -> str:
    """Return the most likely root cause of a losing trade.  Priority: first match wins."""
    sl  = ctx.sl_dist_pct
    atr = ctx.atr_pct

    # -- 1. WRONG_DIRECTION ----------------------------------------------------
    # BTC EMA20 slope directly contradicts trade direction
    btc_contra = (
        (ctx.direction == "LONG"  and ctx.btc_direction == "down") or
        (ctx.direction == "SHORT" and ctx.btc_direction == "up")
    )
    if btc_contra:
        return "WRONG_DIRECTION"

    # -- 2. SL_TOO_TIGHT -------------------------------------------------------
    # SL was inside normal noise range (less than 80% of ATR)
    sl_inside_noise = atr > 0 and sl < atr * 0.8
    # Stopped out immediately (within 3 bars)
    fast_stop = ctx.bars_to_sl <= 3
    # Price recovered past entry after SL -- pure noise whipsaw
    noise_whipsaw = ctx.sl_was_noise
    # Trade went in our favour first, then reversed
    went_right_first = sl > 0 and ctx.max_favor_pct > sl and ctx.max_adverse_pct > sl

    if sl_inside_noise or fast_stop or noise_whipsaw or went_right_first:
        return "SL_TOO_TIGHT"

    # -- 3. BAD_ENTRY_TIMING ---------------------------------------------------
    low_vol_entry = ctx.vol_ratio < 0.7
    overbought_long  = ctx.direction == "LONG"  and ctx.rsi > 70
    oversold_short   = ctx.direction == "SHORT" and ctx.rsi < 30
    off_hours        = ctx.session == "off"

    if low_vol_entry or overbought_long or oversold_short or off_hours:
        return "BAD_ENTRY_TIMING"

    # -- 4. WRONG_MARKET_CONTEXT -----------------------------------------------
    # atr_pct > 2% = extremely volatile (3x threshold of ~0.6% typical for 15m)
    too_volatile = atr > 2.0
    weekend      = ctx.day_of_week in ("Sat", "Sun")

    if too_volatile or weekend:
        return "WRONG_MARKET_CONTEXT"

    # -- 5. WEAK_SIGNAL --------------------------------------------------------
    too_few_signals  = ctx.signal_count <= 2
    large_excursion  = sl > 0 and ctx.max_adverse_pct > sl * 2

    if too_few_signals or large_excursion:
        return "WEAK_SIGNAL"

    return "UNKNOWN"


# -----------------------------------------------------------------------------
# Analysis printer
# -----------------------------------------------------------------------------

def print_analysis(
    symbol:        str,
    strategy:      str,
    contexts:      list[TradeContext],
    regime_filter: str | None = None,
) -> None:
    if not contexts:
        print(f"\nNo trades to analyze for {symbol} x {strategy}")
        return

    losing  = [t for t in contexts if t.outcome == "SL"]
    winning = [t for t in contexts if t.outcome == "TP"]
    timeout = [t for t in contexts if t.outcome == "TIMEOUT"]
    total   = len(contexts)

    regime_note = f"  (regime filter: {regime_filter})" if regime_filter else ""
    print(f"\n{'='*62}")
    print(f"TRADE ANALYZER: {strategy.upper()} x {symbol}{regime_note}")
    print(f"  Total: {total}  TP: {len(winning)}  SL: {len(losing)}  Timeout: {len(timeout)}")
    print(f"  Win rate: {len(winning) / total * 100:.0f}%" if total > 0 else "  Win rate: N/A")
    print(f"{'='*62}")

    if not losing:
        print("\n  No losing (SL) trades in this sample -- nothing to diagnose.")
        return

    # -- Failure patterns ------------------------------------------------------
    pattern_counts = Counter(classify_failure(t) for t in losing)
    print(f"\n-- FAILURE PATTERNS ({len(losing)} losing trades) --")
    for pattern, count in pattern_counts.most_common():
        pct = count / len(losing) * 100
        print(f"  {pattern:<25}  {count:>3} trades  ({pct:.0f}%)")

    # -- Signal quality --------------------------------------------------------
    print(f"\n-- SIGNAL ANALYSIS --")
    if winning:
        print(f"  Avg score on WINS:   {mean(t.score for t in winning):.3f}")
    if losing:
        print(f"  Avg score on LOSSES: {mean(t.score for t in losing):.3f}")
    if winning:
        print(f"  Avg signals on wins:   {mean(t.signal_count for t in winning):.1f}")
    if losing:
        print(f"  Avg signals on losses: {mean(t.signal_count for t in losing):.1f}")

    # Per-signal win rate (boolean signals only)
    all_sig_keys = set()
    for t in contexts:
        for k, v in t.signals.items():
            if isinstance(v, bool):
                all_sig_keys.add(k)

    if all_sig_keys:
        print(f"\n-- SIGNAL WIN RATES (when signal=True) --")
        for sig in sorted(all_sig_keys):
            sig_wins   = [t for t in winning if t.signals.get(sig) is True]
            sig_losses = [t for t in losing  if t.signals.get(sig) is True]
            total_sig  = len(sig_wins) + len(sig_losses)
            if total_sig < 3:
                continue
            wr = len(sig_wins) / total_sig * 100
            bar = "#" * int(wr / 10) + "." * (10 - int(wr / 10))
            print(f"  {sig:<30}  WR={wr:.0f}%  {bar}  ({total_sig} trades)")

    # -- Market context on losses ----------------------------------------------
    print(f"\n-- MARKET CONTEXT ON LOSSES --")

    long_losses  = [t for t in losing if t.direction == "LONG"]
    short_losses = [t for t in losing if t.direction == "SHORT"]

    if long_losses:
        btc_up_l   = sum(1 for t in long_losses if t.btc_direction == "up")
        btc_down_l = sum(1 for t in long_losses if t.btc_direction == "down")
        print(f"  LONG losses when BTC going UP:   {btc_up_l}")
        print(f"  LONG losses when BTC going DOWN: {btc_down_l}  {'<- problem if high' if btc_down_l > btc_up_l else ''}")

    if short_losses:
        btc_up_s   = sum(1 for t in short_losses if t.btc_direction == "up")
        btc_down_s = sum(1 for t in short_losses if t.btc_direction == "down")
        print(f"  SHORT losses when BTC going UP:   {btc_up_s}  {'<- problem if high' if btc_up_s > btc_down_s else ''}")
        print(f"  SHORT losses when BTC going DOWN: {btc_down_s}")

    print(f"\n  Session breakdown of losses:")
    for sess in ("asia", "london", "newyork", "off"):
        cnt = sum(1 for t in losing if t.session == sess)
        pct = cnt / len(losing) * 100 if losing else 0.0
        bar = "#" * int(pct / 10 + 0.5)
        print(f"    {sess:<10}  {cnt:>3} ({pct:.0f}%)  {bar}")

    print(f"\n  Day of week losses:")
    dow_line = ""
    for day in ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"):
        cnt = sum(1 for t in losing if t.day_of_week == day)
        dow_line += f"  {day}: {cnt}"
    print(f"   {dow_line}")

    # -- SL analysis -----------------------------------------------------------
    avg_sl_w = mean(t.sl_dist_pct for t in winning) if winning else 0.0
    avg_sl_l = mean(t.sl_dist_pct for t in losing)
    avg_atr  = mean(t.atr_pct     for t in losing)
    noise_n  = sum(1 for t in losing if t.sl_was_noise)

    print(f"\n-- SL ANALYSIS --")
    print(f"  Avg SL dist on wins:   {avg_sl_w:.3f}%")
    print(f"  Avg SL dist on losses: {avg_sl_l:.3f}%")
    print(f"  Avg ATR on losses:     {avg_atr:.3f}%")
    inside_noise = avg_sl_l < avg_atr
    print(f"  SL inside ATR noise:   {inside_noise}  {'<- SL too tight' if inside_noise else ''}")
    print(f"  Noise stops (recovered after SL): {noise_n}/{len(losing)}")

    # -- Loss speed ------------------------------------------------------------
    fast_stops = sum(1 for t in losing if t.bars_to_sl <= 3)
    print(f"\n-- LOSS SPEED --")
    print(f"  Stopped within 3 bars: {fast_stops}/{len(losing)}  {'<- wrong direction if high' if fast_stops > len(losing) // 3 else ''}")
    print(f"  Avg bars in trade (loss): {mean(t.bars_to_sl for t in losing):.1f}")
    print(f"  Avg max adverse move:  {mean(t.max_adverse_pct for t in losing):.3f}%")
    print(f"  Avg max favor before exit: {mean(t.max_favor_pct for t in losing):.3f}%")

    # -- Top 5 worst trades ----------------------------------------------------
    print(f"\n-- TOP 5 WORST TRADES --")
    worst = sorted(losing, key=lambda t: t.max_adverse_pct, reverse=True)[:5]
    for t in worst:
        ts_str = datetime.fromtimestamp(t.entry_ts / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
        print(
            f"  {t.direction:<6} {ts_str}"
            f"  entry={t.entry_price:.4f}"
            f"  sl_dist={t.sl_dist_pct:.3f}%"
            f"  adverse={t.max_adverse_pct:.3f}%"
            f"  bars={t.bars_to_sl}"
            f"  {t.session}/{t.day_of_week}"
            f"  btc={t.btc_direction}"
            f"  [{classify_failure(t)}]"
        )

    # -- Actionable diagnosis --------------------------------------------------
    top_pattern = pattern_counts.most_common(1)[0][0]
    top_count   = pattern_counts.most_common(1)[0][1]
    top_pct     = top_count / len(losing) * 100

    print(f"\n-- DIAGNOSIS --")
    print(f"  Primary failure mode: {top_pattern}  ({top_count}/{len(losing)} = {top_pct:.0f}% of losses)")

    fixes: dict[str, list[str]] = {
        "WRONG_DIRECTION": [
            "Add BTC direction filter -- only LONG when BTC 1h EMA20 slope is positive",
            "Add session-open day-bias check -- skip entries where daily open > close (bearish day)",
            f"  {sum(1 for t in losing if t.btc_direction == 'down' and t.direction == 'LONG')} LONG losses had BTC trending down",
        ],
        "SL_TOO_TIGHT": [
            f"Current avg SL: {avg_sl_l:.3f}%  vs  ATR: {avg_atr:.3f}%  (should be >= ATR x 1.0)",
            f"Minimum SL recommendation: {avg_atr * 1.2:.3f}% (ATR x 1.2)",
            "Switch to ATR-based SL -- sl = entry +/- atr x multiplier instead of fixed %",
            f"{noise_n}/{len(losing)} trades recovered after SL -- wider SL would have saved them",
        ],
        "BAD_ENTRY_TIMING": [
            "Add RVOL gate: require vol_ratio >= 0.8 at entry (skip low-momentum entries)",
            "Tighten RSI entry window -- for LONG: rsi <= 55 (avoid near-overbought entries)",
            "Add session filter: disable during 'off' hours (21:00-01:00 UTC)",
        ],
        "WRONG_MARKET_CONTEXT": [
            f"Add ATR volatility gate: skip if atr_pct > {avg_atr * 2.0:.2f}% (2x avg)",
            "Disable on Sat/Sun -- weekend low-volume amplifies whipsaws",
            "Cross-check BTC regime before entering alt setups (alt LONG in BTC crash = bad)",
        ],
        "WEAK_SIGNAL": [
            "Raise score threshold or require >= 3 boolean signals True before entry",
            f"Avg signal count on losses: {mean(t.signal_count for t in losing):.1f}  vs wins: {mean(t.signal_count for t in winning):.1f}" if winning else "",
            "Review per-signal WR above -- remove signals with WR < overall WR",
        ],
        "UNKNOWN": [
            "No dominant pattern -- losses may be random variance in a valid strategy",
            "Increase sample size (run longer date range) before drawing conclusions",
        ],
    }

    for fix in fixes.get(top_pattern, []):
        if fix:
            print(f"  -> {fix}")

    print(f"\n{'-'*62}\n")


# -----------------------------------------------------------------------------
# Main entry point (called from run.py or directly)
# -----------------------------------------------------------------------------

#: Maps strategy name -> primary OHLCV timeframe key suffix
_STRATEGY_TF: dict[str, str] = {
    "ema_pullback": "15m",
    "vwap_band":    "15m",
    "fvg":          "1h",
    "microrange":   "5m",
    "sweep":        "15m",
    "zone":         "4h",
    "bos":          "1h",
    "leadlag":      "5m",
}


def analyze(
    symbol:        str,
    strategy:      str,
    trades:        list[dict],
    ohlcv:         dict,
    regime_filter: str | None = None,
) -> None:
    """Enrich trade list with diagnostic context and print the full analysis.

    Parameters
    ----------
    symbol        : symbol to analyze (e.g. 'XRPUSDT')
    strategy      : engine name (e.g. 'ema_pullback', 'fvg', 'vwap_band', 'microrange')
    trades        : closed trade list from engine.run()
    ohlcv         : {'SYMBOL:TF': [bar, ...]} from fetcher
    regime_filter : optional regime to limit analysis to (e.g. 'TREND')
    """
    sym_trades = [t for t in trades if t.get("symbol") == symbol]
    if not sym_trades:
        # Engines don't always tag symbol -- fall through with all trades
        sym_trades = trades

    if regime_filter:
        sym_trades = [t for t in sym_trades
                      if t.get("regime", "").upper() == regime_filter.upper()]

    if not sym_trades:
        print(f"\nNo trades for {symbol} (strategy={strategy}, regime={regime_filter})")
        return

    primary_tf   = _STRATEGY_TF.get(strategy, "1h")
    primary_bars = ohlcv.get(f"{symbol}:{primary_tf}", [])
    bars_4h      = ohlcv.get(f"{symbol}:4h",  [])
    btc_1h       = ohlcv.get("BTCUSDT:1h",   [])

    if not primary_bars:
        print(f"\nNo {primary_tf} bars for {symbol} in ohlcv -- cannot analyze")
        return

    contexts: list[TradeContext] = []
    for trade in sym_trades:
        try:
            if strategy == "ema_pullback":
                signals = _signals_ema_pullback(trade, primary_bars, bars_4h)
            elif strategy == "fvg":
                signals = _signals_fvg(trade, primary_bars, bars_4h)
            elif strategy == "vwap_band":
                signals = _signals_vwap_band(trade, primary_bars, bars_4h)
            elif strategy == "microrange":
                signals = _signals_microrange(trade, primary_bars)
            else:
                signals = {}

            ctx = _enrich_common(trade, primary_bars, btc_1h, signals)
            contexts.append(ctx)
        except Exception as exc:
            log.debug("Skipping trade (enrichment error): %s", exc)

    print_analysis(symbol, strategy, contexts, regime_filter)


# -----------------------------------------------------------------------------
# Standalone CLI
# -----------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Trade analyzer -- diagnose why each trade won/lost",
    )
    parser.add_argument("--strategy",
        choices=list(_STRATEGY_TF.keys()),
        required=True,
        help="Strategy engine to analyze")
    parser.add_argument("--symbol",  required=True,
        help="Symbol to analyze (e.g. XRPUSDT)")
    parser.add_argument("--regime",  default=None,
        help="Limit analysis to one regime (e.g. TREND, RANGE, BREAKOUT)")
    parser.add_argument("--from-date", dest="from_date", default="2024-01-01",
        help="Backtest start date YYYY-MM-DD (default: 2024-01-01)")
    parser.add_argument("--to-date",   dest="to_date",   default="2025-03-01",
        help="Backtest end date YYYY-MM-DD (default: 2025-03-01)")
    parser.add_argument("--capital",   type=float, default=1000.0)
    parser.add_argument("--risk-pct",  type=float, default=0.01, dest="risk_pct")
    args = parser.parse_args()

    symbol   = args.symbol.upper()
    strategy = args.strategy

    # Fetch data
    from datetime import datetime, timezone as _tz
    def _to_ms(d: str) -> int:
        return int(datetime.strptime(d, "%Y-%m-%d").replace(tzinfo=_tz.utc).timestamp() * 1000)

    from_ms = _to_ms(args.from_date)
    to_ms   = _to_ms(args.to_date) + 86_400_000

    # Include BTCUSDT for BTC direction signal
    symbols = [symbol]
    if symbol != "BTCUSDT":
        symbols = ["BTCUSDT", symbol]

    print(f"Fetching {symbol} + BTCUSDT data ({args.from_date} -> {args.to_date})...")
    from backtest.fetcher import fetch_period_sync
    data  = fetch_period_sync(symbols, from_ms, to_ms, warmup_days=45)
    ohlcv = data["ohlcv"]

    # Run engine
    print(f"Running {strategy} engine...")
    if strategy == "ema_pullback":
        from backtest.ema_pullback_engine import run
    elif strategy == "fvg":
        from backtest.fvg_engine import run
    elif strategy == "vwap_band":
        from backtest.vwap_band_engine import run
    elif strategy == "microrange":
        from backtest.microrange_engine import run
    elif strategy == "sweep":
        from backtest.sweep_engine import run
    elif strategy == "zone":
        from backtest.zone_engine import run
    elif strategy == "bos":
        from backtest.bos_engine import run
    else:
        print(f"Engine '{strategy}' not wired in CLI. Add it to trade_analyzer.main().")
        return

    sym_ohlcv = {k: v for k, v in ohlcv.items() if k.startswith(f"{symbol}:")}
    if strategy == "leadlag" and symbol != "BTCUSDT":
        sym_ohlcv.update({k: v for k, v in ohlcv.items() if k.startswith("BTCUSDT:")})
        run_symbols = ["BTCUSDT", symbol]
    else:
        run_symbols = [symbol]

    trades = run(symbols=run_symbols, ohlcv=sym_ohlcv,
                 starting_capital=args.capital, risk_pct=args.risk_pct)

    print(f"Engine produced {len(trades)} trades.")
    analyze(
        symbol        = symbol,
        strategy      = strategy,
        trades        = trades,
        ohlcv         = ohlcv,
        regime_filter = args.regime,
    )


if __name__ == "__main__":
    main()
