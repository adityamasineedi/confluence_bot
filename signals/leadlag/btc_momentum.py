"""BTC VWAP momentum detector — identifies fresh BTC breakouts on 5m with volume confirmation.

Logic
-----
1. Compute a rolling VWAP over the last ``vwap_window_bars`` × 5m candles.
2. Check the current bar vs previous bar — we want a *fresh* cross (previous bar
   was on the other side), not an extension of a move already in progress.
3. Current bar volume must exceed ``vol_spike_mult`` × 20-bar average.

Returns a dict on breakout, None otherwise.
"""
import logging

log = logging.getLogger(__name__)

_BTC = "BTCUSDT"


def _rolling_vwap(bars: list[dict]) -> float:
    """Typical-price VWAP: sum(tp × vol) / sum(vol). Returns 0.0 if vol is zero."""
    num = sum((b["h"] + b["l"] + b["c"]) / 3.0 * b["v"] for b in bars)
    den = sum(b["v"] for b in bars)
    return num / den if den > 0.0 else 0.0


def check_btc_breakout(cache, cfg: dict) -> dict | None:
    """Detect a fresh BTC 5m VWAP breakout with volume confirmation.

    Parameters
    ----------
    cache : DataCache
        Live market data cache.
    cfg : dict
        ``leadlag`` section from config.yaml.

    Returns
    -------
    dict with keys {direction, vwap, btc_price, vol_ratio, strength} or None.
    """
    window   = int(cfg.get("vwap_window_bars",   12))
    min_brk  = float(cfg.get("min_vwap_break_pct", 0.003))
    vol_mult = float(cfg.get("vol_spike_mult",     1.5))

    # Need: window bars for VWAP anchor + 1 previous bar + 1 current bar = window + 2
    bars = cache.get_ohlcv(_BTC, window=window + 2, tf="5m")
    if len(bars) < window + 2:
        return None  # not enough warmup data yet

    # VWAP anchor = bars just before the current one (not including current)
    anchor_bars = bars[-(window + 1):-1]
    curr_bar    = bars[-1]
    prev_bar    = bars[-2]

    vwap = _rolling_vwap(anchor_bars)
    if vwap == 0.0:
        return None

    curr_close = curr_bar["c"]
    prev_close = prev_bar["c"]

    # Volume confirmation: reject weak-volume moves
    vol_ma = cache.get_vol_ma(_BTC, window=20, tf="5m")
    if vol_ma == 0.0:
        return None
    vol_ratio = curr_bar["v"] / vol_ma
    if vol_ratio < vol_mult:
        return None

    gap_pct = (curr_close - vwap) / vwap  # positive = above VWAP

    # ── LONG: current cross above VWAP, previous bar was at or below VWAP ────
    if gap_pct >= min_brk and prev_close <= vwap * 1.0005:
        # Strength: how far above the minimum break threshold (0 → 1)
        strength = min(gap_pct / (min_brk * 4.0), 1.0)
        log.debug(
            "BTC VWAP LONG breakout  price=%.2f  vwap=%.2f  gap=%.3f%%  vol_ratio=%.2f",
            curr_close, vwap, gap_pct * 100, vol_ratio,
        )
        return {
            "direction": "LONG",
            "vwap":      round(vwap, 6),
            "btc_price": curr_close,
            "vol_ratio": round(vol_ratio, 2),
            "strength":  round(strength, 3),
        }

    # ── SHORT: current cross below VWAP, previous bar was at or above VWAP ──
    if -gap_pct >= min_brk and prev_close >= vwap * 0.9995:
        strength = min((-gap_pct) / (min_brk * 4.0), 1.0)
        log.debug(
            "BTC VWAP SHORT breakout  price=%.2f  vwap=%.2f  gap=%.3f%%  vol_ratio=%.2f",
            curr_close, vwap, -gap_pct * 100, vol_ratio,
        )
        return {
            "direction": "SHORT",
            "vwap":      round(vwap, 6),
            "btc_price": curr_close,
            "vol_ratio": round(vol_ratio, 2),
            "strength":  round(strength, 3),
        }

    return None
