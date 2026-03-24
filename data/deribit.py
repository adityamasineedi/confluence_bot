"""Deribit REST client — options skew and IV data.

All endpoints used here are public — no API key required.
Deribit only offers BTC and ETH options, so other symbols fall back to 0.

Public API base: https://www.deribit.com/api/v2/public
"""
import asyncio
import logging
import urllib.request
import urllib.parse
import json as _json

log = logging.getLogger(__name__)

_BASE_URL  = "https://www.deribit.com/api/v2/public"
_TIMEOUT_S = 10
_POLL_SECS = 300   # options data changes slowly; refresh every 5 min

# Only BTC and ETH have Deribit options
_CURRENCY_MAP: dict[str, str] = {
    "BTCUSDT": "BTC",
    "ETHUSDT": "ETH",
}


def _fetch_json(path: str, params: dict) -> dict | None:
    """Blocking GET to Deribit public API."""
    qs  = urllib.parse.urlencode(params)
    url = f"{_BASE_URL}/{path}?{qs}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "confluence_bot/1.0"})
        with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as resp:
            return _json.loads(resp.read())
    except Exception as exc:
        log.debug("Deribit fetch %s failed: %s", path, exc)
        return None


def _calc_skew(currency: str) -> float:
    """Compute 25-delta risk reversal skew (call IV - put IV) for the nearest expiry.

    Algorithm:
    1. Fetch all option summaries for the currency.
    2. Parse strike and underlying_price from each instrument.
    3. Use moneyness (K / underlying) to approximate 25-delta:
         calls: K/S in [1.05, 1.15]  (~10% OTM calls ≈ 25-delta)
         puts:  K/S in [0.85, 0.95]  (~10% OTM puts  ≈ 25-delta)
    4. Pick nearest expiry with ≥ 4 qualifying options (2 calls + 2 puts).
    5. Return avg_call_iv - avg_put_iv.  Positive = calls pricier (bullish skew).
    """
    data = _fetch_json("get_book_summary_by_currency", {
        "currency": currency,
        "kind":     "option",
    })
    if not data or "result" not in data:
        return 0.0

    instruments = data["result"]
    if not instruments:
        return 0.0

    # Parse instruments: extract expiry, strike, type, iv, underlying
    from collections import defaultdict
    by_expiry: dict[str, list] = defaultdict(list)

    for inst in instruments:
        name = inst.get("instrument_name", "")
        # Format: BTC-28MAR25-70000-C
        parts = name.split("-")
        if len(parts) < 4:
            continue
        expiry     = parts[1]
        opt_type   = parts[-1]           # "C" or "P"
        iv         = inst.get("mark_iv") or 0.0
        underlying = inst.get("underlying_price") or 0.0
        if iv <= 0 or underlying <= 0:
            continue
        try:
            strike = float(parts[2])
        except ValueError:
            continue

        moneyness = strike / underlying
        by_expiry[expiry].append({
            "type": opt_type,
            "iv":   iv,
            "moneyness": moneyness,
        })

    # Sort expiries by parsing date (DDMMMYY → sortable)
    _MONTHS = {"JAN":1,"FEB":2,"MAR":3,"APR":4,"MAY":5,"JUN":6,
               "JUL":7,"AUG":8,"SEP":9,"OCT":10,"NOV":11,"DEC":12}
    def _expiry_key(exp: str) -> int:
        try:
            day, mon, yr = int(exp[:2]), _MONTHS.get(exp[2:5], 0), int(exp[5:])
            return yr * 10000 + mon * 100 + day
        except Exception:
            return 999999

    sorted_expiries = sorted(by_expiry.keys(), key=_expiry_key)

    # Find nearest expiry with enough qualifying options
    for expiry in sorted_expiries:
        opts = by_expiry[expiry]
        # ~25-delta: calls 5-15% OTM, puts 5-15% OTM
        calls = [o["iv"] for o in opts if o["type"] == "C" and 1.04 <= o["moneyness"] <= 1.16]
        puts  = [o["iv"] for o in opts if o["type"] == "P" and 0.84 <= o["moneyness"] <= 0.96]

        if len(calls) >= 2 and len(puts) >= 2:
            call_iv = sum(calls) / len(calls)
            put_iv  = sum(puts)  / len(puts)
            return round(call_iv - put_iv, 4)

    return 0.0


async def get_skew(currency: str) -> float:
    """Return 25-delta risk reversal skew for BTC or ETH. 0.0 on failure."""
    return await asyncio.to_thread(_calc_skew, currency)


async def refresh_cache(currencies: list[str], cache) -> None:
    """Fetch one round of options skew and push to cache.

    Called every 5 min by main.py via _periodic(refresh_cache, ..., interval=300).
    Maps currency codes (["BTC", "ETH"]) to cache symbols (BTCUSDT, ETHUSDT).

    The skew is stored via cache.push_skew(symbol, value) for:
      - check_options_flow()   (TREND LONG, weight 0.05)
      - check_options_skew()   (RANGE LONG/SHORT, weight 0.10)
      - check_call_skew_roc()  (RANGE LONG, weight 0.10)
    """
    # Reverse map: BTC → BTCUSDT
    rev_map = {v: k for k, v in _CURRENCY_MAP.items()}

    for currency in currencies:
        symbol = rev_map.get(currency.upper())
        if not symbol:
            continue
        try:
            skew = await get_skew(currency)
            cache.push_skew(symbol, skew)
            log.debug("Deribit skew %s (%s): %.4f", symbol, currency, skew)
        except Exception as exc:
            log.warning("Deribit refresh(%s) error: %s", currency, exc)
