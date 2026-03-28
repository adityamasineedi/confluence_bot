"""OI Spike Fade strategy loop — runs independently every check_interval_secs.

Sequence each tick
------------------
1. Verify OI data is available for the symbol (skip gracefully if not).
2. Score via oi_spike_scorer.score().
3. Log every evaluation to the signals DB.
4. If fire=True and max_positions not exceeded: execute via executor.execute_signal().
5. Set per-symbol cooldown after each fire attempt.

Check interval: 60s (OI data refreshes every ~30s; 60s avoids double-firing on same bar).
Max simultaneous OI Spike positions: 2 (concentrated-risk strategy, not scatter).
"""
import asyncio
import logging
import os
import yaml

log = logging.getLogger(__name__)

_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "config.yaml")
with open(_CONFIG_PATH) as _f:
    _cfg = yaml.safe_load(_f)

_OS_CFG = _cfg.get("oi_spike", {})


async def run_oi_spike_loop(symbols: list[str], cache) -> None:
    """Entry point — runs forever, called as an asyncio task from main.py."""
    interval      = float(_OS_CFG.get("check_interval_secs", 60))
    max_positions = int(_OS_CFG.get("max_positions", 2))

    log.info(
        "OI Spike Fade loop started — interval=%.0fs  symbols=%s  max_positions=%d",
        interval, symbols, max_positions,
    )

    while True:
        try:
            await _tick(symbols, cache, max_positions)
        except Exception:
            log.exception("OI Spike tick error")
        await asyncio.sleep(interval)


async def _tick(symbols: list[str], cache, max_positions: int) -> None:
    from core.oi_spike_scorer import score as os_score, set_cooldown
    from core.executor import execute_signal
    from logging_.logger import TradeLogger

    logger     = TradeLogger()
    open_count = 0

    for symbol in symbols:
        if open_count >= max_positions:
            break

        # OI availability check — skip gracefully when data not yet populated
        oi_val = cache.get_oi(symbol, offset_hours=0, exchange="binance")
        if oi_val is None:
            log.debug("OI Spike skip %s — OI data unavailable", symbol)
            continue

        score_dicts = await os_score(symbol, cache)

        for score_dict in score_dicts:
            try:
                asyncio.create_task(logger.log_signal(score_dict))
            except Exception as exc:
                log.debug("OI Spike log_signal failed for %s: %s", symbol, exc)

            if not score_dict.get("fire"):
                log.debug(
                    "OI Spike skip %s %s  score=%.2f  signals=%s",
                    score_dict["direction"], symbol,
                    score_dict["score"],
                    {k: v for k, v in score_dict["signals"].items() if not v},
                )
                continue

            if open_count >= max_positions:
                break

            log.info(
                "OI Spike FIRE %s %s  score=%.2f  sl=%.6f  tp=%.6f",
                score_dict["direction"], symbol,
                score_dict["score"],
                score_dict.get("os_stop", 0),
                score_dict.get("os_tp",   0),
            )

            order = await execute_signal(score_dict, cache)
            set_cooldown(symbol)   # always cool down after a fire attempt
            if order:
                open_count += 1
                log.info(
                    "OI Spike order placed: %s %s  entry=%.6f  sl=%.6f  tp=%.6f  qty=%.4f",
                    score_dict["direction"], symbol,
                    order.get("entry", 0), order.get("stop", 0),
                    order.get("take_profit", 0), order.get("qty", 0),
                )

    if open_count:
        log.info("OI Spike tick complete — %d position(s) entered", open_count)
