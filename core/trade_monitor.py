"""Trade monitor — polls open positions every 30 s and closes them when TP/SL is hit.

Paper mode:  compares current price in cache against stop/tp from DB.
Live mode:   queries Binance /fapi/v1/order by order_id to detect FILLED/CANCELLED.

Run as an asyncio task from main.py:
    asyncio.create_task(monitor_trades(cache))
"""
import asyncio
import hashlib
import hmac
import logging
import os
import sqlite3
import time
import urllib.parse
from datetime import datetime, timezone

import aiohttp

log = logging.getLogger(__name__)

_DB_PATH         = os.environ.get("DB_PATH", "confluence_bot.db")
_PAPER_MODE      = os.environ.get("PAPER_MODE", "0") == "1"
_POLL_INTERVAL_S = 30
_BINANCE_BASE    = "https://fapi.binance.com"
_API_KEY         = os.environ.get("BINANCE_API_KEY", "")
_SECRET          = os.environ.get("BINANCE_SECRET", "")
_REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=10)


# ── Binance signing ────────────────────────────────────────────────────────────

def _sign(params: dict) -> dict:
    params["timestamp"] = int(time.time() * 1000)
    query = urllib.parse.urlencode(params)
    sig = hmac.new(_SECRET.encode(), query.encode(), hashlib.sha256).hexdigest()
    params["signature"] = sig
    return params


# ── DB helpers ────────────────────────────────────────────────────────────────

def _load_open_trades() -> list[dict]:
    """Return all OPEN trades from DB."""
    try:
        with sqlite3.connect(_DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT id, symbol, direction, entry, stop_loss, take_profit, "
                "size, order_id FROM trades WHERE status='OPEN'"
            ).fetchall()
        return [dict(r) for r in rows]
    except Exception as exc:
        log.warning("load_open_trades failed: %s", exc)
        return []


def _close_trade_db(trade_id: int, exit_price: float, pnl: float) -> None:
    """Mark a trade as FILLED with exit price and PnL."""
    closed_ts = datetime.now(timezone.utc).isoformat()
    try:
        with sqlite3.connect(_DB_PATH) as conn:
            conn.execute(
                "UPDATE trades SET status='FILLED', exit_price=?, pnl_usdt=?, closed_ts=? "
                "WHERE id=?",
                (exit_price, pnl, closed_ts, trade_id),
            )
    except Exception as exc:
        log.warning("_close_trade_db(%s) failed: %s", trade_id, exc)


def _cancel_trade_db(trade_id: int) -> None:
    """Mark a trade as CANCELLED."""
    closed_ts = datetime.now(timezone.utc).isoformat()
    try:
        with sqlite3.connect(_DB_PATH) as conn:
            conn.execute(
                "UPDATE trades SET status='CANCELLED', pnl_usdt=0.0, closed_ts=? WHERE id=?",
                (closed_ts, trade_id),
            )
    except Exception as exc:
        log.warning("_cancel_trade_db(%s) failed: %s", trade_id, exc)


# ── Order status checks ───────────────────────────────────────────────────────

async def _check_live_order(
    trade: dict, session: aiohttp.ClientSession
) -> tuple[str, float] | None:
    """
    Query Binance for entry order status.
    Returns ('FILLED', fill_price) | ('CANCELLED', 0.0) | None (still open).
    """
    order_id = str(trade.get("order_id", "")).strip()
    if not order_id or order_id in ("", "None", "0"):
        return None

    url     = f"{_BINANCE_BASE}/fapi/v1/order"
    headers = {"X-MBX-APIKEY": _API_KEY}
    params  = _sign({"symbol": trade["symbol"], "orderId": int(order_id)})
    try:
        async with session.get(url, params=params, headers=headers) as resp:
            resp.raise_for_status()
            data = await resp.json()
        status = data.get("status", "")
        if status == "FILLED":
            fill_price = float(data.get("avgPrice") or data.get("price") or 0)
            return ("FILLED", fill_price)
        if status in ("CANCELED", "EXPIRED", "REJECTED"):
            return ("CANCELLED", 0.0)
        return None  # NEW or PARTIALLY_FILLED — still active
    except Exception as exc:
        log.debug("_check_live_order(%s #%s): %s", trade["symbol"], order_id, exc)
        return None


def _check_paper_order(trade: dict, cache) -> tuple[str, float] | None:
    """
    In paper mode, simulate TP/SL by comparing current price to trade levels.
    Returns ('TP', exit_price) | ('SL', exit_price) | None (not hit yet).
    """
    symbol    = trade["symbol"]
    direction = trade["direction"]
    sl        = float(trade["stop_loss"])
    tp        = float(trade["take_profit"])

    price = cache.get_last_price(symbol)
    if not price or price <= 0:
        return None

    if direction == "LONG":
        if price >= tp:
            return ("TP", tp)
        if price <= sl:
            return ("SL", sl)
    else:  # SHORT
        if price <= tp:
            return ("TP", tp)
        if price >= sl:
            return ("SL", sl)

    return None


# ── PnL ───────────────────────────────────────────────────────────────────────

def _calc_pnl(trade: dict, exit_price: float) -> float:
    entry = float(trade["entry"])
    size  = float(trade["size"])
    if trade["direction"] == "LONG":
        return round((exit_price - entry) * size, 4)
    return round((entry - exit_price) * size, 4)


# ── Main coroutine ────────────────────────────────────────────────────────────

async def monitor_trades(cache) -> None:
    """
    Continuously polls open trades and closes them when TP/SL is hit.
    Launch as an asyncio task — runs forever.
    """
    log.info(
        "Trade monitor started  paper=%s  poll=%.0fs",
        _PAPER_MODE, _POLL_INTERVAL_S,
    )

    while True:
        await asyncio.sleep(_POLL_INTERVAL_S)

        trades = await asyncio.to_thread(_load_open_trades)
        if not trades:
            continue

        async with aiohttp.ClientSession(timeout=_REQUEST_TIMEOUT) as session:
            for trade in trades:
                try:
                    if _PAPER_MODE:
                        result = _check_paper_order(trade, cache)
                    else:
                        result = await _check_live_order(trade, session)

                    if result is None:
                        continue

                    outcome, exit_price = result
                    pnl = _calc_pnl(trade, exit_price) if exit_price > 0 else 0.0

                    if outcome == "CANCELLED":
                        await asyncio.to_thread(_cancel_trade_db, trade["id"])
                    else:
                        await asyncio.to_thread(_close_trade_db, trade["id"], exit_price, pnl)

                    # Remove from executor's active set
                    from core.executor import close_deal
                    close_deal(trade["symbol"], trade["direction"])

                    emoji = "✅" if pnl >= 0 else "❌"
                    log.info(
                        "%s Trade closed: %s %s  outcome=%s  exit=%.4f  pnl=%+.2f USDT",
                        emoji, trade["direction"], trade["symbol"],
                        outcome, exit_price, pnl,
                    )

                    # Telegram close alert
                    try:
                        from notifications.telegram import send_trade_close
                        await send_trade_close(trade, outcome, exit_price, pnl)
                    except Exception as exc:
                        log.debug("Telegram close alert failed: %s", exc)

                except Exception as exc:
                    log.warning(
                        "monitor_trades error on %s: %s",
                        trade.get("symbol", "?"), exc,
                    )
