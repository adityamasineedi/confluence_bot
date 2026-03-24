"""Executor — places orders via Binance API when a signal fires."""
import logging
import os
import yaml

_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "config.yaml")
with open(_CONFIG_PATH) as _f:
    _cfg = yaml.safe_load(_f)

_MAX_OPEN = _cfg["risk"]["max_open_positions"]
_LEVERAGE = _cfg["risk"]["leverage"]

# Paper mode: set PAPER_MODE=1 to skip real order submission
_PAPER_MODE = os.environ.get("PAPER_MODE", "0") == "1"

log = logging.getLogger(__name__)

# Active deal tracking: set of (symbol, direction) tuples
_active_deals: set[tuple[str, str]] = set()


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

    symbol    = score_dict["symbol"]
    direction = score_dict["direction"]
    regime    = score_dict["regime"]

    # Gate: no duplicate open position for same symbol+direction
    deal_key = (symbol, direction)
    if deal_key in _active_deals:
        log.debug("Skipping %s %s — already active", direction, symbol)
        return None

    # Gate: max open positions
    if len(_active_deals) >= _MAX_OPEN:
        log.debug("Skipping %s %s — max positions (%d) reached", direction, symbol, _MAX_OPEN)
        return None

    from .rr_calculator import compute, position_size
    entry, stop, tp = compute(symbol, direction, cache)
    if entry == 0.0 or stop == 0.0 or tp == 0.0:
        log.warning("RR compute failed for %s %s — skipping", direction, symbol)
        return None

    stop_dist = abs(entry - stop)

    # PUMP and BREAKOUT: extend TP to 5× and flag trailing stop
    use_trailing = regime in ("PUMP", "BREAKOUT")
    if use_trailing:
        tp = (entry + stop_dist * 5.0) if direction == "LONG" else (entry - stop_dist * 5.0)

    # Gate: RR ≥ 2.5 (trailing trades always pass since TP set to 5×)
    tp_dist = abs(tp - entry)
    if stop_dist == 0 or tp_dist / stop_dist < _cfg["risk"]["rr_ratio"]:
        log.debug("RR below threshold for %s %s — skipping", direction, symbol)
        return None

    qty = position_size(entry, stop, cache)
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
        from data.binance_rest import place_order
        order = await place_order(
            symbol=symbol,
            side=side,
            quantity=qty,
            entry=0.0,         # 0.0 → MARKET
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

    return order


def close_deal(symbol: str, direction: str) -> None:
    """Remove a closed/cancelled position from the active set."""
    _active_deals.discard((symbol, direction))


def restore_active_deals(deals: list[tuple[str, str]]) -> None:
    """Repopulate _active_deals from persisted DB state on startup."""
    for deal in deals:
        _active_deals.add(deal)
    if deals:
        log.info("Restored %d active deal(s) from DB: %s", len(deals), deals)
