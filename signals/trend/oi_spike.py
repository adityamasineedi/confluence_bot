"""OI Spike Fade signals — detects sudden OI surges followed by liquidation-cascade reversals.

Logic
-----
A large OI spike (≥ threshold % in lookback_hours) indicates a wave of leveraged longs/shorts
were just opened — likely by retail FOMO.  When price immediately rejects with a wick, those
positions face immediate liquidation pressure, creating a fade opportunity.

check_oi_spike_long  → short squeeze: OI spike + downward wick → price bounces LONG
check_oi_spike_short → long squeeze:  OI spike + upward  wick → price fades SHORT
"""
import logging
import os
import yaml

log = logging.getLogger(__name__)

_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "config.yaml")
with open(_CONFIG_PATH) as _f:
    _cfg = yaml.safe_load(_f)

_OS_CFG       = _cfg.get("oi_spike", {})
_SPIKE_PCT    = float(_OS_CFG.get("spike_pct",       0.15))   # 15% OI increase
_LOOKBACK_HRS = float(_OS_CFG.get("lookback_hours",  2.0))    # compare now vs 2h ago
_WICK_PCT     = float(_OS_CFG.get("wick_pct",        0.005))  # 0.5% wick minimum
_SL_BUFFER    = float(_OS_CFG.get("sl_buffer",       0.002))  # 0.2% beyond wick tip
_ATR_MULT     = float(_OS_CFG.get("atr_mult",        2.0))    # TP = entry +/- ATR*mult
_ATR_WINDOW   = int(_OS_CFG.get("atr_window",        14))
_EMA_PERIOD   = int(_OS_CFG.get("ema_period",        21))
_RSI_WINDOW   = int(_OS_CFG.get("rsi_window",        14))
_VOL_MULT     = float(_OS_CFG.get("vol_mult",        1.5))    # volume spike confirmation


# ── Helpers ───────────────────────────────────────────────────────────────────

def _oi_spike_pct(symbol: str, cache, lookback_hours: float) -> float | None:
    """Return fractional OI change aggregated across Binance + Bybit.

    Returns None when OI data is unavailable.
    """
    oi_now_bn  = cache.get_oi(symbol, offset_hours=0,             exchange="binance")
    oi_prev_bn = cache.get_oi(symbol, offset_hours=lookback_hours, exchange="binance")
    oi_now_by  = cache.get_oi(symbol, offset_hours=0,             exchange="bybit")
    oi_prev_by = cache.get_oi(symbol, offset_hours=lookback_hours, exchange="bybit")

    oi_now  = (oi_now_bn  or 0.0) + (oi_now_by  or 0.0)
    oi_prev = (oi_prev_bn or 0.0) + (oi_prev_by or 0.0)

    if oi_prev <= 0 or oi_now <= 0:
        return None
    return (oi_now - oi_prev) / oi_prev


def _ema(values: list[float], period: int) -> float:
    if not values:
        return 0.0
    k = 2.0 / (period + 1)
    ema = values[0]
    for v in values[1:]:
        ema = v * k + ema * (1 - k)
    return ema


def _rsi(closes: list[float], window: int) -> float:
    if len(closes) < window + 1:
        return 50.0
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains  = [d for d in deltas if d > 0]
    losses = [-d for d in deltas if d < 0]
    avg_gain = sum(gains[-window:])  / window if gains  else 0.0
    avg_loss = sum(losses[-window:]) / window if losses else 1e-9
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)


def _atr(candles: list[dict], window: int) -> float:
    if len(candles) < 2:
        return 0.0
    trs = []
    for i in range(1, len(candles)):
        h = candles[i]["h"]; l = candles[i]["l"]; pc = candles[i - 1]["c"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    recent = trs[-window:]
    return sum(recent) / len(recent) if recent else 0.0


# ── Public signal functions ───────────────────────────────────────────────────

def check_oi_spike_long(symbol: str, cache) -> dict | None:
    """Short-squeeze bounce: OI spike + lower wick rejection → fade short, go LONG.

    Returns dict{"fired": bool, "spike_pct": float, "wick_pct": float} or None.
    """
    candles = cache.get_ohlcv(symbol, _ATR_WINDOW + 5, "15m")
    if not candles or len(candles) < _ATR_WINDOW + 2:
        return None

    closes = [c["c"] for c in candles]
    last   = candles[-1]
    o, h, lo, c = last["o"], last["h"], last["l"], last["c"]

    spike = _oi_spike_pct(symbol, cache, _LOOKBACK_HRS)
    if spike is None or spike < _SPIKE_PCT:
        return {"fired": False, "spike_pct": spike or 0.0, "wick_pct": 0.0}

    # Lower wick rejection (price closed well above the low)
    body_size  = abs(c - o)
    lower_wick = min(o, c) - lo
    wick_pct   = lower_wick / lo if lo > 0 else 0.0
    if wick_pct < _WICK_PCT or lower_wick < body_size * 0.5:
        return {"fired": False, "spike_pct": spike, "wick_pct": wick_pct}

    # Price above EMA21 (buyers in control after wick)
    ema_val = _ema(closes[:-1], _EMA_PERIOD)
    if c < ema_val:
        return {"fired": False, "spike_pct": spike, "wick_pct": wick_pct}

    # RSI in neutral-to-oversold zone (35–55) — not already overbought
    rsi = _rsi(closes, _RSI_WINDOW)
    if not (35 <= rsi <= 55):
        return {"fired": False, "spike_pct": spike, "wick_pct": wick_pct}

    # Volume spike confirms liquidation cascade
    vols    = [c_["v"] for c_ in candles[:-1]]
    avg_vol = sum(vols[-20:]) / len(vols[-20:]) if len(vols) >= 20 else sum(vols) / max(len(vols), 1)
    if last["v"] < avg_vol * _VOL_MULT:
        return {"fired": False, "spike_pct": spike, "wick_pct": wick_pct}

    return {"fired": True, "spike_pct": spike, "wick_pct": wick_pct}


def check_oi_spike_short(symbol: str, cache) -> dict | None:
    """Long-squeeze fade: OI spike + upper wick rejection → fade long, go SHORT.

    Returns dict{"fired": bool, "spike_pct": float, "wick_pct": float} or None.
    """
    candles = cache.get_ohlcv(symbol, _ATR_WINDOW + 5, "15m")
    if not candles or len(candles) < _ATR_WINDOW + 2:
        return None

    closes = [c["c"] for c in candles]
    last   = candles[-1]
    o, h, lo, c = last["o"], last["h"], last["l"], last["c"]

    spike = _oi_spike_pct(symbol, cache, _LOOKBACK_HRS)
    if spike is None or spike < _SPIKE_PCT:
        return {"fired": False, "spike_pct": spike or 0.0, "wick_pct": 0.0}

    # Upper wick rejection
    body_size  = abs(c - o)
    upper_wick = h - max(o, c)
    wick_pct   = upper_wick / h if h > 0 else 0.0
    if wick_pct < _WICK_PCT or upper_wick < body_size * 0.5:
        return {"fired": False, "spike_pct": spike, "wick_pct": wick_pct}

    # Price below EMA21
    ema_val = _ema(closes[:-1], _EMA_PERIOD)
    if c > ema_val:
        return {"fired": False, "spike_pct": spike, "wick_pct": wick_pct}

    # RSI in neutral-to-overbought zone (45–65)
    rsi = _rsi(closes, _RSI_WINDOW)
    if not (45 <= rsi <= 65):
        return {"fired": False, "spike_pct": spike, "wick_pct": wick_pct}

    # Volume spike
    vols    = [c_["v"] for c_ in candles[:-1]]
    avg_vol = sum(vols[-20:]) / len(vols[-20:]) if len(vols) >= 20 else sum(vols) / max(len(vols), 1)
    if last["v"] < avg_vol * _VOL_MULT:
        return {"fired": False, "spike_pct": spike, "wick_pct": wick_pct}

    return {"fired": True, "spike_pct": spike, "wick_pct": wick_pct}


def get_oi_spike_levels(
    symbol: str, cache, direction: str
) -> tuple[float, float] | None:
    """Compute (stop_loss, take_profit) for an OI spike fade entry.

    LONG:  SL = candle.low  * (1 - sl_buffer),  TP = entry + ATR * atr_mult
    SHORT: SL = candle.high * (1 + sl_buffer),  TP = entry - ATR * atr_mult
    """
    candles = cache.get_ohlcv(symbol, _ATR_WINDOW + 5, "15m")
    if not candles or len(candles) < 2:
        return None

    last  = candles[-1]
    entry = last["c"]
    atr   = _atr(candles, _ATR_WINDOW)
    if atr <= 0 or entry <= 0:
        return None

    if direction == "LONG":
        sl = last["l"] * (1.0 - _SL_BUFFER)
        tp = entry + atr * _ATR_MULT
    else:
        sl = last["h"] * (1.0 + _SL_BUFFER)
        tp = entry - atr * _ATR_MULT

    return round(sl, 8), round(tp, 8)
