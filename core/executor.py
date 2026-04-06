"""Executor — places orders via Binance API when a signal fires."""
import logging
import os
import yaml

_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "config.yaml")
with open(_CONFIG_PATH) as _f:
    _cfg = yaml.safe_load(_f)

_MAX_OPEN            = _cfg["risk"]["max_open_positions"]
_LEVERAGE            = _cfg["risk"]["leverage"]
_POST_TRADE_COOLDOWN = float(_cfg["risk"].get("post_trade_cooldown_mins", 30)) * 60.0
# Max same-direction positions open simultaneously (correlated-exposure cap).
# e.g. 2 longs max: prevents 5 correlated alts all hitting SL in one BTC flush.
_MAX_SAME_DIRECTION  = int(_cfg["risk"].get("max_same_direction_positions", 2))

# Trailing stop config — applies to PUMP and BREAKOUT regimes only
_TRAIL_CFG      = _cfg.get("trailing_stop", {})
_TRAIL_ENABLED  = bool(_TRAIL_CFG.get("enabled", True))
_TRAIL_REGIMES  = set(_TRAIL_CFG.get("apply_to", ["PUMP", "BREAKOUT"]))
_TRAIL_MIN_CB   = float(_TRAIL_CFG.get("min_callback_pct", 0.5))
_TRAIL_MAX_CB   = float(_TRAIL_CFG.get("max_callback_pct", 3.0))
_TRAIL_ATR_MULT = float(_TRAIL_CFG.get("atr_multiplier",   1.5))

# Paper mode: set PAPER_MODE=1 to skip real order submission
_PAPER_MODE = os.environ.get("PAPER_MODE", "0") == "1"

log = logging.getLogger(__name__)

_QTY_PRECISION = {
    "BTCUSDT":  3,
    "ETHUSDT":  3,
    "SOLUSDT":  1,
    "BNBUSDT":  2,
    "XRPUSDT":  0,
    "LINKUSDT": 1,
    "DOGEUSDT": 0,
    "SUIUSDT":  0,
}
_PRICE_PRECISION = {
    "BTCUSDT":  1,
    "ETHUSDT":  2,
    "SOLUSDT":  2,
    "BNBUSDT":  2,
    "XRPUSDT":  4,
    "LINKUSDT": 3,
    "DOGEUSDT": 5,
    "SUIUSDT":  4,
}

# Active deal tracking: set of (symbol, direction) tuples
_active_deals: set[tuple[str, str]] = set()

# Pending deals: claimed inside _deal_lock before any I/O awaits.
_pending_deals: set[tuple[str, str]] = set()

# Lock protecting _active_deals and _pending_deals — held only during check+claim,
# never during exchange I/O (keeps latency low).
import asyncio as _asyncio
_deal_lock = _asyncio.Lock()

# Post-trade cooldown: symbol → monotonic timestamp when cooldown expires
_post_trade_until: dict[str, float] = {}

# Cross-strategy directional cooldown — prevents 2 strategies entering
# the same symbol in the same direction within post_trade_cooldown_mins
_symbol_direction_until: dict[tuple[str, str], float] = {}

# ── Dynamic slippage ──────────────────────────────────────────────────────────
# Baseline slippage per regime (one-way). Used as the reference in log output
# and as the fallback when config doesn't override a regime.
_SLIP_BY_REGIME: dict[str, float] = {
    "TREND":    0.0002,   # 0.02% — normal trending market
    "RANGE":    0.0002,   # 0.02% — range-bound, tight spreads
    "CRASH":    0.0010,   # 0.10% — cascading liquidations, wide spreads
    "PUMP":     0.0008,   # 0.08% — euphoria, thin asks above
    "BREAKOUT": 0.0005,   # 0.05% — momentum, some slippage on entry
}
_SLIP_BASE = 0.0002   # fallback when regime unknown


def _dynamic_slippage(symbol: str, regime: str, cache) -> float:
    """Return estimated one-way slippage for a market order.

    Scales the regime base by the current ATR ratio:
        slip = base_regime × clamp(current_atr / avg_atr, 0.5, 5.0)

    ATR scaling can be disabled via config: risk.slippage_atr_scale: false
    Never returns less than 0.0001 (1 basis point minimum).
    Falls back to the regime base without ATR adjustment on any error.
    """
    _risk_cfg  = _cfg.get("risk", {})
    _slip_cfg  = _risk_cfg.get("slippage_by_regime", {})
    base = float(_slip_cfg.get(regime.upper(),
                               _SLIP_BY_REGIME.get(regime.upper(), _SLIP_BASE)))
    try:
        if not _risk_cfg.get("slippage_atr_scale", True):
            return base
        bars = cache.get_ohlcv(symbol, 22, "1h")
        if not bars or len(bars) < 15:
            return base
        trs = []
        for i in range(1, len(bars)):
            h, l, pc = bars[i]["h"], bars[i]["l"], bars[i - 1]["c"]
            trs.append(max(h - l, abs(h - pc), abs(l - pc)))
        if len(trs) < 14:
            return base
        avg_atr = sum(trs[-21:-1]) / min(20, len(trs) - 1)
        if avg_atr == 0.0:
            return base
        atr_ratio = min(max(trs[-1] / avg_atr, 0.5), 5.0)
        return max(base * atr_ratio, 0.0001)
    except Exception as exc:
        log.debug("_dynamic_slippage fallback for %s: %s", symbol, exc)
        return base


async def execute_signal(score_dict: dict, cache) -> dict | None:
    """Place an order based on a fired score dict.

    Steps:
    1. Skip if fire=False, duplicate position, or max positions reached.
    2. Compute entry / stop / tp / size via rr_calculator.
    3. Validate RR ≥ 2.5 (hard filter).
    4. Place bracket order on Binance Futures (or log in PAPER_MODE).
    5. Log the trade via TradeLogger.
    6. Mark position as active.

    Returns the Binance order confirmation dict, or None on skip/failure.
    """
    if not score_dict.get("fire"):
        return None

    # Circuit breaker — halt all entries on daily loss / streak limits
    from core.circuit_breaker import is_tripped, status as cb_status
    if is_tripped():
        cb = cb_status()
        log.warning("CIRCUIT BREAKER active — skipping signal. Reason: %s", cb["reason"])
        return None

    symbol    = score_dict["symbol"]
    direction = score_dict["direction"]

    # Gate: post-trade cooldown (prevents whipsaw re-entry after close)
    import time as _time
    if _time.monotonic() < _post_trade_until.get(symbol, 0.0):
        remaining = _post_trade_until[symbol] - _time.monotonic()
        log.debug("Skipping %s %s — post-trade cooldown (%.0f min remaining)",
                  direction, symbol, remaining / 60)
        return None

    # Gate: cross-strategy direction cooldown (prevents 2 strategies entering same symbol+direction)
    _sym_dir_key = (symbol, direction)
    if _time.monotonic() < _symbol_direction_until.get(_sym_dir_key, 0.0):
        remaining = _symbol_direction_until[_sym_dir_key] - _time.monotonic()
        log.debug("Skipping %s %s — cross-strategy direction cooldown (%.0f min)",
                  direction, symbol, remaining / 60)
        return None

    # Gate: no duplicate open position — hold lock during check+claim only,
    # never during I/O so other signals can proceed concurrently.
    deal_key = (symbol, direction)
    async with _deal_lock:
        if deal_key in _active_deals or deal_key in _pending_deals:
            log.debug("Skipping %s %s — already active or pending", direction, symbol)
            return None
        if len(_active_deals) + len(_pending_deals) >= _MAX_OPEN:
            log.debug("Skipping %s %s — max positions (%d) reached", direction, symbol, _MAX_OPEN)
            return None
        same_dir = sum(1 for _, d in (_active_deals | _pending_deals) if d == direction)
        if same_dir >= _MAX_SAME_DIRECTION:
            log.debug("Skipping %s %s — directional cap %d/%d", direction, symbol,
                      same_dir, _MAX_SAME_DIRECTION)
            return None
        _pending_deals.add(deal_key)   # claim slot before releasing lock

    try:
        result = await _execute_signal_inner(score_dict, cache, deal_key)
    except Exception:
        async with _deal_lock:
            _pending_deals.discard(deal_key)
        raise
    # Clean up pending on any failure path (discard is idempotent —
    # harmless if inner already promoted to active or discarded).
    if result is None:
        async with _deal_lock:
            _pending_deals.discard(deal_key)
    return result


async def _execute_signal_inner(score_dict: dict, cache, deal_key: tuple) -> dict | None:
    """Inner execution — called only after deal_key is claimed in _pending_deals."""
    symbol    = score_dict["symbol"]
    direction = score_dict["direction"]
    regime    = score_dict["regime"]

    # Gate: DB-level open trade check — cross-process safe (catches duplicate instances)
    import sqlite3 as _sqlite3
    _db_path = os.environ.get("DB_PATH", "confluence_bot.db")
    try:
        with _sqlite3.connect(_db_path) as _conn:
            _open_row = _conn.execute(
                "SELECT id FROM trades WHERE symbol=? AND direction=? AND status='OPEN' LIMIT 1",
                (symbol, direction),
            ).fetchone()
        if _open_row:
            log.info("Skipping %s %s — OPEN trade already in DB (id=%s)",
                     direction, symbol, _open_row[0])
            async with _deal_lock:
                _pending_deals.discard(deal_key)
                _active_deals.add(deal_key)   # re-sync in-memory state
            return None
    except Exception as _exc:
        log.debug("DB open trade pre-check failed: %s", _exc)

    # Gate: verify no open position on exchange (guards against _active_deals desync)
    from data.binance_rest import get_position_amt
    pos_amt = await get_position_amt(symbol)
    if (direction == "LONG" and pos_amt > 0) or (direction == "SHORT" and pos_amt < 0):
        log.info("Skipping %s %s — open position already exists on exchange (amt=%.4f)", direction, symbol, pos_amt)
        async with _deal_lock:
            _pending_deals.discard(deal_key)
            _active_deals.add(deal_key)   # re-sync
        return None

    from .rr_calculator import compute, position_size

    # Strategy-specific pre-computed stop/TP keys (bypass ATR calculator)
    _PRESET_LEVELS: dict[str, tuple[str, str]] = {
        "LEADLAG":     ("ll_stop",  "ll_tp"),
        "MICRORANGE":  ("mr_stop",  "mr_tp"),
        "SESSION":     ("ss_stop",  "ss_tp"),
        "EMA_PULLBACK":("ep_stop",  "ep_tp"),
        "ZONE":        ("zn_stop",  "zn_tp"),
        "FVG":         ("fvg_stop", "fvg_tp"),
        "VWAPBAND":    ("vb_stop",  "vb_tp"),
        "OISPIKE":     ("os_stop",  "os_tp"),
        "WYCKOFF":     ("ws_stop",  "ws_tp"),
        "liq_sweep":       ("ls_stop",  "ls_tp"),
        "BREAKOUT_RETEST": ("br_stop",  "br_tp"),
    }

    # Minimum RR requirement per strategy (scalps operate at lower RR)
    _MIN_RR: dict[str, float] = {
        "LEADLAG":      _cfg["risk"]["rr_ratio"],   # 2.5
        "MICRORANGE":   1.5,
        "SESSION":      1.2,
        "EMA_PULLBACK": 2.0,
        "ZONE":         2.0,
        "FVG":          2.0,
        "VWAPBAND":     1.5,
        "OISPIKE":      2.0,
        "WYCKOFF":      float(_cfg.get("wyckoff_spring", {}).get("rr_ratio", 2.5)),
        "liq_sweep":       float(_cfg.get("liq_sweep",       {}).get("rr_ratio", 2.5)),
        "BREAKOUT_RETEST": 1.3,   # scorer validates 2.2R from flip level; allow slippage
    }

    stop_key, tp_key = _PRESET_LEVELS.get(regime, (None, None))
    if stop_key and score_dict.get(stop_key) and score_dict.get(tp_key):
        closes_1m = cache.get_closes(symbol, window=1, tf="1m")
        entry = closes_1m[-1] if closes_1m else cache.get_last_price(symbol)
        stop  = score_dict[stop_key]
        tp    = score_dict[tp_key]
    else:
        entry, stop, tp = compute(symbol, direction, cache)

    if entry == 0.0 or stop == 0.0 or tp == 0.0:
        log.warning("RR compute failed for %s %s — skipping", direction, symbol)
        return None

    stop_dist = abs(entry - stop)

    # PUMP and BREAKOUT: use exchange-native trailing stop instead of fixed TP.
    # Fixed 5× TP is used as the RR-gate reference and as fallback if trailing fails.
    use_trailing = _TRAIL_ENABLED and regime in _TRAIL_REGIMES
    if use_trailing:
        tp = (entry + stop_dist * 5.0) if direction == "LONG" else (entry - stop_dist * 5.0)

    # Gate: strategy-specific minimum RR
    min_rr  = _MIN_RR.get(regime, _cfg["risk"]["rr_ratio"])
    tp_dist = abs(tp - entry)
    if stop_dist == 0 or tp_dist / stop_dist < min_rr:
        log.info("RR %.2f below min %.2f for %s %s (entry=%.4f sl=%.4f tp=%.4f) — skipping",
                 tp_dist / stop_dist if stop_dist else 0, min_rr,
                 direction, symbol, entry, stop, tp)
        return None

    # Allow per-signal risk override (e.g. drawdown scaling from strategy loops)
    signal_risk_pct = score_dict.get("risk_pct")
    est_slip = _dynamic_slippage(symbol, regime, cache)
    log.info(
        "Dynamic slippage %s %s [%s]: %.4f%%  (base=%.4f%%)",
        direction, symbol, regime,
        est_slip * 100,
        _SLIP_BY_REGIME.get(regime.upper(), _SLIP_BASE) * 100,
    )
    raw_balance = cache.get_account_balance()
    qty = position_size(entry, stop, cache, symbol, risk_pct=signal_risk_pct, slip_pct=est_slip)

    # Round qty and prices to exchange-required precision
    q_dp = _QTY_PRECISION.get(symbol, 3)
    qty   = round(qty, q_dp)
    if q_dp == 0:
        qty = int(qty)
    entry = round(entry, _PRICE_PRECISION.get(symbol, 4))
    stop  = round(stop,  _PRICE_PRECISION.get(symbol, 4))
    tp    = round(tp,    _PRICE_PRECISION.get(symbol, 4))

    log.debug("Position sizing %s %s: raw_bal=%.2f entry=%.4f stop=%.4f qty=%.4f",
              direction, symbol, raw_balance, entry, stop, qty)
    if qty <= 0.0:
        log.warning("Position size 0 for %s %s — skipping", direction, symbol)
        return None

    side = "BUY" if direction == "LONG" else "SELL"

    if _PAPER_MODE:
        order: dict = {
            "paper":       True,
            "symbol":      symbol,
            "side":        side,
            "entry":       entry,
            "stop":        stop,
            "take_profit": tp,
            "qty":         qty,
            "regime":      regime,
            "trailing":    use_trailing,
        }
        log.info(
            "[PAPER] %s %s qty=%.4f entry=%.4f sl=%.4f tp=%s trailing=%s",
            side, symbol, qty, entry, stop,
            f"{tp:.4f}" if tp else "trailing", use_trailing,
        )
    else:
        # For PUMP/BREAKOUT: place trailing stop before the entry order.
        # Binance accepts TRAILING_STOP_MARKET with reduceOnly=true before a
        # position exists — it only fires once the position is open.
        effective_tp: float | None = tp
        if use_trailing:
            from data.binance_rest import place_trailing_stop
            atr_pct      = abs(stop_dist / entry) * 100
            callback_pct = max(_TRAIL_MIN_CB, min(_TRAIL_MAX_CB, atr_pct * _TRAIL_ATR_MULT))
            trail_side   = "SELL" if side == "BUY" else "BUY"
            trailing_resp = await place_trailing_stop(
                symbol         = symbol,
                side           = trail_side,
                quantity       = qty,
                activation_pct = atr_pct * 0.5,
                callback_pct   = callback_pct,
            )
            if trailing_resp:
                log.info("Trailing stop active: %s callback=%.1f%%", symbol, callback_pct)
                effective_tp = None   # trailing stop handles exit — skip fixed TP
            else:
                log.warning(
                    "Trailing stop failed for %s — falling back to fixed TP 5×", symbol
                )
                # effective_tp remains at 5× tp computed above

        from data.binance_rest import place_limit_then_market
        order = await place_limit_then_market(
            symbol      = symbol,
            side        = side,
            quantity    = qty,
            limit_price = entry,
            stop        = stop,
            take_profit = effective_tp,   # None when trailing placed, 5× on fallback
        )
        if not order:
            return None
        order["entry"]       = entry
        order["stop"]        = stop
        order["take_profit"] = effective_tp
        order["qty"]         = qty
        order["regime"]      = regime

    # Log, notify, mark active
    try:
        from logging_.logger import TradeLogger
        await TradeLogger().log_trade(score_dict, order)
    except Exception as exc:
        log.warning("TradeLogger.log_trade failed: %s", exc)

    try:
        from notifications.telegram import send_signal_alert
        await send_signal_alert(score_dict, order)
    except Exception as exc:
        log.warning("Telegram alert failed: %s", exc)

    async with _deal_lock:
        _pending_deals.discard(deal_key)
        _active_deals.add(deal_key)
    from core.rr_calculator import invalidate_committed_cache
    invalidate_committed_cache()
    log.info("Position opened: %s %s — active deals: %d", direction, symbol, len(_active_deals))

    # Set per-symbol cooldown (each strategy manages its own cooldown state)
    if regime == "LEADLAG":
        from .leadlag_scorer import set_cooldown
        set_cooldown(symbol)
    elif regime == "MICRORANGE":
        from .microrange_scorer import set_cooldown
        set_cooldown(symbol)
    elif regime == "EMA_PULLBACK":
        from .ema_pullback_scorer import set_cooldown
        set_cooldown(symbol)
    elif regime == "ZONE":
        from .zone_scorer import set_cooldown
        set_cooldown(symbol)
    elif regime == "FVG":
        from .fvg_scorer import set_cooldown
        set_cooldown(symbol)
    elif regime == "VWAPBAND":
        from .vwap_band_scorer import set_cooldown
        set_cooldown(symbol)
    elif regime == "OISPIKE":
        from .oi_spike_scorer import set_cooldown
        set_cooldown(symbol)
    elif regime == "WYCKOFF":
        from .wyckoff_scorer import set_cooldown
        set_cooldown(symbol)
    elif regime == "liq_sweep":
        pass   # liq_sweep cooldown is managed inside liq_sweep_scorer.score()

    return order


def close_deal(symbol: str, direction: str) -> None:
    """Remove a closed/cancelled position from the active set and set post-trade cooldown."""
    import time as _time
    _active_deals.discard((symbol, direction))
    _post_trade_until[symbol] = _time.monotonic() + _POST_TRADE_COOLDOWN
    _symbol_direction_until[(symbol, direction)] = _time.monotonic() + _POST_TRADE_COOLDOWN
    from core.rr_calculator import invalidate_committed_cache
    invalidate_committed_cache()
    log.debug("Post-trade cooldown set for %s (%.0f min)", symbol, _POST_TRADE_COOLDOWN / 60)


def restore_active_deals(deals: list[tuple[str, str]]) -> None:
    """Repopulate _active_deals from persisted DB state on startup."""
    for deal in deals:
        _active_deals.add(deal)
    if deals:
        log.info("Restored %d active deal(s) from DB: %s", len(deals), deals)
