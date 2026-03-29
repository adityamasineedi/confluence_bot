"""Realistic trading cost simulation for backtest engines.

Covers per round-trip: taker fees (entry + exit), slippage (entry + exit),
and accrued funding based on how long the trade was held.

All rates are read from config.yaml backtest: section with hardcoded fallbacks.
"""
import os
import yaml

_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "config.yaml")


def _load_costs() -> dict:
    with open(_CONFIG_PATH) as f:
        cfg = yaml.safe_load(f)
    return cfg.get("backtest", {})


def dynamic_slippage_pct(entry: float, bars: list[dict], base_slip: float = 0.0002) -> float:
    """Estimate realistic slippage based on current volatility.

    Normal conditions (ATR 0.3%): ~0.02-0.04% slippage
    Elevated volatility (ATR 1.0%): ~0.08% slippage
    Flash crash (ATR 3.0%+): ~0.3%+ slippage

    Formula: slippage = max(base_slip, atr_pct × 0.12)
    """
    if not bars or len(bars) < 2:
        return base_slip
    trs = []
    for i in range(1, min(len(bars), 15)):
        b, p = bars[i], bars[i - 1]
        tr = max(b["h"] - b["l"], abs(b["h"] - p["c"]), abs(b["l"] - p["c"]))
        trs.append(tr)
    avg_tr = sum(trs) / len(trs) if trs else 0
    atr_pct = avg_tr / entry if entry > 0 else 0
    return max(base_slip, atr_pct * 0.12)


def apply_costs(
    raw_pnl:     float,
    entry:       float,
    qty:         float,
    hold_bars:   int,
    bar_minutes: int = 5,
    bars:        list[dict] | None = None,
) -> tuple[float, dict]:
    """Return (net_pnl, cost_detail) after deducting fees, slippage, and funding.

    Parameters
    ----------
    raw_pnl     : gross PnL before costs (USD)
    entry       : entry price
    qty         : position size in base asset (risk_amount / sl_dist)
    hold_bars   : number of bars the trade was held open
    bar_minutes : bar duration in minutes (5, 15, or 60)

    Returns
    -------
    net_pnl     : raw_pnl minus all costs
    cost_detail : {"total", "fee", "slip", "funding"} — all in USD
    """
    cfg       = _load_costs()
    taker     = float(cfg.get("taker_fee_pct",       0.0005))
    base_slip = float(cfg.get("slippage_pct",         0.0002))
    funding   = float(cfg.get("funding_cost_per_8h",  0.0001))

    notional  = entry * qty
    fee_cost  = round(notional * taker * 2, 6)
    slip      = dynamic_slippage_pct(entry, bars or [], base_slip)
    slip_cost = round(notional * slip  * 2, 6)
    bars_per_8h     = 480.0 / max(bar_minutes, 1)
    funding_periods = max(hold_bars, 0) / bars_per_8h
    funding_cost    = round(notional * funding * funding_periods, 6)
    total_cost      = round(fee_cost + slip_cost + funding_cost, 4)

    net_pnl = round(raw_pnl - total_cost, 4)
    return net_pnl, {
        "total":   total_cost,
        "fee":     round(fee_cost,     4),
        "slip":    round(slip_cost,    4),
        "funding": round(funding_cost, 4),
    }
