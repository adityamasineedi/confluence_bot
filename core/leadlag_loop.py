"""Lead-lag strategy loop — runs independently every check_interval_secs.

Sequence each tick
------------------
1. Check BTC for a fresh VWAP breakout with volume confirmation.
2. If no breakout → sleep and return.
3. For each alt (excluding BTCUSDT and configured exclusions):
   a. Score the alt via leadlag_scorer.score().
   b. Log every evaluation to the signals DB (same as main loop).
   c. If fire=True: execute via executor.execute_signal().
4. Cap entries at max_alts_per_signal per BTC event.
"""
import asyncio
import logging
import os
import yaml

log = logging.getLogger(__name__)

_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "config.yaml")
with open(_CONFIG_PATH) as _f:
    _cfg = yaml.safe_load(_f)

_LL_CFG = _cfg.get("leadlag", {})


async def run_leadlag_loop(symbols: list[str], cache) -> None:
    """Entry point — runs forever, called as an asyncio task from main.py."""
    interval   = float(_LL_CFG.get("check_interval_secs", 30))
    exclude    = set(_LL_CFG.get("exclude_symbols", ["BTCUSDT"]))
    max_alts   = int(_LL_CFG.get("max_alts_per_signal", 3))

    alts = [s for s in symbols if s not in exclude]

    log.info(
        "LeadLag loop started — interval=%.0fs  alts=%s  max_per_signal=%d",
        interval, alts, max_alts,
    )

    while True:
        try:
            await _tick(alts, cache, max_alts)
        except Exception:
            log.exception("LeadLag tick error")
        await asyncio.sleep(interval)


async def _tick(alts: list[str], cache, max_alts: int) -> None:
    from signals.leadlag.btc_momentum import check_btc_breakout
    from core.leadlag_scorer import score as ll_score
    from core.executor import execute_signal
    from logging_.logger import TradeLogger

    # Step 1: check BTC for a breakout
    btc_info = check_btc_breakout(cache, _LL_CFG)
    if btc_info is None:
        return  # no signal — nothing to do

    log.info(
        "LeadLag BTC breakout: %s  price=%.4f  vwap=%.4f  vol_ratio=%.2fx  strength=%.2f",
        btc_info["direction"], btc_info["btc_price"],
        btc_info["vwap"], btc_info["vol_ratio"], btc_info["strength"],
    )

    logger    = TradeLogger()
    fired     = 0

    # Step 2: score every alt, fire up to max_alts
    for symbol in alts:
        if fired >= max_alts:
            break

        score_dict = await ll_score(symbol, cache, btc_info)

        # Log every evaluation (mirrors main loop behaviour)
        try:
            asyncio.create_task(logger.log_signal(score_dict))
        except Exception as exc:
            log.debug("LeadLag log_signal failed for %s: %s", symbol, exc)

        if not score_dict.get("fire"):
            log.debug(
                "LeadLag skip %s  score=%.2f  signals=%s",
                symbol, score_dict["score"],
                {k: v for k, v in score_dict["signals"].items() if not v},
            )
            continue

        log.info(
            "LeadLag FIRE %s %s  score=%.2f  premove=%.3f%%  vol_ratio=%.2fx",
            score_dict["direction"], symbol,
            score_dict["score"],
            score_dict.get("premove", 0) * 100,
            score_dict.get("vol_ratio", 0),
        )

        order = await execute_signal(score_dict, cache)
        if order:
            fired += 1
            log.info(
                "LeadLag order placed: %s %s  entry=%.4f  sl=%.4f  tp=%.4f  qty=%.4f",
                score_dict["direction"], symbol,
                order.get("entry", 0), order.get("stop", 0),
                order.get("take_profit", 0), order.get("qty", 0),
            )

    if fired:
        log.info("LeadLag tick complete — %d alt(s) entered", fired)
