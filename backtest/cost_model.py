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


def apply_costs(
    raw_pnl:     float,
    entry:       float,
    qty:         float,
    hold_bars:   int,
    bar_minutes: int = 5,
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
    cfg     = _load_costs()
    taker   = float(cfg.get("taker_fee_pct",       0.0005))
    slip    = float(cfg.get("slippage_pct",         0.0002))
    funding = float(cfg.get("funding_cost_per_8h",  0.0001))

    notional        = entry * qty
    fee_cost        = round(notional * taker * 2,  6)
    slip_cost       = round(notional * slip  * 2,  6)
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
