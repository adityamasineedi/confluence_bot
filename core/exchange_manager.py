"""Exchange configuration manager — stores API keys and tests connectivity.

Supports: Binance Futures, Bybit, OKX, Bitget, BingX.
Uses ccxt for unified connectivity tests across all exchanges.
Configs are persisted to exchanges.json (gitignored) with base64-encoded secrets.
"""
import base64
import json
import logging
import os
import time

log = logging.getLogger(__name__)

_CONFIG_FILE = os.path.join(os.path.dirname(__file__), "..", "exchanges.json")

SUPPORTED_EXCHANGES = ["binance", "bybit", "okx", "bitget", "bingx"]

# ── Storage ──────────────────────────────────────────────────────────────────

def _encode(s: str) -> str:
    return base64.b64encode(s.encode()).decode()

def _decode(s: str) -> str:
    return base64.b64decode(s.encode()).decode()


def load_exchanges() -> list[dict]:
    """Load all exchange configs. Returns list of dicts (secrets are decoded)."""
    if not os.path.exists(_CONFIG_FILE):
        return []
    try:
        with open(_CONFIG_FILE) as f:
            raw = json.load(f)
        result = []
        for ex in raw:
            result.append({
                "id": ex["id"],
                "name": ex["name"],
                "exchange": ex["exchange"],
                "api_key": _decode(ex["api_key"]),
                "api_secret": _decode(ex["api_secret"]),
                "passphrase": _decode(ex["passphrase"]) if ex.get("passphrase") else "",
                "testnet": ex.get("testnet", False),
                "active": ex.get("active", False),
                "created_at": ex.get("created_at", ""),
            })
        return result
    except Exception as exc:
        log.error("Failed to load exchanges.json: %s", exc)
        return []


def _save_exchanges(exchanges: list[dict]) -> None:
    """Persist exchange configs to disk (secrets base64-encoded)."""
    raw = []
    for ex in exchanges:
        raw.append({
            "id": ex["id"],
            "name": ex["name"],
            "exchange": ex["exchange"],
            "api_key": _encode(ex["api_key"]),
            "api_secret": _encode(ex["api_secret"]),
            "passphrase": _encode(ex["passphrase"]) if ex.get("passphrase") else "",
            "testnet": ex.get("testnet", False),
            "active": ex.get("active", False),
            "created_at": ex.get("created_at", ""),
        })
    with open(_CONFIG_FILE, "w") as f:
        json.dump(raw, f, indent=2)


def add_exchange(name: str, exchange: str, api_key: str, api_secret: str,
                 passphrase: str = "", testnet: bool = False) -> dict:
    """Add a new exchange config. Returns the created entry."""
    if exchange not in SUPPORTED_EXCHANGES:
        raise ValueError(f"Unsupported exchange: {exchange}")
    exchanges = load_exchanges()
    entry = {
        "id": f"{exchange}_{int(time.time())}",
        "name": name,
        "exchange": exchange,
        "api_key": api_key,
        "api_secret": api_secret,
        "passphrase": passphrase,
        "testnet": testnet,
        "active": len(exchanges) == 0,  # first one is active by default
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    exchanges.append(entry)
    _save_exchanges(exchanges)
    return entry


def delete_exchange(ex_id: str) -> bool:
    """Delete an exchange config by ID."""
    exchanges = load_exchanges()
    before = len(exchanges)
    exchanges = [e for e in exchanges if e["id"] != ex_id]
    if len(exchanges) == before:
        return False
    _save_exchanges(exchanges)
    return True


def set_active(ex_id: str) -> bool:
    """Set one exchange as the active trading exchange."""
    exchanges = load_exchanges()
    found = False
    for ex in exchanges:
        if ex["id"] == ex_id:
            ex["active"] = True
            found = True
        else:
            ex["active"] = False
    if not found:
        return False
    _save_exchanges(exchanges)
    return found


def get_active_exchange() -> dict | None:
    """Return the currently active exchange config, or None."""
    for ex in load_exchanges():
        if ex.get("active"):
            return ex
    return None


def list_exchanges_safe() -> list[dict]:
    """Return exchange list with masked secrets (for UI display)."""
    result = []
    for ex in load_exchanges():
        result.append({
            "id": ex["id"],
            "name": ex["name"],
            "exchange": ex["exchange"],
            "api_key_masked": ex["api_key"][:6] + "..." + ex["api_key"][-4:] if len(ex["api_key"]) > 10 else "***",
            "testnet": ex.get("testnet", False),
            "active": ex.get("active", False),
            "created_at": ex.get("created_at", ""),
        })
    return result


# ── Connectivity tests (unified via ccxt) ────────────────────────────────────

# ccxt exchange class names for futures
_CCXT_CLASS_MAP = {
    "binance": "binanceusdm",
    "bybit":   "bybit",
    "okx":     "okx",
    "bitget":  "bitget",
    "bingx":   "bingx",
}


async def _test_via_ccxt(exchange: str, api_key: str, api_secret: str,
                         passphrase: str = "", testnet: bool = False) -> dict:
    """Test any exchange connectivity via ccxt. Returns {ok, balance, message}."""
    import ccxt.async_support as ccxt

    cls_name = _CCXT_CLASS_MAP.get(exchange)
    if not cls_name or not hasattr(ccxt, cls_name):
        return {"ok": False, "balance": 0, "message": f"Unsupported: {exchange}"}

    cls = getattr(ccxt, cls_name)
    config = {
        "apiKey": api_key,
        "secret": api_secret,
        "enableRateLimit": True,
        "options": {"defaultType": "swap"},
    }
    if passphrase:
        config["password"] = passphrase
    if testnet:
        config["sandbox"] = True

    ex_inst = cls(config)
    try:
        balance = await ex_inst.fetch_balance()
        usdt = balance.get("USDT", {})
        total = float(usdt.get("total", 0) or 0)
        free = float(usdt.get("free", 0) or 0)
        bal = total if total > 0 else free
        return {"ok": True, "balance": round(bal, 2), "message": "Connected"}
    except Exception as exc:
        msg = str(exc)
        # Extract the useful part from ccxt error messages
        if hasattr(exc, "args") and exc.args:
            msg = str(exc.args[0])[:200]
        return {"ok": False, "balance": 0, "message": msg}
    finally:
        await ex_inst.close()


def apply_active_exchange() -> dict | None:
    """Inject the active exchange's credentials into the trading modules.

    Called at bot startup (main.py) to wire exchange manager configs into
    the exchange router (ccxt for non-Binance, binance_rest for Binance).
    Returns the active config or None if no exchange is configured.
    """
    from data.exchange_router import configure

    ex = get_active_exchange()
    if not ex:
        log.info("No active exchange in exchange manager — using env vars")
        return None

    exchange = ex["exchange"]
    testnet = ex.get("testnet", False)

    configure(
        exchange=exchange,
        api_key=ex["api_key"],
        api_secret=ex["api_secret"],
        passphrase=ex.get("passphrase", ""),
        testnet=testnet,
    )
    log.info("Applied active exchange: %s (%s%s)",
             ex["name"], exchange,
             " TESTNET" if testnet else "")
    return ex


async def test_exchange(ex_id: str) -> dict:
    """Test connectivity for a stored exchange config by ID (via ccxt)."""
    exchanges = load_exchanges()
    ex = next((e for e in exchanges if e["id"] == ex_id), None)
    if not ex:
        return {"ok": False, "balance": 0, "message": "Exchange config not found"}

    return await _test_via_ccxt(
        exchange=ex["exchange"],
        api_key=ex["api_key"],
        api_secret=ex["api_secret"],
        passphrase=ex.get("passphrase", ""),
        testnet=ex.get("testnet", False),
    )
