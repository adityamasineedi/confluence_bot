"""Quick test: place a minimal LONG on Binance demo and immediately close it."""
import urllib.request, urllib.parse, json, os, hmac, hashlib, time, sys

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
except Exception:
    pass

BASE   = os.environ.get("BINANCE_BASE_URL", "https://demo-fapi.binance.com")
KEY    = os.environ.get("BINANCE_API_KEY", "")
SECRET = os.environ.get("BINANCE_SECRET", "").encode()
SYMBOL = "BTCUSDT"
QTY    = "0.002"   # ~$141 notional at $70k — Binance demo requires ≥$100


def req(method: str, path: str, params: dict | None = None) -> dict:
    params = params or {}
    params["timestamp"] = int(time.time() * 1000)
    qs  = urllib.parse.urlencode(params)
    sig = hmac.new(SECRET, qs.encode(), hashlib.sha256).hexdigest()
    url = f"{BASE}{path}?{qs}&signature={sig}"
    data = b"" if method == "POST" else None
    r = urllib.request.Request(
        url, headers={"X-MBX-APIKEY": KEY}, method=method, data=data
    )
    try:
        with urllib.request.urlopen(r, timeout=10) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return json.loads(e.read())


def main() -> None:
    # Current price
    raw   = urllib.request.urlopen(f"{BASE}/fapi/v1/ticker/price?symbol={SYMBOL}", timeout=5)
    price = float(json.loads(raw.read())["price"])
    print(f"BTC price : ${price:,.2f}")
    print(f"Test order: BUY {QTY} {SYMBOL}  (~${price * float(QTY):.2f})")
    print()

    # Set leverage
    lev = req("POST", "/fapi/v1/leverage", {"symbol": SYMBOL, "leverage": 5})
    print(f"Leverage  : {lev.get('leverage', lev)}x")

    # Open LONG
    print(f"\n[1/3] Opening LONG...")
    order = req("POST", "/fapi/v1/order", {
        "symbol":   SYMBOL,
        "side":     "BUY",
        "type":     "MARKET",
        "quantity": QTY,
    })
    if "code" in order:
        print(f"ERROR: {order}")
        sys.exit(1)

    order_id = order["orderId"]
    avg_price = float(order.get("avgPrice") or order.get("price") or price)
    print(f"  orderId  : {order_id}")
    print(f"  status   : {order['status']}")
    print(f"  avgPrice : ${avg_price:,.2f}")
    print(f"  qty      : {order['executedQty']}")

    # Wait
    print(f"\n[2/3] Holding 3 seconds...")
    time.sleep(3)

    # Check unrealised PnL
    pos_list = req("GET", "/fapi/v2/positionRisk", {"symbol": SYMBOL})
    for p in pos_list:
        amt = float(p.get("positionAmt", 0))
        if abs(amt) > 0:
            pnl = float(p.get("unRealizedProfit", 0))
            print(f"  position : {amt} {SYMBOL}  entry={p['entryPrice']}  uPnL={pnl:+.4f} USDT")

    # Close LONG
    print(f"\n[3/3] Closing position (SELL reduceOnly)...")
    close = req("POST", "/fapi/v1/order", {
        "symbol":     SYMBOL,
        "side":       "SELL",
        "type":       "MARKET",
        "quantity":   QTY,
        "reduceOnly": "true",
    })
    if "code" in close:
        print(f"ERROR: {close}")
        sys.exit(1)

    close_price = float(close.get("avgPrice") or close.get("price") or price)
    realised    = (close_price - avg_price) * float(QTY)
    print(f"  orderId  : {close['orderId']}")
    print(f"  status   : {close['status']}")
    print(f"  avgPrice : ${close_price:,.2f}")
    print(f"  realised : {realised:+.4f} USDT")
    print()
    print("Test complete — Binance demo connection verified.")


if __name__ == "__main__":
    main()
