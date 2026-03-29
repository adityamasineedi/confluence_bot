"""VWAP Band Reversion backtest engine — bar-by-bar replay on 15m OHLCV.

Strategy
--------
- Rolling VWAP ± 2σ bands over window_bars (20 × 15m = 5h).
- Entry when price touches and closes back inside the ±2σ band.
- ADX gate: block if 4H ADX ≥ 30 (trending market kills mean reversion).
- RSI gate: ≤ 35 for LONG, ≥ 65 for SHORT.
- TP = VWAP midline (dynamic, computed at entry). SL = band edge ± buffer.

Entry  : close of the bar that re-enters inside the band
SL     : band_level ± sl_buffer_pct
TP     : vwap midline at entry time
"""
import logging
import os
import yaml

from backtest.cost_model import apply_costs
from backtest.regime_classifier import classify_regime
from signals.volume_momentum import get_volume_params_static

_BAR_MINUTES = 15

log = logging.getLogger(__name__)

_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "config.yaml")
with open(_CONFIG_PATH) as _f:
    _cfg = yaml.safe_load(_f)

_VB_CFG       = _cfg.get("vwap_band", {})
_WINDOW       = int(_VB_CFG.get("window_bars",    20))
_BAND_MULT    = float(_VB_CFG.get("band_mult",     2.0))
_RSI_LONG_MAX = float(_VB_CFG.get("rsi_long_max",  35))
_RSI_SHORT_MIN= float(_VB_CFG.get("rsi_short_min", 65))
_ADX_MAX      = float(_VB_CFG.get("adx_max",       30))
_VOL_MAX_MULT = float(_VB_CFG.get("vol_max_mult",   1.5))
_SL_BUFFER    = float(_VB_CFG.get("sl_buffer_pct",  0.002))
_MIN_RR       = 1.5   # inline RR gate: |vwap - entry| / |entry - SL| >= 1.5
_RSI_WINDOW   = 14
_ADX_WINDOW   = 14
_COOLDOWN_BARS = int(_VB_CFG.get("cooldown_mins", 30) // 15)  # 30min → 2 × 15m bars
_MAX_HOLD      = 8    # 8 × 15m = 2h


def _ts_str(ts_ms: int) -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


def _compute_vwap_bands(bars: list[dict]) -> dict | None:
    """Compute rolling VWAP and ±2σ bands over bars slice."""
    if len(bars) < 2:
        return None
    total_vol = sum(b["v"] for b in bars)
    if total_vol <= 0:
        return None
    vwap = sum((b["h"] + b["l"] + b["c"]) / 3 * b["v"] for b in bars) / total_vol
    var  = sum(b["v"] * (((b["h"] + b["l"] + b["c"]) / 3) - vwap) ** 2 for b in bars) / total_vol
    std  = var ** 0.5
    return {
        "vwap":    vwap,
        "upper_2": vwap + _BAND_MULT * std,
        "lower_2": vwap - _BAND_MULT * std,
        "std_dev": std,
    }


def _rsi(closes: list[float], window: int = _RSI_WINDOW) -> float:
    if len(closes) < window + 1:
        return 50.0
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0.0)); losses.append(max(-d, 0.0))
    ag = sum(gains[-window:]) / window
    al = sum(losses[-window:]) / window or 1e-9
    return 100.0 - 100.0 / (1.0 + ag / al)


def _adx(bars: list[dict], window: int = _ADX_WINDOW) -> float:
    """Simplified ADX from 4H bars slice."""
    if len(bars) < window + 2:
        return 0.0
    trs, pdms, ndms = [], [], []
    for i in range(1, len(bars)):
        h, l, ph, pl, pc = bars[i]["h"], bars[i]["l"], bars[i-1]["h"], bars[i-1]["l"], bars[i-1]["c"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
        pdms.append(max(h - ph, 0) if (h - ph) > (pl - l) else 0)
        ndms.append(max(pl - l, 0) if (pl - l) > (h - ph) else 0)
    atr  = sum(trs[-window:]) / window or 1e-9
    pdi  = sum(pdms[-window:]) / window / atr * 100
    ndi  = sum(ndms[-window:]) / window / atr * 100
    if pdi + ndi == 0:
        return 0.0
    dx   = abs(pdi - ndi) / (pdi + ndi) * 100
    return dx   # simplified single-period DX used as ADX proxy


def _check_exit(trade: dict, bar: dict, vwap_mid: float) -> dict | None:
    """Check SL/TP; for VWAP Band, TP is the dynamic VWAP midline."""
    risk = trade["risk_amount"]
    sl   = trade["sl"]
    tp   = vwap_mid   # chase VWAP dynamically

    if trade["direction"] == "LONG":
        if bar["l"] <= sl:
            return {**trade, "exit_ts": bar["ts"], "outcome": "LOSS",
                    "pnl": round(-risk, 4)}
        if bar["h"] >= tp:
            pnl_r = abs(tp - trade["entry"]) / abs(trade["entry"] - sl) if abs(trade["entry"] - sl) > 0 else 0
            return {**trade, "exit_ts": bar["ts"], "outcome": "WIN",
                    "pnl": round(risk * pnl_r, 4)}
    else:
        if bar["h"] >= sl:
            return {**trade, "exit_ts": bar["ts"], "outcome": "LOSS",
                    "pnl": round(-risk, 4)}
        if bar["l"] <= tp:
            pnl_r = abs(trade["entry"] - tp) / abs(sl - trade["entry"]) if abs(sl - trade["entry"]) > 0 else 0
            return {**trade, "exit_ts": bar["ts"], "outcome": "WIN",
                    "pnl": round(risk * pnl_r, 4)}
    return None


def _force_close(trade: dict, bar: dict) -> dict:
    entry = trade["entry"]; close = bar["c"]; risk = trade["risk_amount"]
    sl_dist = abs(entry - trade["sl"])
    if sl_dist <= 0:
        return {**trade, "exit_ts": bar["ts"], "outcome": "TIMEOUT", "pnl": 0.0}
    pct = ((close - entry) / entry) if trade["direction"] == "LONG" else ((entry - close) / entry)
    pnl = max(pct / (sl_dist / entry) * risk, -risk)
    return {**trade, "exit_ts": bar["ts"], "outcome": "TIMEOUT", "pnl": round(pnl, 4)}


def run(
    symbols:          list[str],
    ohlcv:            dict[str, list[dict]],
    starting_capital: float = 100.0,
    risk_pct:         float = 0.01,
) -> list[dict]:
    """Run VWAP Band Reversion backtest on 15m bars. Returns list of closed trades."""
    all_closed: list[dict] = []
    warmup = _WINDOW + _RSI_WINDOW + 5

    for sym in symbols:
        bars_15m = ohlcv.get(f"{sym}:15m", [])
        bars_4h  = ohlcv.get(f"{sym}:4h",  [])
        bars_1d  = ohlcv.get(f"{sym}:1d",  [])
        if len(bars_15m) < warmup + 10:
            log.warning("VWAPBand: insufficient 15m data for %s (%d bars)", sym, len(bars_15m))
            continue

        equity         = starting_capital
        eval_bars      = bars_15m[warmup:]
        open_trade: dict | None  = None
        cooldown_until = 0

        log.info("VWAPBand backtest %s: %d bars (%s → %s)",
                 sym, len(eval_bars),
                 _ts_str(eval_bars[0]["ts"]), _ts_str(eval_bars[-1]["ts"]))

        for bar_idx, bar in enumerate(eval_bars):
            global_idx = warmup + bar_idx
            bar_ts     = bar["ts"]

            # ── Compute current VWAP bands (for exit TP tracking) ──────────────
            band_slice = bars_15m[max(0, global_idx - _WINDOW): global_idx + 1]
            bands      = _compute_vwap_bands(band_slice)
            vwap_mid   = bands["vwap"] if bands else (open_trade["entry"] if open_trade else bar["c"])

            # ── Manage open trade ──────────────────────────────────────────────
            if open_trade is not None:
                result = _check_exit(open_trade, bar, vwap_mid)
                if result is not None:
                    hold_bars = bar_idx - open_trade["bar_idx"]
                    gross_pnl = result["pnl"]
                    net_pnl, cost_d = apply_costs(gross_pnl, open_trade["entry"], open_trade["qty"], hold_bars, _BAR_MINUTES)
                    result["gross_pnl"] = gross_pnl; result["cost"] = cost_d["total"]
                    result["cost_fee"]  = cost_d["fee"]; result["cost_slip"] = cost_d["slip"]
                    result["cost_fund"] = cost_d["funding"]; result["pnl"] = net_pnl
                    equity += net_pnl
                    result["equity_after"] = round(equity, 4)
                    all_closed.append(result)
                    cooldown_until = bar_idx + _COOLDOWN_BARS
                    open_trade = None
                elif bar_idx - open_trade["bar_idx"] >= _MAX_HOLD:
                    result = _force_close(open_trade, bar)
                    hold_bars = bar_idx - open_trade["bar_idx"]
                    gross_pnl = result["pnl"]
                    net_pnl, cost_d = apply_costs(gross_pnl, open_trade["entry"], open_trade["qty"], hold_bars, _BAR_MINUTES)
                    result["gross_pnl"] = gross_pnl; result["cost"] = cost_d["total"]
                    result["cost_fee"]  = cost_d["fee"]; result["cost_slip"] = cost_d["slip"]
                    result["cost_fund"] = cost_d["funding"]; result["pnl"] = net_pnl
                    equity += net_pnl
                    result["equity_after"] = round(equity, 4)
                    all_closed.append(result)
                    cooldown_until = bar_idx + _COOLDOWN_BARS
                    open_trade = None
                continue

            if bar_idx < cooldown_until:
                continue

            if bands is None:
                continue

            # ── ADX gate (4H) ─────────────────────────────────────────────────
            b4h_now = [b for b in bars_4h if b["ts"] <= bar_ts]
            if len(b4h_now) >= _ADX_WINDOW + 2:
                adx_val = _adx(b4h_now[-(  _ADX_WINDOW + 5):])
                if adx_val >= _ADX_MAX:
                    continue   # trending — skip mean reversion

            # ── Regime + dynamic volume params ────────────────────────────────
            if len(b4h_now) >= 28:
                _c4h = b4h_now[-30:]
                _c1d = [b for b in bars_1d if b["ts"] <= bar_ts][-60:]
                regime = classify_regime(
                    closes_4h=[b["c"] for b in _c4h],
                    highs_4h =[b["h"] for b in _c4h],
                    lows_4h  =[b["l"] for b in _c4h],
                    closes_1d=[b["c"] for b in _c1d],
                )
            else:
                regime = "TREND"
            vol_params = get_volume_params_static(sym, regime, "15m")

            # ── RSI ───────────────────────────────────────────────────────────
            closes   = [b["c"] for b in bars_15m[max(0, global_idx - 20): global_idx + 1]]
            rsi      = _rsi(closes)

            # ── Volume filter (dynamic quiet_mult from vol_params) ────────────
            vol_slice = bars_15m[max(0, global_idx - 21): global_idx]
            avg_vol   = sum(b["v"] for b in vol_slice) / len(vol_slice) if vol_slice else 0
            vol_ok    = avg_vol <= 0 or bar["v"] <= avg_vol * vol_params.quiet_mult

            prev_bar  = bars_15m[global_idx - 1]
            direction = None
            sl = tp   = 0.0

            # ── LONG: price touched lower_2 then closed back inside ────────────
            if (rsi <= _RSI_LONG_MAX and vol_ok
                    and prev_bar["l"] <= bands["lower_2"]
                    and bar["c"] > bands["lower_2"]):
                sl   = bands["lower_2"] * (1.0 - _SL_BUFFER)
                dist = abs(bar["c"] - sl)
                rr_avail = abs(vwap_mid - bar["c"]) / dist if dist > 0 else 0
                if dist > 0 and rr_avail >= _MIN_RR:
                    tp        = vwap_mid
                    direction = "LONG"

            # ── SHORT: price touched upper_2 then closed back inside ───────────
            elif (rsi >= _RSI_SHORT_MIN and vol_ok
                    and prev_bar["h"] >= bands["upper_2"]
                    and bar["c"] < bands["upper_2"]):
                sl   = bands["upper_2"] * (1.0 + _SL_BUFFER)
                dist = abs(sl - bar["c"])
                rr_avail = abs(bar["c"] - vwap_mid) / dist if dist > 0 else 0
                if dist > 0 and rr_avail >= _MIN_RR:
                    tp        = vwap_mid
                    direction = "SHORT"

            if direction is None:
                continue

            risk_amount = round(equity * risk_pct, 4)
            sl_dist     = abs(bar["c"] - sl)
            qty         = round(risk_amount / sl_dist, 8) if sl_dist > 0.0 else 0.0
            open_trade = {
                "symbol":      sym,
                "regime":      "VWAPBAND",
                "direction":   direction,
                "entry":       bar["c"],
                "sl":          sl,
                "tp":          tp,
                "entry_ts":    bar_ts,
                "bar_idx":     bar_idx,
                "risk_amount": risk_amount,
                "qty":         qty,
                "score":       0.67,
            }

        # Force-close remaining
        if open_trade is not None:
            result = _force_close(open_trade, eval_bars[-1])
            hold_bars = len(eval_bars) - 1 - open_trade["bar_idx"]
            gross_pnl = result["pnl"]
            net_pnl, cost_d = apply_costs(gross_pnl, open_trade["entry"], open_trade["qty"], hold_bars, _BAR_MINUTES)
            result["gross_pnl"] = gross_pnl; result["cost"] = cost_d["total"]
            result["cost_fee"]  = cost_d["fee"]; result["cost_slip"] = cost_d["slip"]
            result["cost_fund"] = cost_d["funding"]; result["pnl"] = net_pnl
            equity += net_pnl
            result["equity_after"] = round(equity, 4)
            all_closed.append(result)

    total_pnl = sum(t["pnl"] for t in all_closed)
    log.info("VWAPBand backtest complete: %d trades  total_pnl=$%.2f", len(all_closed), total_pnl)
    return all_closed
