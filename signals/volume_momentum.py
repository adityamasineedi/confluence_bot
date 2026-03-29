"""Dynamic volume momentum signals.

All thresholds adapt to:
  - Market regime    (TREND / RANGE / CRASH / PUMP / BREAKOUT)
  - Timeframe        (5m / 15m / 1h / 4h — lower TF needs higher mult)
  - Coin tier        (tier1=BTC tight, tier2=medium, tier3=volatile)
  - Liquidation data (OI flush events change what "spike" means)

Usage:
    from signals.volume_momentum import VolumeContext, get_volume_params
    ctx = VolumeContext(symbol, regime, timeframe, cache)
    params = get_volume_params(ctx)
    if params.spike(bars):
        ...

Backtest usage (no cache / no liquidation adjustment):
    from signals.volume_momentum import get_volume_params_static
    params = get_volume_params_static(symbol, regime, "5m")
"""
from __future__ import annotations
import os
import yaml
from dataclasses import dataclass

_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "config.yaml")
_cfg_cache: dict = {}


def _load_cfg() -> dict:
    global _cfg_cache
    if not _cfg_cache:
        with open(_CONFIG_PATH) as f:
            _cfg_cache = yaml.safe_load(f)
    return _cfg_cache


@dataclass
class VolumeContext:
    """All context needed to compute dynamic volume thresholds."""
    symbol:    str
    regime:    str    # TREND / RANGE / CRASH / PUMP / BREAKOUT
    timeframe: str    # 5m / 15m / 1h / 4h
    cache:     object = None   # live DataCache (None in backtest)

    @property
    def tier(self) -> str:
        cfg = _load_cfg()
        tiers = cfg.get("symbol_tiers", {})
        for tier_name, tier_data in tiers.items():
            if self.symbol.upper() in [s.upper() for s in tier_data.get("symbols", [])]:
                return tier_name
        return "base"


@dataclass
class VolumeParams:
    """Dynamic volume thresholds computed for a specific context."""
    spike_mult:      float   # volume > avg × this = spike
    quiet_mult:      float   # volume < avg × this = quiet
    divergence_bars: int     # bars to check for divergence
    momentum_bars:   int     # bars to check for increasing momentum
    rvol_min:        float   # minimum relative volume to allow entry
    lookback:        int     # bars for rolling average
    liq_spike_mult:  float   # liquidation-adjusted spike threshold

    def spike(self, bars: list[dict]) -> bool:
        """Current bar is a volume spike relative to recent average."""
        if not bars or len(bars) < self.lookback + 1:
            return False
        avg = sum(b["v"] for b in bars[-self.lookback - 1:-1]) / self.lookback
        return avg > 0 and bars[-1]["v"] >= avg * self.spike_mult

    def quiet(self, bars: list[dict]) -> bool:
        """Current bar has quiet volume — consolidation confirmed."""
        if not bars or len(bars) < self.lookback + 1:
            return False
        avg = sum(b["v"] for b in bars[-self.lookback - 1:-1]) / self.lookback
        return avg > 0 and bars[-1]["v"] <= avg * self.quiet_mult

    def increasing(self, bars: list[dict]) -> bool:
        """Volume increasing over last N bars — momentum building, range break risk."""
        if not bars or len(bars) < self.momentum_bars + 1:
            return False
        vols = [b["v"] for b in bars[-self.momentum_bars - 1:]]
        return all(vols[i] < vols[i + 1] for i in range(len(vols) - 1))

    def bearish_divergence(self, bars: list[dict]) -> bool:
        """Price higher highs + volume falling = distribution. Block longs."""
        if not bars or len(bars) < self.divergence_bars + 1:
            return False
        price_up = bars[-1]["c"] > bars[-self.divergence_bars]["c"]
        vols = [b["v"] for b in bars[-self.divergence_bars:]]
        vol_declining = vols[-1] < vols[0] * 0.8
        return price_up and vol_declining

    def bullish_divergence(self, bars: list[dict]) -> bool:
        """Price lower lows + volume falling = accumulation. Block shorts."""
        if not bars or len(bars) < self.divergence_bars + 1:
            return False
        price_down = bars[-1]["c"] < bars[-self.divergence_bars]["c"]
        vols = [b["v"] for b in bars[-self.divergence_bars:]]
        vol_declining = vols[-1] < vols[0] * 0.8
        return price_down and vol_declining

    def rvol(self, bars: list[dict]) -> float:
        """Relative volume: current / average at same hour. 1.0 = normal."""
        if not bars or len(bars) < 2:
            return 1.0
        current_hour = (bars[-1].get("ts", 0) // 3600000) % 24
        same_hour = [b for b in bars[:-1]
                     if (b.get("ts", 0) // 3600000) % 24 == current_hour][-5:]
        if not same_hour:
            avg = sum(b["v"] for b in bars[-21:-1]) / 20 if len(bars) >= 21 else 1.0
            return bars[-1]["v"] / avg if avg > 0 else 1.0
        avg = sum(b["v"] for b in same_hour) / len(same_hour)
        return bars[-1]["v"] / avg if avg > 0 else 1.0

    def rvol_ok(self, bars: list[dict]) -> bool:
        """Entry volume is acceptable relative to time-of-day baseline."""
        return self.rvol(bars) >= self.rvol_min


def get_volume_params(ctx: VolumeContext) -> VolumeParams:
    """Return dynamic volume thresholds for this specific context.

    Reads from config.yaml volume_momentum section; falls back to
    hardcoded defaults if keys are missing (safe for backward compat).

    Logic:
      CRASH/PUMP: very high thresholds — only institutional moves count
      TREND:      medium thresholds — trend continuation needs confirmation
      RANGE:      lower thresholds — absorption happens at lower volume
      BREAKOUT:   high thresholds — fake breakouts dominate on low volume

      Tier1 (BTC): tightest — most liquid, cleanest volume signals
      Tier2:       medium
      Tier3:       loosest — erratic volume, higher noise floor

      Short TF (5m): higher mult needed (noisier)
      Long TF (4h):  lower mult (smoother, more meaningful)
    """
    cfg = _load_cfg()
    vm  = cfg.get("volume_momentum", {})

    # ── Hardcoded fallbacks ───────────────────────────────────────────────────
    _hc_spike = {"TREND": 1.5, "RANGE": 1.3, "CRASH": 2.5, "PUMP": 2.5, "BREAKOUT": 2.0}
    _hc_quiet = {"TREND": 0.8, "RANGE": 0.7, "CRASH": 0.5, "PUMP": 0.5, "BREAKOUT": 0.6}
    _hc_rvol  = {"TREND": 0.7, "RANGE": 0.6, "CRASH": 0.9, "PUMP": 0.9, "BREAKOUT": 0.8}

    # ── Base multipliers by regime ────────────────────────────────────────────
    rk         = ctx.regime.upper()
    spike_base = float(vm.get("spike_mult", {}).get(rk, _hc_spike.get(rk, 1.5)))
    quiet_base = float(_hc_quiet.get(rk, 0.8))   # no config section for quiet
    rvol_base  = float(vm.get("rvol_min",   {}).get(rk, _hc_rvol.get(rk, 0.7)))

    # ── Tier adjustment ───────────────────────────────────────────────────────
    _hc_tier_spike = {"tier1": -0.2, "tier2":  0.0, "tier3": 0.4, "base": 0.2}
    _hc_tier_quiet = {"tier1":  0.1, "tier2":  0.0, "tier3":-0.1, "base": 0.0}
    tier_spike_adj = float(vm.get("tier_spike_adj", {}).get(ctx.tier, _hc_tier_spike.get(ctx.tier, 0.0)))
    tier_quiet_adj = float(_hc_tier_quiet.get(ctx.tier, 0.0))

    # ── Timeframe adjustment ──────────────────────────────────────────────────
    _hc_tf_spike = {"5m": 0.3, "15m": 0.1, "1h": 0.0, "4h": -0.2}
    _tf_meta = {
        "5m":  {"lookback": 20, "div_bars":  5, "mom_bars": 3},
        "15m": {"lookback": 20, "div_bars":  6, "mom_bars": 3},
        "1h":  {"lookback": 24, "div_bars":  8, "mom_bars": 4},
        "4h":  {"lookback": 20, "div_bars": 10, "mom_bars": 4},
    }
    tf_spike_adj = float(vm.get("tf_spike_adj", {}).get(ctx.timeframe, _hc_tf_spike.get(ctx.timeframe, 0.0)))
    tf_meta      = _tf_meta.get(ctx.timeframe, {"lookback": 20, "div_bars": 6, "mom_bars": 3})

    spike_mult = round(spike_base + tier_spike_adj + tf_spike_adj, 2)
    quiet_mult = round(quiet_base + tier_quiet_adj, 2)

    # ── Liquidation adjustment ────────────────────────────────────────────────
    # If recent liquidations are large (OI flush), volume spike threshold rises
    # because the spike is just liquidation noise, not clean entry signal.
    liq_cfg   = vm.get("liq_adjust", {})
    small_usd = float(liq_cfg.get("small_usd", 100_000))
    large_usd = float(liq_cfg.get("large_usd", 500_000))

    liq_spike_mult = spike_mult
    if ctx.cache is not None:
        try:
            liqs = ctx.cache.get_liquidations(ctx.symbol, window_seconds=300)
            if liqs:
                total_liq_usd = sum(abs(l.get("qty", 0) * l.get("price", 0)) for l in liqs)
                if total_liq_usd > large_usd:
                    liq_spike_mult = spike_mult * 1.5
                elif total_liq_usd > small_usd:
                    liq_spike_mult = spike_mult * 1.2
        except Exception:
            pass

    return VolumeParams(
        spike_mult      = max(1.1, spike_mult),
        quiet_mult      = max(0.4, min(1.0, quiet_mult)),
        divergence_bars = tf_meta["div_bars"],
        momentum_bars   = tf_meta["mom_bars"],
        rvol_min        = rvol_base,
        lookback        = tf_meta["lookback"],
        liq_spike_mult  = max(1.1, liq_spike_mult),
    )


def get_volume_params_static(symbol: str, regime: str, timeframe: str) -> VolumeParams:
    """Backtest-safe version — no cache, no liquidation adjustment."""
    ctx = VolumeContext(symbol=symbol, regime=regime, timeframe=timeframe, cache=None)
    return get_volume_params(ctx)
