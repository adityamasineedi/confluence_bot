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

import yaml

import aiohttp

from data.binance_rest import _round_price, _make_sl_params

log = logging.getLogger(__name__)

_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "config.yaml")
with open(_CONFIG_PATH) as _f:
    _cfg = yaml.safe_load(_f)

_DB_PATH         = os.environ.get("DB_PATH", "confluence_bot.db")
_PAPER_MODE      = os.environ.get("PAPER_MODE", "0") == "1"
_POLL_INTERVAL_S = 30
_STALE_PRICE_S   = 120   # if cached price is older than 2 min, fetch via REST
_BINANCE_BASE    = os.environ.get("BINANCE_BASE_URL", "https://fapi.binance.com")
_BINANCE_DATA_BASE = os.environ.get("BINANCE_DATA_URL", "https://fapi.binance.com")
_API_KEY         = os.environ.get("BINANCE_API_KEY", "")
_SECRET          = os.environ.get("BINANCE_SECRET", "")
_REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=10)
TAKER_FEE_RATE   = 0.0005   # 0.05% per side (Binance Futures taker)

# Session-level guard: once a trade ID is detected as closed it stays here.
_closing_trade_ids: set[int] = set()


# ── Binance signing ────────────────────────────────────────────────────────────

def _sign(params: dict) -> dict:
    params["timestamp"] = int(time.time() * 1000)
    query = urllib.parse.urlencode(params)
    sig = hmac.new(_SECRET.encode(), query.encode(), hashlib.sha256).hexdigest()
    params["signature"] = sig
    return params


# ── Price with staleness guard ────────────────────────────────────────────────

async def _get_fresh_price(symbol: str, cache) -> float:
    """Return current price, falling back to REST if WebSocket data is stale.

    Checks whether the most recent 1m candle timestamp is within _STALE_PRICE_S.
    If stale (WebSocket disconnected), fetches price via Binance REST ticker.
    This prevents the monitor from sitting on an old price while the market moves.
    """
    # Try cache first
    price = cache.get_last_price(symbol)
    if price and price > 0:
        # Check freshness — 1m candle timestamp should be within _STALE_PRICE_S
        bars_1m = cache.get_ohlcv(symbol, window=1, tf="1m")
        if bars_1m:
            last_ts_s = bars_1m[-1]["ts"] / 1000.0
            age = time.time() - last_ts_s
            if age <= _STALE_PRICE_S:
                return price
            log.warning("Stale price for %s (%.0fs old) — fetching via REST", symbol, age)

    # REST fallback
    try:
        url = f"{_BINANCE_DATA_BASE}/fapi/v1/ticker/price"
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=5)
        ) as session:
            async with session.get(url, params={"symbol": symbol}) as resp:
                data = await resp.json()
            rest_price = float(data.get("price", 0))
            if rest_price > 0:
                log.info("REST price for %s: %.6f", symbol, rest_price)
                return rest_price
    except Exception as exc:
        log.warning("REST price fallback failed for %s: %s", symbol, exc)

    return price or 0.0


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


def _close_trade_db(trade: dict, exit_price: float) -> bool:
    """Mark a trade as FILLED with net PnL (after fees).

    Returns True only if this call changed the row.
    Uses WHERE status='OPEN' so that if two processes (or two DB rows for the
    same position) race to close the same trade, only the first UPDATE succeeds
    and returns True — preventing duplicate Telegram alerts.
    """
    pnl = _calc_net_pnl(trade, exit_price)
    closed_ts = datetime.now(timezone.utc).isoformat()
    try:
        with sqlite3.connect(_DB_PATH) as conn:
            cur = conn.execute(
                "UPDATE trades SET status='FILLED', exit_price=?, pnl_usdt=?, closed_ts=? "
                "WHERE id=? AND status='OPEN'",
                (exit_price, pnl, closed_ts, trade["id"]),
            )
            return cur.rowcount > 0
    except Exception as exc:
        log.warning("_close_trade_db(%s) failed: %s", trade["id"], exc)
        return False


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

    # ── Step 1: Check open orders — regular + algo (SL/TP are algo orders now) ──
    direction = trade["direction"]
    sl = float(trade["stop_loss"])
    tp = float(trade["take_profit"])
    bracket_open = 0
    try:
        # 1a. Legacy open orders (non-conditional)
        open_params = _sign({"symbol": symbol})
        async with session.get(
            f"{_BINANCE_BASE}/fapi/v1/openOrders",
            params=open_params, headers=headers
        ) as resp:
            resp.raise_for_status()
            open_orders = await resp.json()
        bracket_open += sum(
            1 for o in open_orders
            if o.get("reduceOnly") and o.get("symbol") == symbol
        )
    except Exception as exc:
        log.debug("_check_live_order openOrders (%s): %s", symbol, exc)

    try:
        # 1b. Algo open orders (SL/TP conditional orders — migrated Dec 2025)
        algo_params = _sign({"symbol": symbol})
        async with session.get(
            f"{_BINANCE_BASE}/fapi/v1/openAlgoOrders",
            params=algo_params, headers=headers
        ) as resp:
            resp.raise_for_status()
            algo_orders = await resp.json()
        if isinstance(algo_orders, list):
            bracket_open += sum(
                1 for o in algo_orders
                if o.get("symbol") == symbol and o.get("reduceOnly")
            )
    except Exception as exc:
        log.debug("_check_live_order openAlgoOrders (%s): %s", symbol, exc)

    if bracket_open >= 1:
        return None   # at least one bracket leg still live → position open

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

                # When SL has been moved to breakeven, price can oscillate
                # around entry before reaching TP.  Require price to actually
                # breach the BE level by a small buffer (0.05%) to avoid
                # closing a profitable trade on noise.
                entry = float(trade["entry"])
                sl_is_at_be = abs(sl - entry) / entry < 0.002 if entry > 0 else False
                if sl_is_at_be and hit_sl and not hit_tp:
                    # Only honour BE-stop if price actually lost money
                    if direction == "LONG" and price > entry * 0.9995:
                        hit_sl = False
                    elif direction == "SHORT" and price < entry * 1.0005:
                        hit_sl = False

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
    close_side  = "SELL" if direction == "LONG" else "BUY"

    # 3a. Check legacy allOrders
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

        for o in reversed(all_orders):
            if (o.get("reduceOnly") and
                    o.get("side") == close_side and
                    o.get("status") == "FILLED"):
                fill_price = float(o.get("avgPrice") or o.get("stopPrice") or 0)
                order_type = o.get("type", "")
                if fill_price > 0:
                    actual_exit = fill_price
                    if "TAKE_PROFIT" in order_type:
                        outcome = "TP"
                    elif "TRAILING_STOP" in order_type:
                        outcome = "TP"
                    else:
                        outcome = "SL"
                    break

    except Exception as exc:
        log.debug("_check_live_order allOrders (%s): %s", symbol, exc)

    # 3b. Check algo order history (SL/TP placed via Algo Order API)
    if actual_exit <= 0:
        try:
            algo_hist_params = _sign({"symbol": symbol, "algoType": "CONDITIONAL"})
            async with session.get(
                f"{_BINANCE_BASE}/fapi/v1/allAlgoOrders",
                params=algo_hist_params, headers=headers
            ) as resp:
                resp.raise_for_status()
                algo_all = await resp.json()
            if isinstance(algo_all, list):
                for o in reversed(algo_all):
                    if (o.get("symbol") == symbol and
                            o.get("side") == close_side and
                            o.get("algoStatus") == "TRIGGERED"):
                        fill_price = float(o.get("actualPrice") or
                                           o.get("triggerPrice") or 0)
                        order_type = o.get("orderType", o.get("type", ""))
                        if fill_price > 0:
                            actual_exit = fill_price
                            if "TAKE_PROFIT" in order_type:
                                outcome = "TP"
                            elif "TRAILING_STOP" in order_type:
                                outcome = "TP"
                            else:
                                outcome = "SL"
                            break
        except Exception as exc:
            log.debug("_check_live_order allAlgoOrders (%s): %s", symbol, exc)

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


# ── Breakeven move ────────────────────────────────────────────────────────────

async def _check_breakeven(trade: dict, session: aiohttp.ClientSession, cache) -> bool:
    """Move SL to entry when price reaches configurable R-multiple.

    Skips strategies that were backtested WITHOUT breakeven management
    (applying it live would degrade performance vs the validated backtest).
    For all other strategies, trigger is configurable via breakeven_trigger_r
    (default 2.0 — was 1.0, too aggressive on 5M scalps).

    Cancels only the SL order (not TP). Skips if already at breakeven.
    """
    # Skip BE for strategies that did not use it in backtest
    _risk_cfg = _cfg.get("risk", {})
    _be_disabled = set(
        s.lower()
        for s in _risk_cfg.get("breakeven_disabled_strategies", [])
    )
    regime = trade.get("regime", "")
    if regime.lower() in _be_disabled:
        return False

    symbol    = trade["symbol"]
    direction = trade["direction"]
    entry     = float(trade["entry"])
    sl        = float(trade["stop_loss"])

    if direction == "LONG"  and sl >= entry: return False
    if direction == "SHORT" and sl <= entry: return False

    stop_dist = abs(entry - sl)
    be_trigger_r = float(_risk_cfg.get("breakeven_trigger_r", 2.0))
    be_target = (entry + stop_dist * be_trigger_r
                 if direction == "LONG"
                 else entry - stop_dist * be_trigger_r)
    price = cache.get_last_price(symbol)
    if not price: return False

    reached = (direction == "LONG"  and price >= be_target) or \
              (direction == "SHORT" and price <= be_target)
    if not reached: return False

    headers    = {"X-MBX-APIKEY": _API_KEY}
    close_side = "SELL" if direction == "LONG" else "BUY"

    # Step 1: find the existing SL algo order — cancel only that, leave TP intact
    sl_algo_id = None

    # 1a. Check algo orders (SL/TP placed via Algo Order API since Dec 2025)
    try:
        async with session.get(
            f"{_BINANCE_BASE}/fapi/v1/openAlgoOrders",
            params=_sign({"symbol": symbol}), headers=headers,
        ) as r:
            algo_orders = await r.json()
        if isinstance(algo_orders, list):
            for o in algo_orders:
                order_type = o.get("orderType", o.get("type", ""))
                if (order_type in ("STOP_MARKET", "STOP") and
                        o.get("reduceOnly") and
                        o.get("side") == close_side and
                        o.get("symbol") == symbol):
                    sl_algo_id = o.get("algoId")
                    break
    except Exception as exc:
        log.warning("Breakeven: failed to fetch open algo orders %s: %s", symbol, exc)

    # 1b. Fallback: check legacy open orders (in case older SL was placed pre-migration)
    sl_legacy_id = None
    if not sl_algo_id:
        try:
            async with session.get(
                f"{_BINANCE_BASE}/fapi/v1/openOrders",
                params=_sign({"symbol": symbol}), headers=headers,
            ) as r:
                open_orders = await r.json()
            for o in open_orders:
                if (o.get("type") in ("STOP_MARKET", "STOP") and
                        o.get("reduceOnly") and
                        o.get("side") == close_side):
                    sl_legacy_id = o.get("orderId")
                    break
        except Exception as exc:
            log.warning("Breakeven: failed to fetch legacy open orders %s: %s", symbol, exc)

    if not sl_algo_id and not sl_legacy_id:
        log.debug("Breakeven: no SL order found to cancel for %s", symbol)

    # Step 2: cancel the existing SL order
    if sl_algo_id:
        try:
            async with session.delete(
                f"{_BINANCE_BASE}/fapi/v1/algoOrder",
                params=_sign({"algoId": sl_algo_id}),
                headers=headers,
            ) as r:
                cancel_resp = await r.json()
            if str(cancel_resp.get("code", "200")) not in ("200", "0"):
                log.warning("Breakeven: cancel algo SL failed %s: %s", symbol, cancel_resp)
                return False
        except Exception as exc:
            log.warning("Breakeven: cancel algo SL error %s: %s", symbol, exc)
            return False
    elif sl_legacy_id:
        try:
            async with session.delete(
                f"{_BINANCE_BASE}/fapi/v1/order",
                params=_sign({"symbol": symbol, "orderId": sl_legacy_id}),
                headers=headers,
            ) as r:
                cancel_resp = await r.json()
            if isinstance(cancel_resp.get("code"), int) and cancel_resp["code"] < 0:
                log.warning("Breakeven: cancel legacy SL failed %s: %s", symbol, cancel_resp)
                return False
        except Exception as exc:
            log.warning("Breakeven: cancel legacy SL error %s: %s", symbol, exc)
            return False

    # Step 3: place new SL at entry + fee buffer (true breakeven after fees)
    # Without buffer, a "breakeven" trade loses 0.05% taker fee on exit.
    # Buffer = round-trip fees: 0.05% entry + 0.05% exit = 0.10%
    _FEE_BUFFER_PCT = 0.001   # 0.10% — covers round-trip taker fees
    if direction == "LONG":
        be_price = entry * (1.0 + _FEE_BUFFER_PCT)   # SL slightly above entry
    else:
        be_price = entry * (1.0 - _FEE_BUFFER_PCT)   # SL slightly below entry
    new_sl_params = _sign(
        _make_sl_params(symbol, close_side, float(trade["size"]), be_price)
    )
    try:
        async with session.post(
            f"{_BINANCE_BASE}/fapi/v1/algoOrder",
            params=new_sl_params, headers=headers,
        ) as r:
            res = await r.json()
        if isinstance(res.get("code"), int) and int(res["code"]) < 0:
            log.warning("Breakeven: new SL rejected %s: %s (updating DB only)", symbol, res)
            with sqlite3.connect(_DB_PATH) as conn:
                conn.execute(
                    "UPDATE trades SET stop_loss=? WHERE id=?",
                    (_round_price(symbol, be_price), trade["id"]),
                )
            log.info("Breakeven: SL updated in DB to %.6f (entry+fees) for %s %s (software SL active)",
                     be_price, direction, symbol)
            return True
        with sqlite3.connect(_DB_PATH) as conn:
            conn.execute(
                "UPDATE trades SET stop_loss=? WHERE id=?",
                (_round_price(symbol, be_price), trade["id"]),
            )
        log.info("Breakeven: SL moved to %.6f (entry %.6f + fee buffer) for %s %s",
                 be_price, entry, direction, symbol)
        return True
    except Exception as exc:
        log.warning("Breakeven: new SL error %s: %s", symbol, exc)
        return False


# ── Regime-flip exit ───────────────────────────────────────────────────────────

def _regime_conflicts(trade: dict, cache) -> bool:
    """Return True if current regime conflicts with open trade direction.

    LONG trades should be closed if regime flips to CRASH.
    SHORT trades should be closed if regime flips to PUMP.
    """
    from core.regime_detector import _detector

    symbol    = trade["symbol"]
    direction = trade["direction"]

    try:
        regime = str(_detector.detect(symbol, cache))
    except Exception:
        return False   # can't detect — don't close

    if direction == "LONG"  and regime == "CRASH":
        return True
    if direction == "SHORT" and regime == "PUMP":
        return True
    return False


async def _force_regime_close(trade: dict, session: aiohttp.ClientSession, cache) -> bool:
    """Place a market close order and update the DB for a regime-flip exit.

    Returns True if the close was successfully recorded.
    """
    symbol    = trade["symbol"]
    direction = trade["direction"]
    headers   = {"X-MBX-APIKEY": _API_KEY}
    close_side = "SELL" if direction == "LONG" else "BUY"

    price = cache.get_last_price(symbol) or 0.0

    try:
        qty = float(trade["size"])
        qty = int(qty) if qty == int(qty) else qty
        close_params = _sign({
            "symbol":     symbol,
            "side":       close_side,
            "type":       "MARKET",
            "quantity":   qty,
            "reduceOnly": "true",
        })
        async with session.post(
            f"{_BINANCE_BASE}/fapi/v1/order",
            params=close_params, headers=headers,
        ) as resp:
            close_resp = await resp.json()
        if close_resp.get("orderId"):
            fill_price = float(close_resp.get("avgPrice") or price or trade["entry"])
        else:
            log.warning("Regime flip market close rejected for %s: %s", symbol, close_resp)
            fill_price = price or float(trade["entry"])
    except Exception as exc:
        log.warning("Regime flip market close error %s: %s", symbol, exc)
        fill_price = price or float(trade["entry"])

    return await asyncio.to_thread(_close_trade_db, trade, fill_price)


# ── PnL ───────────────────────────────────────────────────────────────────────

def _calc_net_pnl(trade: dict, exit_price: float) -> float:
    """Net PnL after taker fees on both entry and exit legs."""
    entry     = float(trade["entry"])
    size      = float(trade["size"])
    direction = trade["direction"]

    # Gross PnL
    if direction == "LONG":
        gross = (exit_price - entry) * size
    else:
        gross = (entry - exit_price) * size

    # Taker fee on both entry and exit legs
    entry_fee = entry      * size * TAKER_FEE_RATE
    exit_fee  = exit_price * size * TAKER_FEE_RATE

    return round(gross - entry_fee - exit_fee, 4)


# ── Main coroutine ────────────────────────────────────────────────────────────

async def _close_orphaned_trades(cache) -> None:
    """Run once at startup — close any OPEN trades where price has blown past SL.

    This catches positions orphaned by bot restarts where the exchange SL
    order was lost or the trade monitor wasn't running when SL was hit.
    """
    trades = await asyncio.to_thread(_load_open_trades)
    if not trades:
        return

    closed = 0
    for trade in trades:
        symbol    = trade["symbol"]
        direction = trade["direction"]
        entry     = float(trade["entry"])
        sl        = float(trade["stop_loss"])
        size      = float(trade.get("size", 0))

        price = cache.get_last_price(symbol) if cache else 0
        if not price:
            continue

        # Check if SL was blown
        sl_blown = False
        if direction == "LONG" and price < sl:
            sl_blown = True
        elif direction == "SHORT" and price > sl:
            sl_blown = True

        if not sl_blown:
            continue

        sl_dist_pct = abs(price - sl) / sl * 100
        log.warning(
            "ORPHANED TRADE: %s %s entry=%.4f SL=%.4f price=%.4f (%.1f%% past SL) — force closing",
            direction, symbol, entry, sl, price, sl_dist_pct,
        )

        # Cancel any stale exchange orders (TP/SL left over)
        if not _PAPER_MODE:
            try:
                from data.binance_rest import cancel_all_orders
                await cancel_all_orders(symbol)
                log.info("Orphan cleanup: cancelled stale exchange orders for %s", symbol)
            except Exception as exc:
                log.debug("Orphan cleanup: cancel orders failed %s: %s", symbol, exc)

        # Close at current price (SL was already blown)
        pnl = _calc_net_pnl(entry, price, size, direction)
        try:
            from core.executor import close_deal
            await close_deal(symbol, direction, exit_price=price, pnl_usdt=pnl)
        except Exception as exc:
            log.error("Failed to close orphaned trade %s %s: %s", direction, symbol, exc)
            # Fallback: update DB directly
            try:
                from datetime import datetime, timezone
                now = datetime.now(timezone.utc).isoformat()
                with sqlite3.connect(_DB_PATH) as conn:
                    conn.execute(
                        "UPDATE trades SET status='FILLED', exit_price=?, pnl_usdt=?, "
                        "closed_ts=? WHERE id=?",
                        (price, round(pnl, 2), now, trade["id"]),
                    )
                log.info("Orphaned trade closed in DB: %s %s pnl=%.2f", direction, symbol, pnl)
            except Exception as db_exc:
                log.error("DB fallback close also failed: %s", db_exc)

        try:
            from notifications.telegram import send_trade_close
            await send_trade_close(trade, "ORPHAN_SL", price, pnl)
        except Exception:
            pass

        closed += 1

    if closed:
        log.warning("Startup orphan check: closed %d trades with blown SL", closed)
    else:
        log.info("Startup orphan check: all open trades OK")


async def monitor_trades(cache) -> None:
    """
    Continuously polls open trades and closes them when TP/SL is hit.
    Launch as an asyncio task — runs forever.
    """
    log.info(
        "Trade monitor started  paper=%s  poll=%.0fs",
        _PAPER_MODE, _POLL_INTERVAL_S,
    )

    # Run orphan check once at startup
    try:
        await _close_orphaned_trades(cache)
    except Exception as exc:
        log.error("Orphan check failed: %s", exc)

    while True:
        await asyncio.sleep(_POLL_INTERVAL_S)

        trades = await asyncio.to_thread(_load_open_trades)
        if not trades:
            continue

        async with aiohttp.ClientSession(timeout=_REQUEST_TIMEOUT) as session:
            for trade in trades:
                try:
                    trade_id = trade["id"]
                    # Skip if already being closed this session (prevents duplicate
                    # close alerts when multiple DB rows exist for the same position)
                    if trade_id in _closing_trade_ids:
                        continue

                    if not _PAPER_MODE:
                        # Move SL to breakeven once price reaches +2R
                        await _check_breakeven(trade, session, cache)

                        # Force-close if regime flipped against the trade direction
                        if _regime_conflicts(trade, cache):
                            log.warning(
                                "Regime flip forced close: %s %s",
                                trade["direction"], trade["symbol"],
                            )
                            _closing_trade_ids.add(trade_id)
                            row_claimed = await _force_regime_close(trade, session, cache)
                            if row_claimed:
                                from core.executor import close_deal
                                close_deal(trade["symbol"], trade["direction"])
                                if not _PAPER_MODE:
                                    from data.binance_rest import refresh_account_balance
                                    await refresh_account_balance()
                                # Use actual market price, not entry — entry gives ~$0 PnL
                                flip_exit = cache.get_last_price(trade["symbol"]) or float(trade["entry"])
                                pnl = _calc_net_pnl(trade, flip_exit)
                                log.info(
                                    "Regime flip closed: %s %s  pnl≈%+.2f USDT",
                                    trade["direction"], trade["symbol"], pnl,
                                )
                                try:
                                    from notifications.telegram import send_trade_close
                                    await send_trade_close(trade, "REGIME_FLIP",
                                                           float(trade["entry"]), pnl)
                                except Exception:
                                    pass
                            continue

                    if _PAPER_MODE:
                        result = _check_paper_order(trade, cache)
                    else:
                        result = await _check_live_order(trade, session)

                    if result is None:
                        continue

                    # Claim the trade ID before any await — atomic in asyncio
                    _closing_trade_ids.add(trade_id)
                    outcome, exit_price = result
                    pnl = _calc_net_pnl(trade, exit_price) if exit_price > 0 else 0.0

                    if outcome == "CANCELLED":
                        await asyncio.to_thread(_cancel_trade_db, trade["id"])
                        row_claimed = True
                    else:
                        # Atomic: only the first UPDATE (status='OPEN' guard) returns True.
                        # Handles both duplicate DB rows and two concurrent bot processes.
                        row_claimed = await asyncio.to_thread(
                            _close_trade_db, trade, exit_price
                        )

                    if not row_claimed:
                        # Another process already closed this row — skip notification.
                        log.debug("Silently skipped already-closed trade row id=%d %s %s",
                                  trade_id, trade["direction"], trade["symbol"])
                        continue

                    # Remove from executor's active set + refresh balance for compounding
                    from core.executor import close_deal
                    close_deal(trade["symbol"], trade["direction"])
                    if not _PAPER_MODE:
                        from data.binance_rest import refresh_account_balance
                        await refresh_account_balance()
                    else:
                        # Paper mode: compound by adding PnL to cached balance
                        from data.cache import _global_cache
                        if _global_cache is not None:
                            old_bal = _global_cache.get_account_balance()
                            _global_cache.set_account_balance(old_bal + pnl)

                    # Log gross vs net for fee-drag tracking
                    entry_f = float(trade["entry"])
                    size_f  = float(trade["size"])
                    gross = (exit_price - entry_f) * size_f if trade["direction"] == "LONG" \
                        else (entry_f - exit_price) * size_f
                    log.info(
                        "Trade closed %s %s: gross=%.4f  fees=%.4f  net=%.4f  outcome=%s",
                        trade["direction"], trade["symbol"],
                        gross, abs(pnl - gross), pnl, outcome,
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
