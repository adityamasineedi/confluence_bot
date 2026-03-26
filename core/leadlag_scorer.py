"""Lead-lag scorer — scores alt entry candidates after a confirmed BTC VWAP breakout.

Scoring
-------
Four boolean signals, equal-weighted (0.25 each):
  btc_vwap_break   — BTC crossed its rolling 1h VWAP with volume (always True here)
  vol_spike        — BTC 5m bar volume ≥ vol_spike_mult × 20-bar average
  alt_not_premoved — alt price has NOT already moved ≥ max_alt_premove_pct
  cooldown_ok      — symbol not in 30-min post-trade cooldown

BTC breakout *strength* adds a bonus of up to 0.10 to push borderline setups over
the threshold without changing the fundamental 4-signal gate.

Per-symbol cooldown
-------------------
After a lead-lag trade fires on *symbol*, that symbol is locked out for
``cooldown_mins`` minutes to avoid re-entering the same move multiple times.
The cooldown is in-process state — it resets on bot restart.
"""
import logging
import os
import time
import yaml

log = logging.getLogger(__name__)

_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "config.yaml")
with open(_CONFIG_PATH) as _f:
    _cfg = yaml.safe_load(_f)

_LL_CFG    = _cfg.get("leadlag", {})
_THRESHOLD = float(_LL_CFG.get("fire_threshold",  0.60))
_COOLDOWN  = float(_LL_CFG.get("cooldown_mins",   30)) * 60.0   # → seconds
_STOP_PCT  = float(_LL_CFG.get("stop_pct",  0.0020))
_TP_PCT    = float(_LL_CFG.get("tp_pct",    0.0050))

from core.cooldown_store import CooldownStore
_cd = CooldownStore("LEADLAG")


# ── Cooldown helpers ──────────────────────────────────────────────────────────

def is_on_cooldown(symbol: str) -> bool:
    return _cd.is_active(symbol)


def set_cooldown(symbol: str) -> None:
    """Call this after a lead-lag trade fires to block re-entry for cooldown_mins."""
    _cd.set(symbol, _COOLDOWN)
    log.debug("LeadLag cooldown set for %s (%.0f min)", symbol, _COOLDOWN / 60)


def cooldown_remaining(symbol: str) -> float:
    """Seconds remaining on cooldown; 0.0 if ready."""
    return _cd.remaining(symbol)


# ── Fixed-percentage RR calculator ───────────────────────────────────────────

def compute_levels(entry: float, direction: str) -> tuple[float, float]:
    """Return (stop_loss, take_profit) using fixed percentage offsets.

    Uses config ``stop_pct`` and ``tp_pct`` — not ATR-based because this is
    a short-horizon scalp where time-in-trade (not volatility) governs the risk.
    """
    if direction == "LONG":
        stop = round(entry * (1.0 - _STOP_PCT), 8)
        tp   = round(entry * (1.0 + _TP_PCT),   8)
    else:
        stop = round(entry * (1.0 + _STOP_PCT), 8)
        tp   = round(entry * (1.0 - _TP_PCT),   8)
    return stop, tp


# ── Main scorer ──────────────────────────────────────────────────────────────

async def score(symbol: str, cache, btc_info: dict) -> dict:
    """Score *symbol* as a lead-lag alt entry given a confirmed BTC breakout.

    Parameters
    ----------
    symbol   : altcoin to evaluate (never BTCUSDT)
    cache    : live DataCache
    btc_info : dict returned by btc_momentum.check_btc_breakout()

    Returns standard score dict compatible with executor.execute_signal():
        symbol, regime, direction, score, signals, fire
    Plus leadlag-specific keys:
        ll_stop, ll_tp   — pre-computed fixed-% stop/TP (used by executor)
        btc_vwap, btc_price, vol_ratio
    """
    from signals.leadlag.alt_readiness import check_alt_ready

    direction  = btc_info["direction"]
    vol_spike  = btc_info["vol_ratio"] >= float(_LL_CFG.get("vol_spike_mult", 1.5))
    cool_ok    = not is_on_cooldown(symbol)

    readiness  = check_alt_ready(symbol, direction, cache, _LL_CFG)
    alt_ready  = readiness["ready"]

    signals = {
        "btc_vwap_break":  True,      # BTC check already passed before calling this
        "vol_spike":       vol_spike,
        "alt_not_premoved": alt_ready,
        "cooldown_ok":     cool_ok,
    }

    # Base score: fraction of signals True (equal weight 0.25 each)
    base = sum(1 for v in signals.values() if v) / len(signals)

    # Strength bonus: up to +0.10 for a strong BTC breakout
    score_val = min(base + btc_info["strength"] * 0.10, 1.0)

    fire = (
        score_val >= _THRESHOLD
        and alt_ready      # hard gate: alt must not have already moved
        and cool_ok        # hard gate: no cooldown active
    )

    # Pre-compute fixed-% stop/TP for the executor
    entry          = cache.get_last_price(symbol)
    ll_stop, ll_tp = compute_levels(entry, direction) if entry > 0.0 else (0.0, 0.0)

    return {
        "symbol":    symbol,
        "regime":    "LEADLAG",
        "direction": direction,
        "score":     round(score_val, 4),
        "signals":   signals,
        "fire":      fire,
        # Leadlag-specific price levels (executor reads these instead of rr_calculator)
        "ll_stop":   ll_stop,
        "ll_tp":     ll_tp,
        # Informational
        "btc_vwap":  btc_info["vwap"],
        "btc_price": btc_info["btc_price"],
        "vol_ratio": btc_info["vol_ratio"],
        "premove":   readiness["premove_pct"],
    }
