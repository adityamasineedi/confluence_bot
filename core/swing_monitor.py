"""Swing structure monitor — Telegram alert when buy confidence flips to 100%.

Runs every `interval` seconds. For each symbol it:
  1. Pulls 4H candles from cache
  2. Detects HH/HL/LH/LL pivot structure
  3. Sends a Telegram alert the first time buy_confidence reaches 1.0
     (i.e. both HH + HL confirmed) after being below 1.0
"""
import asyncio
import logging

log = logging.getLogger(__name__)

# Tracks last known confidence per symbol — initialised to -1 (unknown)
_prev_confidence: dict[str, float] = {}


# ── Standalone swing structure calc (mirrors metrics_api._calc_swing_structure) ─

def _calc_swing(candles: list, pivot_n: int = 3) -> dict:
    if len(candles) < pivot_n * 2 + 4:
        return {"structure": [], "buy_confidence": 0.0,
                "buy_zone_low": 0.0, "buy_zone_high": 0.0,
                "last_pivot_high": 0.0, "last_pivot_low": 0.0}

    pivot_highs: list[float] = []
    pivot_lows:  list[float] = []
    for i in range(pivot_n, len(candles) - pivot_n):
        if all(candles[i]["h"] >= candles[i - j]["h"] for j in range(1, pivot_n + 1)) and \
           all(candles[i]["h"] >= candles[i + j]["h"] for j in range(1, pivot_n + 1)):
            pivot_highs.append(candles[i]["h"])
        if all(candles[i]["l"] <= candles[i - j]["l"] for j in range(1, pivot_n + 1)) and \
           all(candles[i]["l"] <= candles[i + j]["l"] for j in range(1, pivot_n + 1)):
            pivot_lows.append(candles[i]["l"])

    structure: list[str] = []
    buy_score = 0
    total     = 0

    if len(pivot_highs) >= 2:
        if pivot_highs[-1] > pivot_highs[-2]:
            structure.append("HH"); buy_score += 1
        else:
            structure.append("LH")
        total += 1

    if len(pivot_lows) >= 2:
        if pivot_lows[-1] > pivot_lows[-2]:
            structure.append("HL"); buy_score += 1
        else:
            structure.append("LL")
        total += 1

    buy_confidence = round(buy_score / max(total, 1), 2)
    lph = pivot_highs[-1] if pivot_highs else 0.0
    lpl = pivot_lows[-1]  if pivot_lows  else 0.0
    bz_high = round((lpl + lph) / 2.0, 4) if lph and lpl else 0.0

    return {
        "structure":       structure,
        "buy_confidence":  buy_confidence,
        "buy_zone_low":    round(lpl, 6),
        "buy_zone_high":   bz_high,
        "last_pivot_high": round(lph, 6),
        "last_pivot_low":  round(lpl, 6),
    }


def _price_fmt(p: float) -> str:
    if p >= 100:
        return f"{p:,.0f}"
    if p >= 1:
        return f"{p:,.2f}"
    return f"{p:,.4f}"


# ── Entry point ───────────────────────────────────────────────────────────────

async def run_swing_monitor(symbols: list[str], cache, interval: float = 300.0) -> None:
    """Runs forever as an asyncio task.  Checks swing structure every `interval` sec."""
    log.info("Swing monitor started — interval=%.0fs  symbols=%s", interval, symbols)
    while True:
        try:
            await _tick(symbols, cache)
        except Exception:
            log.exception("Swing monitor tick error")
        await asyncio.sleep(interval)


async def _tick(symbols: list[str], cache) -> None:
    from notifications.telegram import send_text

    for sym in symbols:
        candles = cache.get_ohlcv(sym, 60, "4h")
        if not candles or len(candles) < 14:
            continue

        sw   = _calc_swing(candles)
        conf = sw["buy_confidence"]
        prev = _prev_confidence.get(sym, -1.0)
        _prev_confidence[sym] = conf

        # Alert only when first confirmed flip to 100% after being below
        if conf < 1.0 or prev == 1.0:
            continue
        if prev < 0:
            # First reading — just seed the state, don't alert yet
            continue

        price     = candles[-1]["c"]
        struct    = " + ".join(sw["structure"])
        bz_lo_fmt = _price_fmt(sw["buy_zone_low"])
        bz_hi_fmt = _price_fmt(sw["buy_zone_high"])
        lph_fmt   = _price_fmt(sw["last_pivot_high"])
        price_fmt = _price_fmt(price)

        msg = (
            f"🟢 <b>Swing structure confirmed bullish</b>\n"
            f"<b>{sym}</b>  {struct}  →  100% confidence\n"
            f"\n"
            f"Price:       <code>${price_fmt}</code>\n"
            f"Buy zone:    <code>${bz_lo_fmt} – ${bz_hi_fmt}</code>\n"
            f"Resistance:  <code>${lph_fmt}</code>\n"
        )
        await send_text(msg)
        log.info("Swing alert: %s HH+HL confirmed (was %.0f%%)", sym, prev * 100)
