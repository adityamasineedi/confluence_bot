"""Symbol × strategy scanner — tests every strategy against every symbol.

Fetches data once per symbol, then runs all strategies against it.
Prints a full result matrix and best-per-symbol summary at the end.
Results are saved to backtest/scan_results.json.

Usage:
    python -m backtest.symbol_strategy_scan
    python -m backtest.symbol_strategy_scan --from 2025-01-01 --to 2025-12-31
    python -m backtest.symbol_strategy_scan --capital 1000 --risk-pct 0.01
"""
import json
import logging
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("symbol_scan")

SYMBOLS = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "AVAXUSDT",
    "ADAUSDT", "DOTUSDT", "DOGEUSDT", "SUIUSDT",
]

STRATEGIES = [
    "microrange", "ema_pullback", "leadlag", "sweep",
    "zone", "fvg", "bos", "vwap_band",
]

FROM_DATE = "2025-06-01"
TO_DATE   = "2025-12-31"
CAPITAL   = 1000.0
RISK_PCT  = 0.01


# ── Helpers ───────────────────────────────────────────────────────────────────

def _date_to_ms(date_str: str) -> int:
    return int(datetime.strptime(date_str, "%Y-%m-%d")
               .replace(tzinfo=timezone.utc).timestamp() * 1000)


def _run_engine(
    strategy: str,
    sym:      str,
    ohlcv:    dict,
    capital:  float,
    risk_pct: float,
) -> list[dict]:
    """Run a single strategy engine for one symbol. Returns closed trade list."""
    if strategy == "microrange":
        from backtest import microrange_engine as eng
    elif strategy == "ema_pullback":
        from backtest import ema_pullback_engine as eng
    elif strategy == "leadlag":
        from backtest import leadlag_engine as eng
    elif strategy == "sweep":
        from backtest import sweep_engine as eng
    elif strategy == "zone":
        from backtest import zone_engine as eng
    elif strategy == "fvg":
        from backtest import fvg_engine as eng
    elif strategy == "bos":
        from backtest import bos_engine as eng
    elif strategy == "vwap_band":
        from backtest import vwap_band_engine as eng
    else:
        raise ValueError(f"Unknown strategy: {strategy}")

    # Build per-symbol ohlcv subset
    sym_ohlcv = {k: v for k, v in ohlcv.items() if k.startswith(f"{sym}:")}

    # leadlag needs BTCUSDT as the trigger anchor
    if strategy == "leadlag" and sym != "BTCUSDT":
        sym_ohlcv.update({k: v for k, v in ohlcv.items() if k.startswith("BTCUSDT:")})
        symbols = ["BTCUSDT", sym]
    else:
        symbols = [sym]

    return eng.run(symbols=symbols, ohlcv=sym_ohlcv,
                   starting_capital=capital, risk_pct=risk_pct)


def _compute_result(trades: list[dict], capital: float) -> dict:
    """Compute summary stats. Returns a result dict with a 'status' field."""
    n = len(trades)
    if n < 10:
        return {"status": "insufficient", "trade_count": n}

    wins      = [t for t in trades if t["outcome"] == "WIN"]
    total_pnl = sum(t["pnl"] for t in trades)
    gross_win  = sum(t["pnl"] for t in trades if t["pnl"] > 0)
    gross_loss = abs(sum(t["pnl"] for t in trades if t["pnl"] < 0))

    pf       = (gross_win / gross_loss) if gross_loss > 0 else float("inf")
    wr       = len(wins) / n
    ret_pct  = total_pnl / capital * 100

    return {
        "status":        "ok",
        "return_pct":    round(ret_pct, 1),
        "win_rate":      round(wr, 3),
        "profit_factor": round(min(pf, 99.9), 3),
        "trade_count":   n,
    }


def _emoji(r: dict) -> str:
    if r["status"] != "ok":
        return "—"
    if r["return_pct"] <= 0:
        return "❌"
    if r["profit_factor"] >= 1.15 and r["win_rate"] >= 0.30:
        return "✅"
    return "⚠️"


def _cell(r: dict) -> str:
    """Short cell text for Table 1."""
    if r["status"] != "ok":
        return "—"
    emoji = _emoji(r)
    return f"{r['return_pct']:+.0f}% {emoji}"


# ── Table printers ─────────────────────────────────────────────────────────────

def _print_matrix(results: dict) -> None:
    col_w  = 15
    sym_w  = 12
    header = f"{'SYMBOL':<{sym_w}}" + "".join(f"{s:<{col_w}}" for s in STRATEGIES)
    print("\n" + "=" * len(header))
    print("TABLE 1 — Symbol × Strategy Matrix")
    print("=" * len(header))
    print(header)
    print("-" * len(header))
    for sym in SYMBOLS:
        row = f"{sym:<{sym_w}}"
        for strategy in STRATEGIES:
            row += f"{_cell(results[sym][strategy]):<{col_w}}"
        print(row)


def _print_best_per_symbol(results: dict) -> None:
    from core.symbol_config import get_symbol_tier

    print("\n" + "=" * 80)
    print("TABLE 2 — Best Strategy Per Symbol")
    print("=" * 80)
    print(f"{'SYMBOL':<12}{'best_strategy':<16}{'return':<10}{'WR':<8}{'PF':<8}{'trades':<10}{'tier'}")
    print("-" * 75)

    for sym in SYMBOLS:
        tier = get_symbol_tier(sym)
        best_strat: str | None = None
        best_ret   = float("-inf")
        best_r: dict | None = None

        for strategy in STRATEGIES:
            r = results[sym][strategy]
            if r["status"] != "ok":
                continue
            if r["return_pct"] > best_ret:
                best_ret   = r["return_pct"]
                best_strat = strategy
                best_r     = r

        if best_strat is None:
            print(f"{sym:<12}{'—':<16}{'—':<10}{'—':<8}{'—':<8}{'—':<10}{tier}")
        else:
            emoji = _emoji(best_r)
            ret_s = f"{best_r['return_pct']:+.1f}%"
            wr_s  = f"{best_r['win_rate']:.1%}"
            pf_s  = f"{best_r['profit_factor']:.2f}"
            print(f"{sym:<12}{best_strat:<16}{ret_s:<10}{wr_s:<8}{pf_s:<8}"
                  f"{best_r['trade_count']:<10}{tier}  {emoji}")


def _print_volatility_profile(ohlcv: dict, from_date: str, to_date: str) -> None:
    from core.symbol_config import _calc_atr, get_symbol_tier, get_symbol_config, _microrange_dynamic

    from_ms = _date_to_ms(from_date)
    to_ms   = _date_to_ms(to_date) + 86_400_000

    print(f"\n{'=' * 80}")
    print(f"VOLATILITY PROFILE (avg ATR% on 5m bars · {from_date} → {to_date})")
    print(f"{'=' * 80}")
    print(f"{'SYMBOL':<12}{'avg ATR%':<12}{'tier':<10}{'box used (avg)':<18}{'stop used (avg)'}")
    print("-" * 65)

    for sym in SYMBOLS:
        bars = ohlcv.get(f"{sym}:5m", [])
        period_bars = [b for b in bars if from_ms <= b["ts"] <= to_ms]
        if len(period_bars) < 20:
            print(f"{sym:<12}{'—':<12}{'—':<10}{'—':<18}{'—'}")
            continue

        tier = get_symbol_tier(sym)
        base = get_symbol_config(sym, "microrange")

        # Sliding-window ATR across every bar in the period
        atrs = []
        for i in range(14, len(period_bars)):
            window = period_bars[max(0, i - 14) : i + 1]
            atr   = _calc_atr(window, period=14)
            price = window[-1]["c"]
            if atr > 0 and price > 0:
                atrs.append(atr / price * 100)

        if not atrs:
            print(f"{sym:<12}{'—':<12}{'—':<10}{'—':<18}{'—'}")
            continue

        avg_atr_pct = sum(atrs) / len(atrs)
        dynamic     = _microrange_dynamic(base, tier, avg_atr_pct / 100)
        box_pct     = dynamic.get("range_max_pct", 0) * 100
        stop_pct    = dynamic.get("stop_pct", 0) * 100

        print(f"{sym:<12}{avg_atr_pct:.3f}%{'':<5}{tier:<10}{box_pct:.3f}%{'':<12}{stop_pct:.3f}%")


def _print_excludes(results: dict) -> None:
    print("\n" + "=" * 80)
    print("RECOMMENDED exclude_symbols per strategy:")
    print("=" * 80)

    for strategy in STRATEGIES:
        excluded = []
        for sym in SYMBOLS:
            r = results[sym][strategy]
            # Exclude: no valid result OR return ≤ 0
            if r["status"] != "ok" or r["return_pct"] <= 0:
                excluded.append(sym)
        if excluded:
            print(f"  {strategy:<16}: exclude {excluded}")
        else:
            print(f"  {strategy:<16}: no exclusions")


# ── Main ──────────────────────────────────────────────────────────────────────

def main(
    from_date: str   = FROM_DATE,
    to_date:   str   = TO_DATE,
    capital:   float = CAPITAL,
    risk_pct:  float = RISK_PCT,
) -> None:
    from backtest.fetcher import fetch_period_sync

    from_ms = _date_to_ms(from_date)
    to_ms   = _date_to_ms(to_date) + 86_400_000   # include end date

    # Ensure BTCUSDT is fetched (needed as leadlag anchor even when testing alts)
    fetch_symbols = list(dict.fromkeys(["BTCUSDT"] + SYMBOLS))

    print(f"\nFetching data for {len(fetch_symbols)} symbols "
          f"({from_date} → {to_date}, warmup=45d)...")
    data  = fetch_period_sync(fetch_symbols, from_ms, to_ms, warmup_days=45)
    ohlcv = data["ohlcv"]
    print(f"Fetch complete. Running {len(SYMBOLS)} × {len(STRATEGIES)} = "
          f"{len(SYMBOLS) * len(STRATEGIES)} combinations...\n")

    total   = len(SYMBOLS) * len(STRATEGIES)
    count   = 0
    results: dict[str, dict[str, dict]] = {sym: {} for sym in SYMBOLS}

    for sym in SYMBOLS:
        for strategy in STRATEGIES:
            count += 1
            print(f"  [{count:2d}/{total}] {sym} × {strategy}...", end=" ", flush=True)
            try:
                trades = _run_engine(strategy, sym, ohlcv, capital, risk_pct)
                r      = _compute_result(trades, capital)
                results[sym][strategy] = r

                if r["status"] == "insufficient":
                    print(f"— ({r['trade_count']} trades)")
                else:
                    emoji = _emoji(r)
                    print(f"{emoji}  {r['return_pct']:+.1f}%  "
                          f"WR={r['win_rate']:.1%}  "
                          f"PF={r['profit_factor']:.2f}  "
                          f"trades={r['trade_count']}")

            except Exception as exc:
                log.warning("Error %s × %s: %s", sym, strategy, exc, exc_info=True)
                results[sym][strategy] = {
                    "status":      "error",
                    "trade_count": 0,
                    "error":       str(exc),
                }
                print(f"ERROR: {exc}")

    # ── Print tables ──────────────────────────────────────────────────────────
    _print_matrix(results)
    _print_best_per_symbol(results)
    _print_volatility_profile(ohlcv, from_date, to_date)
    _print_excludes(results)

    # ── Save JSON ─────────────────────────────────────────────────────────────
    out_path = os.path.join(os.path.dirname(__file__), "scan_results.json")
    save_data = {
        "from_date":  from_date,
        "to_date":    to_date,
        "capital":    capital,
        "risk_pct":   risk_pct,
        "generated":  datetime.utcnow().isoformat() + "Z",
        "symbols":    SYMBOLS,
        "strategies": STRATEGIES,
        "results":    results,
    }
    with open(out_path, "w") as f:
        json.dump(save_data, f, indent=2)
    print(f"\nResults saved to {out_path}")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Symbol × strategy backtest scanner")
    parser.add_argument("--from",     dest="from_date", default=FROM_DATE,
                        help=f"Start date YYYY-MM-DD (default: {FROM_DATE})")
    parser.add_argument("--to",       dest="to_date",   default=TO_DATE,
                        help=f"End date YYYY-MM-DD (default: {TO_DATE})")
    parser.add_argument("--capital",  type=float, default=CAPITAL,
                        help=f"Starting capital USD (default: {CAPITAL})")
    parser.add_argument("--risk-pct", type=float, dest="risk_pct", default=RISK_PCT,
                        help=f"Risk per trade fraction (default: {RISK_PCT})")
    args = parser.parse_args()

    main(
        from_date=args.from_date,
        to_date=args.to_date,
        capital=args.capital,
        risk_pct=args.risk_pct,
    )
