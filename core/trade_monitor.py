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
_BINANCE_BASE    = os.environ.get("BINANCE_BASE_URL", "https://fapi.binance.com")
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
                "size, order_id, regime, ts FROM trades WHERE status='OPEN'"
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
    Check if a live trade has hit SL or TP via Binance position + ticker.

    If order_id is available, checks the order status first.
    Falls back to position-based check: if no open position exists for the
    symbol, infer TP or SL hit from the current price vs entry levels.

    Returns ('TP'/'SL', exit_price) | ('CANCELLED', 0.0) | None (still open).
    """
    symbol   = trade["symbol"]
    headers  = {"X-MBX-APIKEY": _API_KEY}

    # ── Step 1: Check open orders — if both SL and TP still open → trade live ──
    direction = trade["direction"]
    sl = float(trade["stop_loss"])
    tp = float(trade["take_profit"])
    try:
        open_params = _sign({"symbol": symbol})
        async with session.get(
            f"{_BINANCE_BASE}/fapi/v1/openOrders",
            params=open_params, headers=headers
        ) as resp:
            resp.raise_for_status()
            open_orders = await resp.json()

        # Count reduce-only orders for this symbol (our SL + TP bracket)
        bracket_open = sum(
            1 for o in open_orders
            if o.get("reduceOnly") and o.get("symbol") == symbol
        )
        if bracket_open >= 2:
            return None   # both SL and TP still live → position still open
        if bracket_open == 1:
            return None   # one leg remaining → position still open

    except Exception as exc:
        log.debug("_check_live_order openOrders (%s): %s", symbol, exc)

    # ── Step 2: Bracket gone — check position size ───────────────────────────
    pos_amt = 0.0
    try:
        pos_params = _sign({"symbol": symbol})
        async with session.get(
            f"{_BINANCE_BASE}/fapi/v2/positionRisk",
            params=pos_params, headers=headers
        ) as resp:
            resp.raise_for_status()
            positions = await resp.json()

        for p in positions:
            if p.get("symbol") == symbol:
                pos_amt = float(p.get("positionAmt", 0))
                break

        position_still_open = (
            (direction == "LONG"  and pos_amt > 0) or
            (direction == "SHORT" and pos_amt < 0)
        )

        if position_still_open:
            # ── Step 2b: Software SL/TP — no bracket orders but position open ──
            # Protects positions when exchange stop orders couldn't be placed
            # (e.g. demo API limitation) or were cancelled unexpectedly.
            try:
                async with session.get(
                    f"{_BINANCE_BASE}/fapi/v1/ticker/price",
                    params={"symbol": symbol}
                ) as resp:
                    resp.raise_for_status()
                    price = float((await resp.json()).get("price", 0))

                hit_tp = (direction == "LONG"  and price >= tp) or \
                         (direction == "SHORT" and price <= tp)
                hit_sl = (direction == "LONG"  and price <= sl) or \
                         (direction == "SHORT" and price >= sl)

                if hit_tp or hit_sl:
                    outcome    = "TP" if hit_tp else "SL"
                    exit_price = tp if hit_tp else sl
                    # Place market close
                    close_side = "SELL" if direction == "LONG" else "BUY"
                    close_qty  = abs(pos_amt)
                    close_qty  = int(close_qty) if close_qty == int(close_qty) else close_qty
                    close_params = _sign({
                        "symbol":     symbol,
                        "side":       close_side,
                        "type":       "MARKET",
                        "quantity":   close_qty,
                        "reduceOnly": "true",
                    })
                    async with session.post(
                        f"{_BINANCE_BASE}/fapi/v1/order",
                        params=close_params, headers=headers
                    ) as resp:
                        close_resp = await resp.json()
                    if close_resp.get("orderId"):
                        log.info("Software %s triggered for %s — market close placed (price=%.6f %s=%.6f)",
                                 outcome, symbol, price, outcome, exit_price)
                        return (outcome, exit_price)
                    else:
                        log.warning("Software %s market close rejected for %s: %s", outcome, symbol, close_resp)

            except Exception as exc:
                log.debug("_check_live_order software SL/TP (%s): %s", symbol, exc)

            return None   # position still open, SL/TP not hit

    except Exception as exc:
        log.debug("_check_live_order positionRisk (%s): %s", symbol, exc)
        return None

    # ── Step 3: Position flat — fetch actual exit price from recent orders ────
    actual_exit = 0.0
    outcome     = "SL"   # conservative default
    try:
        from datetime import datetime, timezone
        ts_str = trade.get("ts") or ""
        try:
            dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            entry_ts_ms = int(dt.timestamp() * 1000)
        except Exception:
            entry_ts_ms = 0

        hist_params = _sign({
            "symbol":    symbol,
            "startTime": max(entry_ts_ms - 1000, 0),
            "limit":     50,
            "orderId":   0,
        })
        async with session.get(
            f"{_BINANCE_BASE}/fapi/v1/allOrders",
            params=hist_params, headers=headers
        ) as resp:
            resp.raise_for_status()
            all_orders = await resp.json()

        # Find the filled reduce-only order (SL or TP)
        close_side = "SELL" if direction == "LONG" else "BUY"
        for o in reversed(all_orders):
            if (o.get("reduceOnly") and
                    o.get("side") == close_side and
                    o.get("status") == "FILLED"):
                fill_price = float(o.get("avgPrice") or o.get("stopPrice") or 0)
                order_type = o.get("type", "")
                if fill_price > 0:
                    actual_exit = fill_price
                    outcome = "TP" if "TAKE_PROFIT" in order_type else "SL"
                    break

    except Exception as exc:
        log.debug("_check_live_order allOrders (%s): %s", symbol, exc)

    # ── Step 4: Fallback — infer from price proximity to SL/TP levels ─────────
    if actual_exit <= 0:
        try:
            async with session.get(
                f"{_BINANCE_BASE}/fapi/v1/ticker/price",
                params={"symbol": symbol}
            ) as resp:
                resp.raise_for_status()
                price = float((await resp.json()).get("price", 0))
            if price > 0:
                actual_exit = tp if (
                    (direction == "LONG"  and price >= tp * 0.998) or
                    (direction == "SHORT" and price <= tp * 1.002)
                ) else sl
                outcome = "TP" if actual_exit == tp else "SL"
        except Exception:
            actual_exit = sl   # conservative: assume SL hit
            outcome = "SL"

    return (outcome, actual_exit)


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
