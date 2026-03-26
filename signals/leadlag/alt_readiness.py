"""Alt readiness check — ensures the alt has NOT already priced in BTC's move.

If BTCUSDT already moved 0.3% up and ETHUSDT has also already moved 0.3% up,
there is no lag left to capture — skip it.

Also verifies the alt has sufficient recent 5m data.
"""
import logging

log = logging.getLogger(__name__)


def check_alt_ready(symbol: str, direction: str, cache, cfg: dict) -> dict:
    """Check whether *symbol* still has lag to capture in *direction*.

    Returns
    -------
    dict with keys:
        ready        bool    — True if the alt is a valid entry candidate
        premove_pct  float   — price change over the last 3 × 5m bars
        reason       str     — human-readable gate result
    """
    max_premove = float(cfg.get("max_alt_premove_pct", 0.003))

    # Need at least 4 bars: we measure from bar[-4].close to bar[-1].close
    bars = cache.get_ohlcv(symbol, window=4, tf="5m")
    if len(bars) < 4:
        return {"ready": False, "premove_pct": 0.0, "reason": "insufficient_5m_data"}

    start_price = bars[-4]["c"]
    curr_price  = bars[-1]["c"]

    if start_price <= 0.0:
        return {"ready": False, "premove_pct": 0.0, "reason": "zero_price"}

    premove = (curr_price - start_price) / start_price  # signed

    if direction == "LONG" and premove >= max_premove:
        log.debug("%s already moved +%.3f%% — skipping LONG leadlag", symbol, premove * 100)
        return {"ready": False, "premove_pct": round(premove, 5), "reason": "already_moved_up"}

    if direction == "SHORT" and premove <= -max_premove:
        log.debug("%s already moved %.3f%% — skipping SHORT leadlag", symbol, premove * 100)
        return {"ready": False, "premove_pct": round(premove, 5), "reason": "already_moved_down"}

    return {"ready": True, "premove_pct": round(premove, 5), "reason": "ok"}
