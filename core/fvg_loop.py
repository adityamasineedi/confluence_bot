"""FVG Fill strategy loop — runs independently every check_interval_secs.

Sequence each tick
------------------
1. For each symbol, score it via fvg_scorer.score().
2. Log every evaluation to the signals DB.
3. If fire=True and max_positions not exceeded: execute via executor.execute_signal().
4. Set per-symbol cooldown after each entry.

Check interval: 60 s (aligns with 1H bar closes so FVG formations are fresh).
Max simultaneous FVG positions: 3 (controlled by max_positions in config).
"""
import asyncio
import logging
import os
import yaml

log = logging.getLogger(__name__)

_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "config.yaml")
with open(_CONFIG_PATH) as _f:
    _cfg = yaml.safe_load(_f)

_FVG_CFG = _cfg.get("fvg", {})


async def run_fvg_loop(symbols: list[str], cache) -> None:
    """Entry point — runs forever, called as an asyncio task from main.py."""
    interval      = float(_FVG_CFG.get("check_interval_secs", 60))
    max_positions = int(_FVG_CFG.get("max_positions", 3))

    log.info(
        "FVG loop started — interval=%.0fs  symbols=%s  max_positions=%d",
        interval, symbols, max_positions,
    )

    while True:
        try:
            await _tick(symbols, cache, max_positions)
        except Exception:
            log.exception("FVG tick error")
        await asyncio.sleep(interval)


async def _tick(symbols: list[str], cache, max_positions: int) -> None:
    from core.fvg_scorer import score as fvg_score, set_cooldown
    from core.executor import execute_signal
    from logging_.logger import TradeLogger

    logger     = TradeLogger()
    open_count = 0

    for symbol in symbols:
        if open_count >= max_positions:
            break

        score_dicts = await fvg_score(symbol, cache)

        for score_dict in score_dicts:
            # Log every evaluation regardless of fire
            try:
                asyncio.create_task(logger.log_signal(score_dict))
            except Exception as exc:
                log.debug("FVG log_signal failed for %s: %s", symbol, exc)

            if not score_dict.get("fire"):
                log.debug(
                    "FVG skip %s %s  score=%.2f  signals=%s",
                    score_dict["direction"], symbol,
                    score_dict["score"],
                    {k: v for k, v in score_dict["signals"].items() if not v},
                )
                continue

            if open_count >= max_positions:
                break

            log.info(
                "FVG FIRE %s %s  score=%.2f  sl=%.6f  tp=%.6f",
                score_dict["direction"], symbol,
                score_dict["score"],
                score_dict.get("fvg_stop", 0),
                score_dict.get("fvg_tp", 0),
            )

            order = await execute_signal(score_dict, cache)
            set_cooldown(symbol)   # always cool down after a fire attempt
            if order:
                open_count += 1
                log.info(
                    "FVG order placed: %s %s  entry=%.6f  sl=%.6f  tp=%.6f  qty=%.4f",
                    score_dict["direction"], symbol,
                    order.get("entry", 0), order.get("stop", 0),
                    order.get("take_profit", 0), order.get("qty", 0),
                )

    if open_count:
        log.info("FVG tick complete — %d position(s) entered", open_count)
