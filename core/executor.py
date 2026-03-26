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

# Paper mode: set PAPER_MODE=1 to skip real order submission
_PAPER_MODE = os.environ.get("PAPER_MODE", "0") == "1"

log = logging.getLogger(__name__)

# Active deal tracking: set of (symbol, direction) tuples
_active_deals: set[tuple[str, str]] = set()

# Post-trade cooldown: symbol → monotonic timestamp when cooldown expires
_post_trade_until: dict[str, float] = {}


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
    regime    = score_dict["regime"]

    # Gate: post-trade cooldown (prevents whipsaw re-entry after close)
    import time as _time
    if _time.monotonic() < _post_trade_until.get(symbol, 0.0):
        remaining = _post_trade_until[symbol] - _time.monotonic()
        log.debug("Skipping %s %s — post-trade cooldown (%.0f min remaining)",
                  direction, symbol, remaining / 60)
        return None

    # Gate: no duplicate open position for same symbol+direction
    deal_key = (symbol, direction)
    if deal_key in _active_deals:
        log.debug("Skipping %s %s — already active", direction, symbol)
        return None

    # Gate: max open positions
    if len(_active_deals) >= _MAX_OPEN:
        log.debug("Skipping %s %s — max positions (%d) reached", direction, symbol, _MAX_OPEN)
        return None

    # Gate: directional exposure cap — prevents correlated blow-up
    # (e.g. 5 longs on correlated alts all hitting SL simultaneously on one BTC dump)
    same_dir_count = sum(1 for _, d in _active_deals if d == direction)
    if same_dir_count >= _MAX_SAME_DIRECTION:
        log.debug(
            "Skipping %s %s — directional cap: %d/%d same-direction positions open",
            direction, symbol, same_dir_count, _MAX_SAME_DIRECTION,
        )
        return None

    # Gate: verify no open position on exchange (guards against _active_deals desync)
    from data.binance_rest import get_position_amt
    pos_amt = await get_position_amt(symbol)
    if (direction == "LONG" and pos_amt > 0) or (direction == "SHORT" and pos_amt < 0):
        log.info("Skipping %s %s — open position already exists on exchange (amt=%.4f)", direction, symbol, pos_amt)
        _active_deals.add(deal_key)   # re-sync
        return None

    from .rr_calculator import compute, position_size

    # Strategy-specific pre-computed stop/TP keys (bypass ATR calculator)
    _PRESET_LEVELS: dict[str, tuple[str, str]] = {
        "LEADLAG":    ("ll_stop",  "ll_tp"),
        "MICRORANGE": ("mr_stop",  "mr_tp"),
        "SESSION":    ("ss_stop",  "ss_tp"),
        "INSIDEBAR":  ("ib_stop",  "ib_tp"),
        "FUNDING":    ("fh_stop",  "fh_tp"),
    }

    # Minimum RR requirement per strategy (scalps operate at lower RR)
    _MIN_RR: dict[str, float] = {
        "LEADLAG":    _cfg["risk"]["rr_ratio"],   # 2.5
        "MICRORANGE": 1.5,
        "SESSION":    1.2,
        "INSIDEBAR":  1.2,
        "FUNDING":    1.2,
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

    # PUMP and BREAKOUT: extend TP to 5× and flag trailing stop
    use_trailing = regime in ("PUMP", "BREAKOUT")
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

    qty = position_size(entry, stop, cache, symbol)
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
            "[PAPER] %s %s qty=%.4f entry=%.4f sl=%.4f tp=%.4f",
            side, symbol, qty, entry, stop, tp,
        )
    else:
        from data.binance_rest import place_limit_then_market
        order = await place_limit_then_market(
            symbol=symbol,
            side=side,
            quantity=qty,
            limit_price=entry,   # try LIMIT at computed entry; fall back to MARKET after 30s
            stop=stop,
            take_profit=tp,
        )
        if not order:
            return None
        order["entry"]       = entry
        order["stop"]        = stop
        order["take_profit"] = tp
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

    _active_deals.add(deal_key)
    log.info("Position opened: %s %s — active deals: %d", direction, symbol, len(_active_deals))

    # Set per-symbol cooldown (each strategy manages its own cooldown state)
    if regime == "LEADLAG":
        from .leadlag_scorer import set_cooldown
        set_cooldown(symbol)
    elif regime == "MICRORANGE":
        from .microrange_scorer import set_cooldown
        set_cooldown(symbol)
    elif regime == "INSIDEBAR":
        from .insidebar_scorer import set_cooldown
        set_cooldown(symbol)
    elif regime == "FUNDING":
        from .funding_harvest_scorer import set_cooldown
        set_cooldown(symbol)

    return order


def close_deal(symbol: str, direction: str) -> None:
    """Remove a closed/cancelled position from the active set and set post-trade cooldown."""
    import time as _time
    _active_deals.discard((symbol, direction))
    _post_trade_until[symbol] = _time.monotonic() + _POST_TRADE_COOLDOWN
    log.debug("Post-trade cooldown set for %s (%.0f min)", symbol, _POST_TRADE_COOLDOWN / 60)


def restore_active_deals(deals: list[tuple[str, str]]) -> None:
    """Repopulate _active_deals from persisted DB state on startup."""
    for deal in deals:
        _active_deals.add(deal)
    if deals:
        log.info("Restored %d active deal(s) from DB: %s", len(deals), deals)
