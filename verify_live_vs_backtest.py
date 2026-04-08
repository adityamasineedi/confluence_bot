"""Compare live paper trades with backtest for the same period.

Run after 2+ days of PAPER_MODE=1:
    python verify_live_vs_backtest.py

Shows side-by-side: did the live bot fire the same trades as backtest?
"""
import sqlite3
import sys
import os
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(__file__))


def main():
    conn = sqlite3.connect("confluence_bot.db")
    conn.row_factory = sqlite3.Row

    # Get live trades
    live_trades = conn.execute("""
        SELECT ts, symbol, direction, regime, entry, stop_loss, take_profit,
               size, pnl_usdt, status
        FROM trades
        WHERE status IN ('CLOSED', 'FILLED', 'OPEN')
        ORDER BY ts
    """).fetchall()

    if not live_trades:
        print("No live trades found. Run the bot with PAPER_MODE=1 for a few days first.")
        return

    # Determine date range from live trades
    first_ts = live_trades[0]["ts"]
    last_ts  = live_trades[-1]["ts"]
    # Parse ISO format
    from_dt = datetime.fromisoformat(first_ts.replace("Z", "+00:00"))
    to_dt   = datetime.fromisoformat(last_ts.replace("Z", "+00:00"))

    from_ms = int(from_dt.timestamp() * 1000)
    to_ms   = int(to_dt.timestamp() * 1000) + 86_400_000  # +1 day

    from_str = from_dt.strftime("%Y-%m-%d")
    to_str   = to_dt.strftime("%Y-%m-%d")

    print(f"Live trades: {len(live_trades)} ({from_str} to {to_str})")
    print()

    # Run backtest for the same period — use compressed cache (data_store)
    from backtest.engine import run_strategy, RUNNERS
    from backtest.data_store import load_bars
    import numpy as np
    import yaml

    with open("config.yaml") as f:
        cfg = yaml.safe_load(f)
    routing = cfg.get("strategy_routing", {})
    symbols = cfg.get("symbols", [])

    # Build numpy data from compressed cache
    warmup_ms = 45 * 86_400_000
    fetch_from = from_ms - warmup_ms

    def _load_sym(sym):
        """Load all timeframes for a symbol from compressed cache as numpy arrays."""
        data = {}
        for tf in ["5m", "15m", "1h", "4h", "1d", "1w"]:
            bars = load_bars(sym, tf, fetch_from, to_ms)
            if bars:
                arr = np.array([[b["o"], b["h"], b["l"], b["c"], b["v"], b["ts"]]
                                for b in bars], dtype=np.float64)
                data[f"{sym}:{tf}"] = arr
        return data if data else None

    btc_np = _load_sym("BTCUSDT")
    btc_data = {k: btc_np[k] for k in btc_np if k.startswith("BTCUSDT:")} if btc_np else None

    bt_trades = []
    for sym in symbols:
        sym_np = _load_sym(sym)
        if not sym_np:
            continue
        sym_rt = routing.get(sym, routing.get("_default", {}))
        all_strats = set()
        for regime, sl in sym_rt.items():
            if isinstance(sl, list):
                for s in sl:
                    all_strats.add(s)
        sym_data = {k: sym_np[k] for k in sym_np if k.startswith(f"{sym}:")}
        for strat in all_strats:
            if strat not in RUNNERS:
                continue
            trades = run_strategy(sym, strat, sym_data, btc_data, from_ms, to_ms)
            for t in trades:
                b5m = sym_data.get(f"{sym}:5m")
                if b5m is not None and t.bar_idx < len(b5m):
                    t.exit_ts = int(b5m[t.bar_idx, 5])
                bt_trades.append(t)

    bt_trades.sort(key=lambda t: getattr(t, "exit_ts", 0))
    print(f"Backtest trades: {len(bt_trades)} (same period)")
    print()

    # Match: for each backtest trade, find the closest live trade
    # (same symbol, same direction, within 2 hours)
    matched    = 0
    bt_only    = 0
    live_only  = 0

    print(f"{'Date':<18} {'Symbol':<10} {'Dir':<6} {'BT Entry':>10} {'Live Entry':>10} {'Match':>8}")
    print("-" * 70)

    bt_used = set()
    live_used = set()

    for bt in bt_trades:
        bt_ts = getattr(bt, "exit_ts", 0)
        bt_sym = bt.symbol
        bt_dir = bt.direction
        bt_entry = bt.entry

        best_match = None
        best_diff  = 999999999

        for i, lt in enumerate(live_trades):
            if i in live_used:
                continue
            if lt["symbol"] != bt_sym or lt["direction"] != bt_dir:
                continue
            lt_ts = int(datetime.fromisoformat(
                lt["ts"].replace("Z", "+00:00")).timestamp() * 1000)
            time_diff = abs(bt_ts - lt_ts)
            if time_diff < 7200_000 and time_diff < best_diff:  # within 2 hours
                best_match = i
                best_diff = time_diff

        bt_dt = datetime.fromtimestamp(bt_ts / 1000, tz=timezone.utc).strftime(
            "%Y-%m-%d %H:%M") if bt_ts else "?"

        if best_match is not None:
            lt = live_trades[best_match]
            live_used.add(best_match)
            bt_used.add(id(bt))
            price_diff = abs(bt_entry - lt["entry"]) / bt_entry * 100
            match_str = f"{price_diff:.2f}%" if price_diff < 1 else f"DIFF {price_diff:.1f}%"
            print(f"{bt_dt:<18} {bt_sym:<10} {bt_dir:<6} {bt_entry:>10.4f} {lt['entry']:>10.4f} {match_str:>8}")
            matched += 1
        else:
            print(f"{bt_dt:<18} {bt_sym:<10} {bt_dir:<6} {bt_entry:>10.4f} {'—':>10} {'BT ONLY':>8}")
            bt_only += 1

    # Live trades not in backtest
    for i, lt in enumerate(live_trades):
        if i not in live_used:
            live_only += 1
            print(f"{'':18} {lt['symbol']:<10} {lt['direction']:<6} {'—':>10} {lt['entry']:>10.4f} {'LIVE ONLY':>8}")

    print()
    print("=" * 70)
    print(f"Matched:        {matched} trades (backtest + live agree)")
    print(f"Backtest only:  {bt_only} trades (backtest fired, live didn't)")
    print(f"Live only:      {live_only} trades (live fired, backtest didn't)")
    total = matched + bt_only + live_only
    if total:
        print(f"Match rate:     {matched / total * 100:.1f}%")
    print()

    if bt_only > matched:
        print("WARNING: More backtest-only trades than matches.")
        print("Possible causes:")
        print("  - Bot was restarting during those times")
        print("  - Circuit breaker was tripped")
        print("  - Cache warmup not complete")
    elif matched > 0:
        print("Strategy is firing consistently between backtest and live.")


if __name__ == "__main__":
    main()
