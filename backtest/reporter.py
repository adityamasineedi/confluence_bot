"""Backtest reporter — computes stats and prints formatted results + monthly P&L."""
import math
from datetime import datetime, timezone


# ── Stats helpers ─────────────────────────────────────────────────────────────

def _bucket_stats(trades: list[dict]) -> dict:
    if not trades:
        return {"trades": 0, "wins": 0, "losses": 0, "timeouts": 0,
                "win_rate": 0.0, "pnl": 0.0, "avg_win": 0.0, "avg_loss": 0.0}

    wins     = [t for t in trades if t["outcome"] == "WIN"]
    losses   = [t for t in trades if t["outcome"] == "LOSS"]
    timeouts = [t for t in trades if t["outcome"] == "TIMEOUT"]
    total    = len(trades)

    avg_win  = sum(t["pnl"] for t in wins)   / len(wins)   if wins   else 0.0
    avg_loss = sum(t["pnl"] for t in losses) / len(losses) if losses else 0.0

    return {
        "trades":   total,
        "wins":     len(wins),
        "losses":   len(losses),
        "timeouts": len(timeouts),
        "win_rate": len(wins) / total if total else 0.0,
        "pnl":      round(sum(t["pnl"] for t in trades), 2),
        "avg_win":  round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
    }


def _sharpe_from_dicts(trades: list[dict], starting_capital: float) -> float:
    """Annualised Sharpe ratio from trade dicts (pnl field is dollar P&L)."""
    if len(trades) < 10:
        return 0.0
    sorted_t = sorted(trades, key=lambda t: t.get("exit_ts", 0))
    returns = []
    equity = starting_capital
    for t in sorted_t:
        pnl = t.get("pnl", 0)
        ret = pnl / equity if equity > 0 else 0
        returns.append(ret)
        equity = max(equity + pnl, 1)
    avg = sum(returns) / len(returns)
    variance = sum((r - avg) ** 2 for r in returns) / len(returns)
    std = math.sqrt(variance) if variance > 0 else 0
    if std == 0:
        return 0.0
    trades_per_year = 1825
    return round((avg / std) * math.sqrt(trades_per_year), 2)


def _max_drawdown(trades: list[dict], starting_capital: float) -> tuple[float, float]:
    """Return (max_drawdown_$, max_drawdown_%) from peak equity."""
    sorted_t = sorted(trades, key=lambda t: t.get("exit_ts", 0))
    equity   = starting_capital
    peak     = starting_capital
    max_dd   = max_dd_pct = 0.0

    for t in sorted_t:
        equity += t["pnl"]
        if equity > peak:
            peak = equity
        dd     = peak - equity
        dd_pct = dd / peak if peak > 0 else 0.0
        if dd > max_dd:
            max_dd     = dd
            max_dd_pct = dd_pct

    return round(max_dd, 2), round(max_dd_pct * 100, 1)


def _longest_streak(trades: list[dict], outcome: str) -> int:
    sorted_t = sorted(trades, key=lambda t: t.get("exit_ts", 0))
    best = cur = 0
    for t in sorted_t:
        if t["outcome"] == outcome:
            cur  += 1
            best  = max(best, cur)
        else:
            cur = 0
    return best


def _profit_factor(trades: list[dict]) -> str:
    gross_win  = sum(t["pnl"] for t in trades if t["pnl"] > 0)
    gross_loss = abs(sum(t["pnl"] for t in trades if t["pnl"] < 0))
    if gross_loss == 0:
        return "inf" if gross_win > 0 else "0.00"
    return f"{gross_win / gross_loss:.2f}"


# ── Monthly returns ───────────────────────────────────────────────────────────

def compute_monthly_returns(
    trades:           list[dict],
    starting_capital: float,
) -> list[dict]:
    """
    Return a list of monthly summary dicts, sorted chronologically.
    Each dict: month, start_eq, end_eq, pnl, pct, trades, wins, losses, timeouts.
    """
    sorted_t = sorted(trades, key=lambda t: t.get("exit_ts", 0))

    monthly: dict[str, dict] = {}
    equity = starting_capital

    for t in sorted_t:
        ts = t.get("exit_ts", 0)
        if not ts:
            continue
        month = datetime.fromtimestamp(ts / 1000, tz=timezone.utc).strftime("%Y-%m")

        if month not in monthly:
            monthly[month] = {
                "month":    month,
                "start_eq": equity,
                "pnl":      0.0,
                "trades":   0,
                "wins":     0,
                "losses":   0,
                "timeouts": 0,
            }

        monthly[month]["pnl"]     += t["pnl"]
        monthly[month]["trades"]  += 1
        if t["outcome"] == "WIN":
            monthly[month]["wins"] += 1
        elif t["outcome"] == "LOSS":
            monthly[month]["losses"] += 1
        else:
            monthly[month]["timeouts"] += 1

        equity += t["pnl"]

    # Fill end_eq and pct for each month
    rows = []
    running = starting_capital
    for month in sorted(monthly):
        m           = monthly[month]
        m["end_eq"] = round(running + m["pnl"], 2)
        m["pct"]    = (m["pnl"] / running * 100) if running > 0 else 0.0
        running     = m["end_eq"]
        rows.append(m)

    return rows


def compute_signal_stats(trades: list[dict]) -> list[dict]:
    """Return per-signal predictiveness: win rate when signal was True vs False."""
    signal_names: set[str] = set()
    for t in trades:
        signal_names.update(t.get("signals", {}).keys())

    rows = []
    for sig in sorted(signal_names):
        with_true  = [t for t in trades if t.get("signals", {}).get(sig)]
        with_false = [t for t in trades if not t.get("signals", {}).get(sig)]

        def wr(group):
            if not group:
                return None
            return sum(1 for t in group if t["outcome"] == "WIN") / len(group)

        rows.append({
            "signal":       sig,
            "n_true":       len(with_true),
            "n_false":      len(with_false),
            "wr_true":      wr(with_true),
            "wr_false":     wr(with_false),
            "edge":         (wr(with_true) or 0) - (wr(with_false) or 0),
        })

    rows.sort(key=lambda r: r["edge"], reverse=True)
    return rows


def compute_stats(trades: list[dict], starting_capital: float = 1_000.0) -> dict:
    if not trades:
        return {"total": _bucket_stats([]), "by_regime": {}, "by_symbol": {},
                "monthly": [], "starting_capital": starting_capital}

    by_regime: dict[str, list] = {}
    by_symbol: dict[str, list] = {}
    for t in trades:
        by_regime.setdefault(t["regime"], []).append(t)
        by_symbol.setdefault(t["symbol"], []).append(t)

    base = _bucket_stats(trades)
    base["max_drawdown_usd"], base["max_drawdown_pct"] = _max_drawdown(trades, starting_capital)
    base["longest_win_streak"]  = _longest_streak(trades, "WIN")
    base["longest_loss_streak"] = _longest_streak(trades, "LOSS")
    base["final_equity"]        = round(starting_capital + base["pnl"], 2)
    base["total_return_pct"]    = round(base["pnl"] / starting_capital * 100, 1)
    base["sharpe"]              = _sharpe_from_dicts(trades, starting_capital)

    return {
        "total":             base,
        "by_regime":         {k: _bucket_stats(v) for k, v in by_regime.items()},
        "by_symbol":         {k: _bucket_stats(v) for k, v in by_symbol.items()},
        "monthly":           compute_monthly_returns(trades, starting_capital),
        "signal_stats":      compute_signal_stats(trades),
        "starting_capital":  starting_capital,
    }


# ── Formatting ────────────────────────────────────────────────────────────────

_W = 68

def _line(char: str = "-") -> str:
    return char * _W

def _row(label: str, value) -> str:
    label = str(label)
    value = str(value)
    pad   = _W - len(label) - len(value) - 2
    return f"  {label}{'.' * max(pad, 1)}{value}"

def _pnl_str(pnl: float) -> str:
    sign = "+" if pnl >= 0 else ""
    return f"{sign}${pnl:,.2f}"

def _pct(v: float) -> str:
    return f"{v * 100:.1f}%"

def _ts_date(ts_ms: int) -> str:
    if not ts_ms:
        return "-"
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


def print_report(
    stats:            dict,
    trades:           list[dict] | None = None,
    starting_capital: float = 1_000.0,
) -> None:
    t  = stats["total"]
    sc = stats.get("starting_capital", starting_capital)

    print()
    print(_line("="))
    print("  CONFLUENCE BOT -- BACKTEST RESULTS")
    print(_line("="))

    # ── Overall ───────────────────────────────────────────────────────────────
    print()
    print("  OVERALL SUMMARY")
    print(_line())
    print(_row("Starting capital",      f"${sc:,.2f}"))
    print(_row("Final equity",          f"${t['final_equity']:,.2f}"))
    print(_row("Total return",          f"{t['total_return_pct']:+.1f}%"))
    print(_row("Total trades",          t["trades"]))
    print(_row("Wins / Losses / T-O",   f"{t['wins']} / {t['losses']} / {t['timeouts']}"))
    print(_row("Win rate",              _pct(t["win_rate"])))
    print(_row("Profit factor",         _profit_factor(trades or [])))
    print(_row("Avg win",               _pnl_str(t["avg_win"])))
    print(_row("Avg loss",              _pnl_str(t["avg_loss"])))
    print(_row("Max drawdown",          f"${t['max_drawdown_usd']:,.2f}  ({t['max_drawdown_pct']:.1f}%)"))
    print(_row("Longest win streak",    t["longest_win_streak"]))
    print(_row("Longest loss streak",   t["longest_loss_streak"]))

    # ── By regime ─────────────────────────────────────────────────────────────
    if stats["by_regime"]:
        print()
        print("  BY REGIME")
        print(_line())
        _print_bucket_table(stats["by_regime"])

    # ── By symbol ─────────────────────────────────────────────────────────────
    if stats["by_symbol"]:
        print()
        print("  BY SYMBOL")
        print(_line())
        _print_bucket_table(stats["by_symbol"])

    # ── Monthly returns ───────────────────────────────────────────────────────
    monthly = stats.get("monthly", [])
    if monthly:
        print()
        print("  MONTHLY RETURNS")
        print(_line())
        print(f"  {'Month':<9}  {'Trades':>6}  {'W/L/T':>7}  {'WR':>6}  "
              f"{'PnL':>10}  {'Equity':>10}  {'Return%':>8}")
        print("  " + "-" * 62)
        for m in monthly:
            wlt     = f"{m['wins']}/{m['losses']}/{m['timeouts']}"
            wr      = f"{m['wins']/m['trades']*100:.0f}%" if m["trades"] else "  -"
            pnl_s   = f"{'+' if m['pnl'] >= 0 else ''}${m['pnl']:,.2f}"
            eq_s    = f"${m['end_eq']:,.2f}"
            ret_s   = f"{m['pct']:+.1f}%"
            print(f"  {m['month']:<9}  {m['trades']:>6}  {wlt:>7}  {wr:>6}  "
                  f"{pnl_s:>10}  {eq_s:>10}  {ret_s:>8}")

        # Annual summaries
        _print_annual_summary(monthly)

    # ── Signal validation ─────────────────────────────────────────────────────
    sig_stats = stats.get("signal_stats", [])
    if sig_stats:
        print()
        print("  SIGNAL VALIDATION  (win rate when signal True vs False)")
        print(_line())
        print(f"  {'Signal':<22} {'N(T)':>6} {'WR(T)':>7} {'N(F)':>6} {'WR(F)':>7} {'Edge':>7}")
        print("  " + "-" * 58)
        for s in sig_stats:
            wt = f"{s['wr_true']*100:.0f}%"  if s["wr_true"]  is not None else "  -"
            wf = f"{s['wr_false']*100:.0f}%" if s["wr_false"] is not None else "  -"
            edge = f"{s['edge']*100:+.0f}%"
            print(f"  {s['signal']:<22} {s['n_true']:>6} {wt:>7} {s['n_false']:>6} {wf:>7} {edge:>7}")

    # ── Last 15 trades ────────────────────────────────────────────────────────
    if trades:
        print()
        print("  LAST 15 TRADES")
        print(_line())
        recent = sorted(trades, key=lambda x: x.get("exit_ts", 0))[-15:]
        print(f"  {'Date':<12} {'Sym':<10} {'Dir':<6} {'Regime':<7} "
              f"{'Score':>6}  {'Risk$':>7}  {'Outcome':<8} {'PnL':>8}")
        print("  " + "-" * 64)
        for tr in recent:
            date  = _ts_date(tr.get("exit_ts", 0))
            sym   = tr["symbol"][:9]
            dr    = tr["direction"][:5]
            reg   = tr["regime"][:6]
            score = f"{tr.get('score', 0):.2f}"
            risk  = f"${tr.get('risk_amount', 0):.1f}"
            out   = tr["outcome"]
            pnl   = f"{'+' if tr['pnl'] >= 0 else ''}${tr['pnl']:,.2f}"
            print(f"  {date:<12} {sym:<10} {dr:<6} {reg:<7} "
                  f"{score:>6}  {risk:>7}  {out:<8} {pnl:>8}")

    print()
    print(_line("="))
    print()


def _print_bucket_table(buckets: dict) -> None:
    print(f"  {'':14} {'Trades':>7} {'W/L/T':>10} {'WinRate':>8} {'PnL':>12}")
    print("  " + "-" * 56)
    for name, b in sorted(buckets.items()):
        wlt = f"{b['wins']}/{b['losses']}/{b['timeouts']}"
        print(f"  {name:<14} {b['trades']:>7} {wlt:>10} "
              f"{_pct(b['win_rate']):>8} {_pnl_str(b['pnl']):>12}")


def _print_annual_summary(monthly: list[dict]) -> None:
    """Print a compact year-by-year totals row under the monthly table."""
    years: dict[str, dict] = {}
    for m in monthly:
        yr = m["month"][:4]
        if yr not in years:
            years[yr] = {"pnl": 0.0, "trades": 0, "wins": 0,
                         "start_eq": m["start_eq"]}
        years[yr]["pnl"]    += m["pnl"]
        years[yr]["trades"] += m["trades"]
        years[yr]["wins"]   += m["wins"]
        years[yr]["end_eq"]  = m["end_eq"]

    print()
    print("  " + "-" * 62)
    print(f"  {'Year':<9}  {'Trades':>6}  {'Wins':>7}  {'WR':>6}  "
          f"{'PnL':>10}  {'Equity':>10}  {'Return%':>8}")
    print("  " + "-" * 62)
    for yr, y in sorted(years.items()):
        wr    = f"{y['wins']/y['trades']*100:.0f}%" if y["trades"] else "  -"
        pnl_s = f"{'+' if y['pnl'] >= 0 else ''}${y['pnl']:,.2f}"
        eq_s  = f"${y['end_eq']:,.2f}"
        ret_s = f"{y['pnl']/y['start_eq']*100:+.1f}%" if y["start_eq"] > 0 else "  -"
        print(f"  {yr:<9}  {y['trades']:>6}  {y['wins']:>7}  {wr:>6}  "
              f"{pnl_s:>10}  {eq_s:>10}  {ret_s:>8}")
