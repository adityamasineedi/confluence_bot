"""HTF Demand/Supply Zone loop — 4H zone retest entries.

Runs every 60s (aligned with 1H bar closes for confirmation candle).
Lowest frequency of the three new strategies — fires 1-3× per symbol per day
on clean zone retests with high expected win rate.
"""
import asyncio
import logging
import os
import yaml

log = logging.getLogger(__name__)

_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "config.yaml")
with open(_CONFIG_PATH) as _f:
    _cfg = yaml.safe_load(_f)

_ZN_CFG = _cfg.get("zone", {})


async def run_zone_loop(symbols: list[str], cache) -> None:
    """Entry point — runs forever, called as an asyncio task from main.py."""
    interval      = float(_ZN_CFG.get("check_interval_secs", 60))
    max_positions = int(_ZN_CFG.get("max_positions", 2))

    log.info(
        "Zone Retest loop started — interval=%.0fs  symbols=%s  max_positions=%d",
        interval, symbols, max_positions,
    )

    while True:
        try:
            await _tick(symbols, cache, max_positions)
        except Exception:
            log.exception("Zone tick error")
        await asyncio.sleep(interval)


async def _tick(symbols: list[str], cache, max_positions: int) -> None:
    from core.zone_scorer import score as zn_score, set_cooldown
    from core.executor import execute_signal
    from logging_.logger import TradeLogger

    logger     = TradeLogger()
    open_count = 0

    for symbol in symbols:
        if open_count >= max_positions:
            break

        score_dicts = await zn_score(symbol, cache)

        for score_dict in score_dicts:
            try:
                asyncio.create_task(logger.log_signal(score_dict))
            except Exception as exc:
                log.debug("Zone log_signal failed %s: %s", symbol, exc)

            if not score_dict.get("fire"):
                log.debug(
                    "Zone skip %s %s  score=%.2f  signals=%s",
                    score_dict["direction"], symbol,
                    score_dict["score"],
                    {k: v for k, v in score_dict["signals"].items() if not v},
                )
                continue

            if open_count >= max_positions:
                break

            log.info(
                "Zone FIRE %s %s  score=%.2f  sl=%.6f  tp=%.6f",
                score_dict["direction"], symbol,
                score_dict["score"],
                score_dict.get("zn_stop", 0),
                score_dict.get("zn_tp", 0),
            )

            order = await execute_signal(score_dict, cache)
            set_cooldown(symbol)
            if order:
                open_count += 1
                log.info(
                    "Zone order placed: %s %s  entry=%.6f  sl=%.6f  tp=%.6f  qty=%.4f",
                    score_dict["direction"], symbol,
                    order.get("entry", 0), order.get("stop", 0),
                    order.get("take_profit", 0), order.get("qty", 0),
                )

    if open_count:
        log.info("Zone tick complete — %d position(s) entered", open_count)
