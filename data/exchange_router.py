"""Exchange router — unified order/position API via ccxt.

All callers (executor.py, trade_monitor.py, main.py) import from here.
On startup, configure() creates a ccxt async exchange instance.
Default: Binance Futures (backwards compatible — uses binance_rest directly).

When the active exchange is "binance", we delegate to the existing
binance_rest module (battle-tested, Algo Order API for SL/TP).
For all other exchanges, ccxt handles auth, signing, and order placement.
"""
import asyncio
import logging
import os
import sqlite3
import time

log = logging.getLogger(__name__)

_ACTIVE_EXCHANGE: str = "binance"
_ccxt_instance = None  # ccxt.async_support exchange object (non-Binance only)

# ccxt exchange class names
_CCXT_CLASS_MAP = {
    "binance":  "binanceusdm",
    "bybit":    "bybit",
    "okx":      "okx",
    "bitget":   "bitget",
    "bingx":    "bingx",
}

# Price/qty decimals — shared across all exchanges
_PRICE_DECIMALS: dict[str, int] = {
    "BTCUSDT": 1, "ETHUSDT": 2, "SOLUSDT": 2, "BNBUSDT": 2,
    "XRPUSDT": 4, "LINKUSDT": 3, "DOGEUSDT": 5, "SUIUSDT": 4,
    "ADAUSDT": 4, "AVAXUSDT": 2, "TAOUSDT": 2,
}
_QTY_DECIMALS: dict[str, int] = {
    "BTCUSDT": 3, "ETHUSDT": 3, "SOLUSDT": 1, "BNBUSDT": 2,
    "XRPUSDT": 0, "LINKUSDT": 1, "DOGEUSDT": 0, "SUIUSDT": 0,
    "ADAUSDT": 0, "AVAXUSDT": 1, "TAOUSDT": 2,
}


def _round_price(symbol: str, price: float) -> float:
    dp = _PRICE_DECIMALS.get(symbol.upper(), 2)
    return round(price, dp)


def _round_qty(symbol: str, qty: float) -> float:
    dp = _QTY_DECIMALS.get(symbol.upper(), 3)
    rounded = round(qty, dp)
    return int(rounded) if dp == 0 else rounded


# ── Configuration ────────────────────────────────────────────────────────────

def set_exchange(name: str) -> None:
    """Set the active exchange name. Called from exchange_manager."""
    global _ACTIVE_EXCHANGE
    _ACTIVE_EXCHANGE = name.lower()
    log.info("Exchange router set to: %s", _ACTIVE_EXCHANGE)


def get_exchange() -> str:
    return _ACTIVE_EXCHANGE


def configure(exchange: str, api_key: str, api_secret: str,
              passphrase: str = "", testnet: bool = False) -> None:
    """Create a ccxt async exchange instance for non-Binance exchanges."""
    global _ccxt_instance, _ACTIVE_EXCHANGE
    _ACTIVE_EXCHANGE = exchange.lower()

    if _ACTIVE_EXCHANGE == "binance":
        # Binance uses existing binance_rest module
        from data.binance_rest import configure_credentials
        base = "https://testnet.binancefuture.com" if testnet else None
        configure_credentials(api_key, api_secret, base)
        log.info("Exchange router: Binance configured via binance_rest")
        return

    import ccxt.async_support as ccxt

    cls_name = _CCXT_CLASS_MAP.get(_ACTIVE_EXCHANGE)
    if not cls_name or not hasattr(ccxt, cls_name):
        raise ValueError(f"Unsupported exchange for ccxt: {_ACTIVE_EXCHANGE}")

    cls = getattr(ccxt, cls_name)
    config = {
        "apiKey": api_key,
        "secret": api_secret,
        "enableRateLimit": True,
        "options": {"defaultType": "swap"},  # futures/perpetual
    }
    if passphrase:
        config["password"] = passphrase
    if testnet:
        config["sandbox"] = True

    _ccxt_instance = cls(config)
    log.info("Exchange router: ccxt %s instance created (testnet=%s)", cls_name, testnet)


async def _close_ccxt() -> None:
    """Close ccxt session (call on shutdown)."""
    global _ccxt_instance
    if _ccxt_instance:
        await _ccxt_instance.close()
        _ccxt_instance = None


def _use_binance() -> bool:
    return _ACTIVE_EXCHANGE == "binance"


def _ex():
    """Return the active ccxt exchange instance."""
    if _ccxt_instance is None:
        raise RuntimeError("Exchange router: ccxt instance not configured. "
                           "Call configure() or set an active exchange in the UI.")
    return _ccxt_instance


# ── Symbol mapping ───────────────────────────────────────────────────────────
# ccxt uses unified symbol format: "BTC/USDT:USDT" for linear perpetuals

def _to_ccxt(symbol: str) -> str:
    """Convert Binance-style "BTCUSDT" to ccxt unified "BTC/USDT:USDT"."""
    base = symbol.replace("USDT", "")
    return f"{base}/USDT:USDT"


# ── Unified API ──────────────────────────────────────────────────────────────

async def setup_symbols(symbols: list[str], leverage: int,
                        margin_type: str = "ISOLATED") -> None:
    """Set leverage and margin type for all symbols."""
    if _use_binance():
        from data.binance_rest import setup_symbols as _setup
        await _setup(symbols, leverage, margin_type)
        return

    ex = _ex()
    await ex.load_markets()
    for sym in symbols:
        csym = _to_ccxt(sym)
        try:
            await ex.set_leverage(leverage, csym)
        except Exception as exc:
            if "already" not in str(exc).lower():
                log.warning("ccxt set_leverage %s %dx: %s", sym, leverage, exc)
        try:
            mt = margin_type.lower()  # "isolated" or "cross"
            await ex.set_margin_mode(mt, csym)
        except Exception as exc:
            if "already" not in str(exc).lower():
                log.warning("ccxt set_margin_mode %s %s: %s", sym, mt, exc)
    log.info("ccxt symbol setup complete: leverage=%dx margin=%s symbols=%s",
             leverage, margin_type, symbols)


async def get_account_balance() -> float:
    """Fetch USDT available balance."""
    if _use_binance():
        from data.binance_rest import get_account_balance as _get
        return await _get()

    ex = _ex()
    try:
        balance = await ex.fetch_balance()
        usdt = balance.get("USDT", {})
        return float(usdt.get("free", 0))
    except Exception as exc:
        log.error("ccxt get_account_balance failed: %s", exc)
        return 0.0


async def refresh_account_balance() -> float:
    """Fetch balance and update cache + DB."""
    bal = await get_account_balance()
    if bal > 0:
        try:
            from data.cache import _global_cache
            if _global_cache:
                _global_cache.set_account_balance(bal)
            from datetime import datetime, timezone
            db = os.environ.get("DB_PATH", "confluence_bot.db")
            with sqlite3.connect(db) as c:
                c.execute(
                    "INSERT OR REPLACE INTO bot_state(key,value,updated) VALUES(?,?,?)",
                    ("account_balance", str(bal), datetime.now(timezone.utc).isoformat())
                )
        except Exception:
            pass
    return bal


async def get_position_amt(symbol: str) -> float:
    """Return current position size (positive=LONG, negative=SHORT, 0=flat)."""
    if _use_binance():
        from data.binance_rest import get_position_amt as _get
        return await _get(symbol)

    ex = _ex()
    csym = _to_ccxt(symbol)
    try:
        positions = await ex.fetch_positions([csym])
        for pos in positions:
            contracts = float(pos.get("contracts", 0))
            if contracts > 0:
                side = pos.get("side", "")
                return contracts if side == "long" else -contracts
    except Exception as exc:
        log.debug("ccxt get_position_amt(%s): %s", symbol, exc)
    return 0.0


async def fetch_all_positions() -> list[dict]:
    """Fetch all open positions from the active exchange.

    Returns list of dicts with: symbol, direction, size, entry, mark_price,
    unrealized_pnl, leverage, margin_type.
    """
    if _use_binance():
        from data.binance_rest import fetch_all_positions as _fetch
        return await _fetch()

    ex = _ex()
    try:
        positions = await ex.fetch_positions()
        result = []
        for pos in positions:
            contracts = float(pos.get("contracts", 0))
            if contracts <= 0:
                continue
            side = pos.get("side", "")
            sym_raw = pos.get("info", {}).get("symbol", "") or pos.get("symbol", "")
            # ccxt symbol "BTC/USDT:USDT" → "BTCUSDT"
            sym = sym_raw.replace("/", "").replace(":USDT", "")
            result.append({
                "symbol": sym,
                "direction": "LONG" if side == "long" else "SHORT",
                "size": contracts,
                "entry": float(pos.get("entryPrice", 0)),
                "mark_price": float(pos.get("markPrice", 0)),
                "unrealized_pnl": float(pos.get("unrealizedPnl", 0)),
                "leverage": int(pos.get("leverage", 1)),
                "margin_type": pos.get("marginMode", "").upper(),
            })
        return result
    except Exception as exc:
        log.warning("ccxt fetch_all_positions failed: %s", exc)
        return []


async def place_limit_then_market(
    symbol: str,
    side: str,
    quantity: float,
    limit_price: float,
    stop: float,
    take_profit: float | None,
    timeout_s: float = 30.0,
) -> dict:
    """Place entry (LIMIT → fallback MARKET) + SL/TP bracket.

    Returns dict with executedQty > 0 on success, {} on failure.
    """
    if _use_binance():
        from data.binance_rest import place_limit_then_market as _place
        return await _place(symbol, side, quantity, limit_price, stop,
                            take_profit, timeout_s)

    ex = _ex()
    csym = _to_ccxt(symbol)
    qty = _round_qty(symbol, quantity)
    ccxt_side = side.lower()  # "buy" or "sell"

    # Step 1: LIMIT entry
    try:
        order = await ex.create_order(
            csym, "limit", ccxt_side, qty,
            _round_price(symbol, limit_price),
        )
        order_id = order.get("id", "")
        log.info("ccxt LIMIT entry placed %s %s @ %.4f orderId=%s",
                 side, symbol, limit_price, order_id)
    except Exception as exc:
        log.error("ccxt LIMIT entry failed %s %s: %s", side, symbol, exc)
        return {}

    # Step 2: wait for fill
    await asyncio.sleep(timeout_s)

    # Step 3: check fill
    filled_qty = 0.0
    try:
        detail = await ex.fetch_order(order_id, csym)
        filled_qty = float(detail.get("filled", 0))
        status = detail.get("status", "")
    except Exception:
        status = ""

    if filled_qty <= 0 or status not in ("closed", "filled", "partially_filled"):
        # Cancel LIMIT, try MARKET
        try:
            await ex.cancel_order(order_id, csym)
        except Exception:
            pass
        log.info("ccxt LIMIT unfilled after %.0fs — MARKET fallback %s %s",
                 timeout_s, side, symbol)
        try:
            order = await ex.create_order(csym, "market", ccxt_side, qty)
            order_id = order.get("id", "")
            await asyncio.sleep(2)
            detail = await ex.fetch_order(order_id, csym)
            filled_qty = float(detail.get("filled", 0))
            log.info("ccxt MARKET filled %s %s qty=%.4f", side, symbol, filled_qty)
        except Exception as exc:
            log.error("ccxt MARKET failed %s %s: %s", side, symbol, exc)
            return {}

    if filled_qty <= 0:
        log.warning("ccxt entry qty=0 for %s %s — no trade", side, symbol)
        return {}

    # Step 4: verify position
    await asyncio.sleep(1)
    pos_amt = await get_position_amt(symbol)
    if abs(pos_amt) < 0.0001:
        log.warning("ccxt PHANTOM FILL: %s %s reports qty=%.4f but position=0",
                    side, symbol, filled_qty)
        await cancel_all_orders(symbol)
        return {}

    # Step 5: place SL + TP
    bracket_qty = _round_qty(symbol, filled_qty)
    close_side = "sell" if ccxt_side == "buy" else "buy"

    # SL
    try:
        sl_params = {"stopLossPrice": _round_price(symbol, stop),
                     "reduceOnly": True}
        await ex.create_order(
            csym, "market", close_side, bracket_qty,
            None, sl_params,
        )
        log.info("ccxt SL placed %s @ %.4f", symbol, stop)
    except Exception as exc:
        # Many exchanges support stop_loss as trigger order type instead
        try:
            await ex.create_order(
                csym, "stop_market", close_side, bracket_qty,
                _round_price(symbol, stop),
                {"reduceOnly": True, "triggerPrice": _round_price(symbol, stop)},
            )
            log.info("ccxt SL placed %s @ %.4f (trigger)", symbol, stop)
        except Exception as exc2:
            log.warning("ccxt SL rejected %s: %s / %s — software SL will protect",
                        symbol, exc, exc2)

    # TP
    if take_profit is not None:
        try:
            tp_params = {"takeProfitPrice": _round_price(symbol, take_profit),
                         "reduceOnly": True}
            await ex.create_order(
                csym, "market", close_side, bracket_qty,
                None, tp_params,
            )
            log.info("ccxt TP placed %s @ %.4f", symbol, take_profit)
        except Exception as exc:
            try:
                await ex.create_order(
                    csym, "take_profit_market", close_side, bracket_qty,
                    _round_price(symbol, take_profit),
                    {"reduceOnly": True,
                     "triggerPrice": _round_price(symbol, take_profit)},
                )
                log.info("ccxt TP placed %s @ %.4f (trigger)", symbol, take_profit)
            except Exception as exc2:
                log.warning("ccxt TP rejected %s: %s / %s", symbol, exc, exc2)

    result = {
        "orderId": order_id,
        "executedQty": filled_qty,
        "symbol": symbol,
        "side": side,
        "exchange": _ACTIVE_EXCHANGE,
    }
    log.info("ccxt order placed: %s %s qty=%.4f on %s",
             side, symbol, filled_qty, _ACTIVE_EXCHANGE)
    return result


async def place_trailing_stop(
    symbol: str, side: str, quantity: float,
    activation_pct: float, callback_pct: float,
) -> dict:
    """Place trailing stop order."""
    if _use_binance():
        from data.binance_rest import place_trailing_stop as _trail
        return await _trail(symbol, side, quantity, activation_pct, callback_pct)

    ex = _ex()
    csym = _to_ccxt(symbol)
    qty = _round_qty(symbol, quantity)
    ccxt_side = side.lower()
    clamped = round(max(0.1, min(5.0, callback_pct)), 1)

    try:
        order = await ex.create_order(
            csym, "trailing_stop_market", ccxt_side, qty,
            None,
            {"callbackRate": clamped, "reduceOnly": True},
        )
        log.info("ccxt trailing stop placed %s callback=%.1f%%", symbol, clamped)
        return order
    except Exception as exc:
        log.warning("ccxt trailing stop rejected %s: %s", symbol, exc)
        return {}


async def cancel_order(symbol: str, order_id) -> dict:
    """Cancel an order by ID."""
    if _use_binance():
        from data.binance_rest import cancel_order as _cancel
        return await _cancel(symbol, order_id)

    ex = _ex()
    csym = _to_ccxt(symbol)
    try:
        return await ex.cancel_order(str(order_id), csym)
    except Exception as exc:
        log.debug("ccxt cancel_order %s %s: %s", symbol, order_id, exc)
        return {}


async def cancel_all_orders(symbol: str) -> int:
    """Cancel all open orders for a symbol."""
    if _use_binance():
        from data.binance_rest import cancel_all_orders as _cancel
        return await _cancel(symbol)

    ex = _ex()
    csym = _to_ccxt(symbol)
    try:
        orders = await ex.cancel_all_orders(csym)
        log.info("ccxt cancelled all orders for %s", symbol)
        return len(orders) if isinstance(orders, list) else 1
    except Exception as exc:
        log.debug("ccxt cancel_all_orders %s: %s", symbol, exc)
        return 0
