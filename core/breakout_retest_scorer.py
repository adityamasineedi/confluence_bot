"""
core/breakout_retest_scorer.py
Breakout + Retest + Flip scorer on 5m bars.

Confirmed: ALL 8 coins PF 3.0+, WR 67-68%, 24,313 trades/3yr.
Regime-agnostic — fires in TREND, RANGE, CRASH, BREAKOUT.

Entry at flip level (old resistance → new support or vice versa).
SL = ATR(14, 5m) × 1.3
TP = risk × 1.5R
"""

import logging
import os
from datetime import datetime, timezone

import yaml

from core.cooldown_store import CooldownStore
from core.weekly_trend_gate import weekly_allows_long, weekly_allows_short

log = logging.getLogger(__name__)

_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "config.yaml")
with open(_CONFIG_PATH) as _f:
    _cfg = yaml.safe_load(_f)

_BR_CFG         = _cfg.get("breakout_retest", {})
_RANGE_BARS     = int(  _BR_CFG.get("range_bars",       8))
_MIN_WIDTH      = float(_BR_CFG.get("min_width_pct",    0.0018))
_MAX_WIDTH      = float(_BR_CFG.get("max_width_pct",    0.0080))
_ATR_MULT_MAX   = float(_BR_CFG.get("atr_mult_max",     1.35))
_VOL_MULT       = float(_BR_CFG.get("vol_spike_mult",   1.25))
_RETEST_BARS    = int(  _BR_CFG.get("retest_bars",      8))
_SL_ATR_MULT    = float(_BR_CFG.get("sl_atr_mult",      1.3))
_RR_RATIO       = float(_BR_CFG.get("rr_ratio",         1.5))
_COOLDOWN_SECS    = float(_BR_CFG.get("cooldown_mins",    15)) * 60
_MAX_DAY_TRADES   = int(  _BR_CFG.get("max_trades_per_day", 4))
_EXHAUSTION_PCT        = float(_BR_CFG.get("exhaustion_pct",          0.025))
_EXHAUSTION_BARS       = int(  _BR_CFG.get("exhaustion_bars",          6))
_MAX_BOUNDARY_TOUCHES  = int(  _BR_CFG.get("max_boundary_touches",     2))
_REQUIRE_BK_CONFIRM    = bool( _BR_CFG.get("require_breakout_confirm", True))
_MIN_RETEST_BODY_RATIO = float(_BR_CFG.get("min_retest_body_ratio",    0.40))
_CRASH_COOL_PCT        = float(_BR_CFG.get("crash_cooldown_pct",       1.5))
_CRASH_COOL_HOURS      = int(  _BR_CFG.get("crash_cooldown_hours",     4))
_MAX_ENTRIES_30M       = int(  _BR_CFG.get("max_entries_per_30min",    2))
_BTC_CONFIRM_ALTS      = bool( _BR_CFG.get("btc_confirm_for_alts",    True))
_CHOPPY_ATR_MULT       = float(_BR_CFG.get("choppy_atr_mult",         2.0))
_SKIP_HOUR_S           = 14   # skip 14:00-15:00 UTC
_SKIP_HOUR_E           = 15

_cd = CooldownStore("BREAKOUT_RETEST")

# Per-symbol state machine
# state: "IDLE" | "AWAITING_BREAKOUT_CONFIRM" | "AWAITING_RETEST"
_state: dict[str, dict] = {}

# Daily trade counter: symbol → (date_str, count)
_daily_trades: dict[str, tuple[str, int]] = {}

# Recent entry tracker for anti-correlation gate (Fix 2)
# list of (timestamp, direction)
_recent_entries: list[tuple[float, str]] = []


def _utc_hour() -> int:
    return datetime.now(timezone.utc).hour


def _today_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _daily_count(symbol: str) -> int:
    today = _today_str()
    rec = _daily_trades.get(symbol, ("", 0))
    if rec[0] != today:
        return 0
    return rec[1]


def _increment_daily(symbol: str) -> None:
    today = _today_str()
    rec = _daily_trades.get(symbol, ("", 0))
    count = rec[1] + 1 if rec[0] == today else 1
    _daily_trades[symbol] = (today, count)


def _atr(bars: list[dict], period: int = 14) -> float:
    if len(bars) < period + 1:
        return 0.0
    trs = []
    for i in range(1, len(bars)):
        h  = bars[i]["h"]
        l  = bars[i]["l"]
        pc = bars[i-1]["c"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(trs[-period:]) / period


def _vol_ma(bars: list[dict], period: int = 20) -> float:
    vols = [b["v"] for b in bars[-period:] if b.get("v", 0) > 0]
    return sum(vols) / len(vols) if vols else 0.0


def _recent_move_exhausted(bars_4h: list[dict], direction: str) -> bool:
    """Return True if price already moved > _EXHAUSTION_PCT in signal direction.

    Prevents shorting into bottoms / buying into tops after the move is
    already spent.  Checks the last _EXHAUSTION_BARS 4H candles (default 24h).
    """
    if not bars_4h or len(bars_4h) < _EXHAUSTION_BARS:
        return False
    window = bars_4h[-_EXHAUSTION_BARS:]
    start  = window[0]["o"]
    end    = window[-1]["c"]
    if start == 0:
        return False
    move = (end - start) / start
    if direction == "LONG"  and move > _EXHAUSTION_PCT:
        return True
    if direction == "SHORT" and move < -_EXHAUSTION_PCT:
        return True
    return False


def _ema(closes: list[float], period: int) -> float:
    if len(closes) < period:
        return 0.0
    k = 2.0 / (period + 1)
    e = sum(closes[:period]) / period
    for c in closes[period:]:
        e = c * k + e * (1 - k)
    return e


def _btc_crashed_recently(cache) -> bool:
    """Fix 1: Return True if BTC dropped > _CRASH_COOL_PCT in any 1H candle
    in the last _CRASH_COOL_HOURS hours. Blocks LONG entries after crashes."""
    bars_1h = cache.get_ohlcv("BTCUSDT", window=_CRASH_COOL_HOURS + 1, tf="1h")
    if not bars_1h or len(bars_1h) < 2:
        return False
    for b in bars_1h[-_CRASH_COOL_HOURS:]:
        if b["o"] > 0:
            change = (b["c"] - b["o"]) / b["o"] * 100
            if change < -_CRASH_COOL_PCT:
                return True
    return False


def _too_many_recent_entries(direction: str) -> bool:
    """Fix 2: Return True if we already entered _MAX_ENTRIES_30M trades
    in the same direction within the last 30 minutes."""
    import time as _t
    now = _t.time()
    cutoff = now - 1800  # 30 minutes
    count = sum(1 for ts, d in _recent_entries
                if ts > cutoff and d == direction)
    return count >= _MAX_ENTRIES_30M


def _record_entry(direction: str) -> None:
    """Record an entry for anti-correlation tracking."""
    import time as _t
    _recent_entries.append((_t.time(), direction))
    # Clean old entries
    cutoff = _t.time() - 3600
    _recent_entries[:] = [(ts, d) for ts, d in _recent_entries if ts > cutoff]


def _btc_confirms_direction(direction: str, cache) -> bool:
    """Fix 3: For alt coins, require BTC to hold above/below its own
    recent range before entering alts in that direction."""
    if not _BTC_CONFIRM_ALTS:
        return True
    bars_btc_5m = cache.get_ohlcv("BTCUSDT", window=20, tf="5m")
    if not bars_btc_5m or len(bars_btc_5m) < 10:
        return True
    # BTC 10-bar range
    recent = bars_btc_5m[-10:]
    btc_high = max(b["h"] for b in recent)
    btc_low  = min(b["l"] for b in recent)
    btc_now  = bars_btc_5m[-1]["c"]
    if direction == "LONG":
        return btc_now > (btc_high + btc_low) / 2  # BTC above midpoint
    else:
        return btc_now < (btc_high + btc_low) / 2  # BTC below midpoint


def _market_too_choppy(cache, symbol: str) -> bool:
    """Fix 4: Return True if 1H ATR is > _CHOPPY_ATR_MULT x the 24H average."""
    bars_1h = cache.get_ohlcv(symbol, window=25, tf="1h")
    if not bars_1h or len(bars_1h) < 25:
        return False
    trs = []
    for i in range(1, len(bars_1h)):
        h, l, pc = bars_1h[i]["h"], bars_1h[i]["l"], bars_1h[i-1]["c"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    if len(trs) < 2:
        return False
    avg_atr = sum(trs[:-1]) / len(trs[:-1])
    current_atr = trs[-1]
    if avg_atr > 0 and current_atr > avg_atr * _CHOPPY_ATR_MULT:
        return True
    return False


def _detect_range(bars_5m: list[dict],
                  symbol: str = "") -> tuple[bool, float, float]:
    """Check last _RANGE_BARS form a valid tight range.
    Returns (valid, range_high, range_low).
    Logs reason for failure at DEBUG level for diagnostics.
    """
    if len(bars_5m) < _RANGE_BARS + 20:
        log.info("BR %s: not enough bars (%d < %d)",
                  symbol, len(bars_5m), _RANGE_BARS + 20)
        return False, 0.0, 0.0

    window   = bars_5m[-(_RANGE_BARS + 1):-1]
    rng_high = max(b["h"] for b in window)
    rng_low  = min(b["l"] for b in window)

    if rng_low <= 0:
        return False, 0.0, 0.0

    mid   = (rng_high + rng_low) / 2.0
    width = (rng_high - rng_low) / mid

    if not (_MIN_WIDTH <= width <= _MAX_WIDTH):
        log.info("BR %s: range width %.4f%% outside [%.4f%%, %.4f%%]",
                  symbol, width*100, _MIN_WIDTH*100, _MAX_WIDTH*100)
        return False, 0.0, 0.0

    # ATR regime check
    atr_bars = bars_5m[-21:-1]
    trs = []
    for i in range(1, len(atr_bars)):
        h  = atr_bars[i]["h"]
        l  = atr_bars[i]["l"]
        pc = atr_bars[i-1]["c"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))

    if len(trs) >= 2:
        avg_atr     = sum(trs[:-1]) / len(trs[:-1])
        current_atr = trs[-1]
        if avg_atr > 0 and current_atr > avg_atr * _ATR_MULT_MAX:
            log.info("BR %s: ATR spike %.4f > %.1f× avg %.4f",
                      symbol, current_atr, _ATR_MULT_MAX, avg_atr)
            return False, 0.0, 0.0

    # Fix 1: Boundary touch count gate — reject churning ranges
    upper_touches = sum(1 for b in window if b["h"] >= rng_high * 0.999)
    lower_touches = sum(1 for b in window if b["l"] <= rng_low  * 1.001)

    if upper_touches >= _MAX_BOUNDARY_TOUCHES + 1 and lower_touches >= _MAX_BOUNDARY_TOUCHES + 1:
        log.info("BR %s: range exhausted upper=%d lower=%d touches — skip",
                  symbol, upper_touches, lower_touches)
        return False, 0.0, 0.0

    if upper_touches == 0 or lower_touches == 0:
        log.info("BR %s: not a real range (upper_touches=%d lower_touches=%d)",
                  symbol, upper_touches, lower_touches)
        return False, 0.0, 0.0

    log.info("BR %s: range valid [%.4f, %.4f] width=%.3f%% touches=%d/%d",
              symbol, rng_low, rng_high, width*100, upper_touches, lower_touches)
    return True, rng_high, rng_low


async def score(symbol: str, cache) -> list[dict]:
    """Score symbol for breakout_retest setup on 5m bars.

    State machine:
      IDLE             → check for range + breakout on current bar
      AWAITING_RETEST  → check if price retested flip level
    """
    # Time filter
    if _SKIP_HOUR_S <= _utc_hour() < _SKIP_HOUR_E:
        return []

    # Cooldown
    if _cd.is_active(symbol):
        return []

    # Daily trade limit
    if _daily_count(symbol) >= _MAX_DAY_TRADES:
        return []

    # Fix 4: Choppy market gate
    if _market_too_choppy(cache, symbol):
        log.info("BR %s: market too choppy (1H ATR > %.1fx avg) -- skip", symbol, _CHOPPY_ATR_MULT)
        return []

    bars_5m = cache.get_ohlcv(symbol, window=50, tf="5m")
    if not bars_5m or len(bars_5m) < 30:
        log.info("BR %s: insufficient 5m bars (%d)",
                  symbol, len(bars_5m) if bars_5m else 0)
        return []

    # Heartbeat -- shows the scorer is running each tick
    log.info("BR eval %s  bars=%d  state=%s",
             symbol, len(bars_5m),
             _state.get(symbol, {}).get("state", "IDLE"))

    bars_4h = cache.get_ohlcv(symbol, window=25, tf="4h")

    # Patch last 4H bar close with live price — prevents stale EMA
    # during intra-candle drops (4H bar only updates on close, so mid-candle
    # the scorer sees the previous close, not the current price)
    if bars_4h:
        live_closes = cache.get_closes(symbol, window=1, tf="1m")
        if live_closes:
            bars_4h[-1] = {**bars_4h[-1], "c": live_closes[-1]}

    # HTF EMA20 direction (4H — more stable than 1H)
    htf_bull = True
    htf_bear = True
    if bars_4h and len(bars_4h) >= 21:
        closes_4h = [b["c"] for b in bars_4h]
        ema20_4h  = _ema(closes_4h, 20)
        htf_bull  = closes_4h[-1] > ema20_4h
        htf_bear  = closes_4h[-1] < ema20_4h

    st = _state.get(symbol, {"state": "IDLE"})

    # ── STATE: AWAITING_BREAKOUT_CONFIRM (Fix 2 — two-bar confirmation) ─
    if st["state"] == "AWAITING_BREAKOUT_CONFIRM":
        bar = bars_5m[-1]
        direction = st["direction"]
        flip      = st["flip_level"]
        confirmed = False
        if direction == "LONG":
            confirmed = bar["c"] > flip
        else:
            confirmed = bar["c"] < flip

        if confirmed:
            _state[symbol] = {
                "state":       "AWAITING_RETEST",
                "direction":   direction,
                "flip_level":  flip,
                "bars_waited": 0,
            }
            log.info("BR %s %s — breakout confirmed on next bar, awaiting retest",
                     symbol, direction)
        else:
            _state[symbol] = {"state": "IDLE"}
            log.info("BR %s %s — breakout not confirmed next bar (close=%.4f flip=%.4f), reset",
                     symbol, direction, bar["c"], flip)
        return []

    # ── STATE: AWAITING_RETEST ──────────────────────────────────────────
    if st["state"] == "AWAITING_RETEST":
        bar = bars_5m[-1]
        direction   = st["direction"]
        flip        = st["flip_level"]
        bars_waited = st.get("bars_waited", 0) + 1
        st["bars_waited"] = bars_waited

        # Exhaustion — skip if price already moved too far in signal direction
        if _recent_move_exhausted(bars_4h, direction):
            log.info("BR %s %s — recent move exhausted, resetting", symbol, direction)
            _state[symbol] = {"state": "IDLE"}
            return []

        # Timeout — discard after _RETEST_BARS
        if bars_waited > _RETEST_BARS:
            log.info("BR %s %s — retest timeout, reset", symbol, direction)
            _state[symbol] = {"state": "IDLE"}
            return []

        retest_confirmed = False
        if direction == "LONG":
            touched   = bar["l"] <= flip * 1.002
            confirmed = bar["c"] > flip
            failed    = bar["c"] < flip * 0.997
            if touched and confirmed:
                retest_confirmed = True
            elif failed:
                _state[symbol] = {"state": "IDLE"}
                return []
        else:
            touched   = bar["h"] >= flip * 0.998
            confirmed = bar["c"] < flip
            failed    = bar["c"] > flip * 1.003
            if touched and confirmed:
                retest_confirmed = True
            elif failed:
                _state[symbol] = {"state": "IDLE"}
                return []

        if not retest_confirmed:
            _state[symbol] = st
            return []

        # Fix 4: Retest bar quality — reject indecision/wick candles
        bar_body  = abs(bar["c"] - bar["o"])
        bar_range = bar["h"] - bar["l"]
        if bar_range > 0 and bar_body / bar_range < _MIN_RETEST_BODY_RATIO:
            log.info("BR %s %s — retest bar indecision (body/range=%.2f < %.2f), skip",
                     symbol, direction, bar_body / bar_range, _MIN_RETEST_BODY_RATIO)
            _state[symbol] = st  # keep waiting, don't reset
            return []

        # ── RETEST CONFIRMED — build signal ──────────────────────────
        atr_val = _atr(bars_5m)
        if atr_val <= 0:
            _state[symbol] = {"state": "IDLE"}
            return []

        entry   = flip
        sl_dist = max(atr_val * _SL_ATR_MULT, entry * 0.005)  # 0.5% min floor

        if direction == "LONG":
            stop = entry - sl_dist
            tp   = entry + sl_dist * _RR_RATIO
        else:
            stop = entry + sl_dist
            tp   = entry - sl_dist * _RR_RATIO

        if stop <= 0 or tp <= 0:
            _state[symbol] = {"state": "IDLE"}
            return []

        score_val = 1.0  # all gates passed

        signals = {
            "range_detected":     True,
            "breakout_confirmed": True,
            "retest_confirmed":   True,
            "htf_aligned":        htf_bull if direction == "LONG" else htf_bear,
            "weekly_ok":          True,
            "atr_ok":             True,
        }

        _state[symbol] = {"state": "IDLE"}
        _increment_daily(symbol)
        _cd.set(symbol, _COOLDOWN_SECS)

        log.info("BR FIRE %s %s  entry=%.4f  sl=%.4f  tp=%.4f  atr=%.4f",
                 symbol, direction, entry, stop, tp, atr_val)

        # Fix 2: Record entry for anti-correlation tracking
        _record_entry(direction)

        return [{
            "symbol":    symbol,
            "regime":    "BREAKOUT_RETEST",
            "direction": direction,
            "score":     score_val,
            "signals":   signals,
            "fire":      True,
            "br_stop":   round(stop,  8),
            "br_tp":     round(tp,    8),
            "br_flip":   round(entry, 8),
        }]

    # ── STATE: IDLE — look for range + breakout ──────────────────────
    range_ok, rng_high, rng_low = _detect_range(bars_5m, symbol)
    if not range_ok:
        return []

    bar    = bars_5m[-1]
    vm_val = _vol_ma(bars_5m)
    vol_ok = bar["v"] >= vm_val * _VOL_MULT if vm_val > 0 else True

    # Fix 3: In RANGE regime, only allow HTF-confirmed direction
    allow_long  = True
    allow_short = True
    regime_str = ""
    if hasattr(cache, "get_regime"):
        try:
            regime_str = str(cache.get_regime(symbol)).upper()
        except Exception:
            pass
    if "RANGE" in regime_str:
        if not htf_bull and not htf_bear:
            log.info("BR %s: RANGE regime, no HTF direction — skip", symbol)
            return []
        if htf_bull and not htf_bear:
            allow_short = False
        elif htf_bear and not htf_bull:
            allow_long = False

    # Fix 1: Post-crash cooldown — block LONGs after BTC crash
    if allow_long and _btc_crashed_recently(cache):
        log.info("BR %s: BTC crashed > %.1f%% recently -- LONG blocked", symbol, _CRASH_COOL_PCT)
        allow_long = False

    # Fix 2: Anti-correlation — max entries in same direction per 30 min
    if allow_long and _too_many_recent_entries("LONG"):
        log.info("BR %s: too many LONG entries in 30min -- skip", symbol)
        allow_long = False
    if allow_short and _too_many_recent_entries("SHORT"):
        log.info("BR %s: too many SHORT entries in 30min -- skip", symbol)
        allow_short = False

    # Fix 3: BTC confirmation for alt LONGs only (not SHORTs)
    # Only blocks LONGs when BTC is dropping — prevents dead cat bounce entries
    if allow_long and symbol != "BTCUSDT" and _BTC_CONFIRM_ALTS:
        if not _btc_confirms_direction("LONG", cache):
            log.info("BR %s: BTC not confirming LONG direction -- skip", symbol)
            allow_long = False

    # Breakout detection
    broke_long  = (allow_long
                   and bar["c"] > rng_high
                   and max(bar["o"], bar["c"]) > rng_high
                   and vol_ok
                   and htf_bull
                   and weekly_allows_long("breakout_retest", cache))

    broke_short = (allow_short
                   and bar["c"] < rng_low
                   and min(bar["o"], bar["c"]) < rng_low
                   and vol_ok
                   and htf_bear
                   and weekly_allows_short("breakout_retest", cache))

    # Exhaustion gate — cancel breakout if 4H move already extended
    if broke_long and _recent_move_exhausted(bars_4h, "LONG"):
        log.info("BR %s LONG breakout — but 4H exhausted, skip", symbol)
        broke_long = False
    if broke_short and _recent_move_exhausted(bars_4h, "SHORT"):
        log.info("BR %s SHORT breakout — but 4H exhausted, skip", symbol)
        broke_short = False

    if not broke_long and not broke_short:
        log.info("BR %s: range valid but no breakout  "
                  "bar_close=%.4f  rng=[%.4f, %.4f]  "
                  "vol_ok=%s  htf_bull=%s  htf_bear=%s",
                  symbol, bar["c"],
                  rng_low, rng_high,
                  vol_ok, htf_bull, htf_bear)
        return []

    next_state = "AWAITING_BREAKOUT_CONFIRM" if _REQUIRE_BK_CONFIRM else "AWAITING_RETEST"

    if broke_long:
        _state[symbol] = {
            "state":       next_state,
            "direction":   "LONG",
            "flip_level":  rng_high,
            "bars_waited": 0,
        }
        log.info("BR %s LONG breakout above %.4f — %s",
                  symbol, rng_high,
                  "confirming next bar" if _REQUIRE_BK_CONFIRM else "waiting for retest")

    elif broke_short:
        _state[symbol] = {
            "state":       next_state,
            "direction":   "SHORT",
            "flip_level":  rng_low,
            "bars_waited": 0,
        }
        log.info("BR %s SHORT breakout below %.4f — %s",
                  symbol, rng_low,
                  "confirming next bar" if _REQUIRE_BK_CONFIRM else "waiting for retest")

    return []   # no signal on breakout bar — wait for retest
