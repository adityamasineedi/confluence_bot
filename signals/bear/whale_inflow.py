"""Whale exchange inflow signal — large on-chain deposits as bearish sell-pressure signal."""

_INFLOW_SPIKE_MULT = 2.0   # current inflow must be ≥ 2× the rolling MA
_INFLOW_MA_WINDOW  = 24    # rolling window for the inflow moving average (hours)


def check_whale_exchange_inflow(symbol: str, cache) -> bool:
    """True when exchange inflows spike ≥ 2× above their rolling average.

    Large deposits to exchanges from whales signal imminent sell pressure —
    coins moving onto exchanges are typically moved there to be sold.

    Uses cache.get_exchange_inflow() for the latest value and
    cache.get_inflow_ma() for the rolling average.
    """
    inflow_now = cache.get_exchange_inflow(symbol)
    if inflow_now is None:
        return False

    inflow_ma = cache.get_inflow_ma(symbol, days=_INFLOW_MA_WINDOW)
    if inflow_ma == 0.0:
        return False

    return inflow_now >= inflow_ma * _INFLOW_SPIKE_MULT
