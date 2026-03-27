"""Micro-range flip strategy loop — runs independently every check_interval_secs.

Sequence each tick
------------------
1. For each symbol, score it via microrange_scorer.score().
2. Log every evaluation to the signals DB.
3. If fire=True and max_positions not exceeded: execute via executor.execute_signal().
4. Set per-symbol cooldown after each entry.
"""
import asyncio
import logging
import os
import yaml

log = logging.getLogger(__name__)

_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "config.yaml")
with open(_CONFIG_PATH) as _f:
    _cfg = yaml.safe_load(_f)

_MR_CFG = _cfg.get("microrange", {})


async def run_microrange_loop(symbols: list[str], cache) -> None:
    """Entry point — runs forever, called as an asyncio task from main.py."""
    interval      = float(_MR_CFG.get("check_interval_secs", 30))
    max_positions = int(_MR_CFG.get("max_positions", 4))

    log.info(
        "MicroRange loop started — interval=%.0fs  symbols=%s  max_positions=%d",
        interval, symbols, max_positions,
    )

    while True:
        try:
            await _tick(symbols, cache, max_positions)
        except Exception:
            log.exception("MicroRange tick error")
        await asyncio.sleep(interval)


def _regime_allows(symbol: str, direction: str, cache) -> bool:
    """Block trades that contradict the macro regime direction.

    In a TREND regime, also checks the 4H directional bias (+DI vs -DI).
    A bearish TREND will stop-out LONG micro-range entries every time the
    range floor breaks — the most common cause of micro-range losses.
    """
    from core.regime_detector import detect_regime, get_trend_bias
    r     = str(detect_regime(symbol, cache))
    bias  = get_trend_bias(symbol, cache)   # "LONG" | "SHORT" | "NEUTRAL"

    # Hard blocks: regime and direction fundamentally opposed
    if r == "CRASH" and direction == "LONG":  return False
    if r == "PUMP"  and direction == "SHORT": return False

    # In a trending market, only trade WITH the 4H trend direction.
    # NEUTRAL bias = ranging-within-trend → allow both sides.
    if r == "TREND":
        if direction == "LONG"  and bias == "SHORT": return False
        if direction == "SHORT" and bias == "LONG":  return False

    return True


async def _tick(symbols: list[str], cache, max_positions: int) -> None:
    from core.microrange_scorer import score as mr_score, set_cooldown
    from core.executor import execute_signal
    from logging_.logger import TradeLogger

    logger  = TradeLogger()
    open_count = 0   # approximate: we track fired this tick to cap max_positions

    for symbol in symbols:
        if open_count >= max_positions:
            break

        score_dicts = await mr_score(symbol, cache)

        for score_dict in score_dicts:
            # Log every evaluation
            try:
                asyncio.create_task(logger.log_signal(score_dict))
            except Exception as exc:
                log.debug("MicroRange log_signal failed for %s: %s", symbol, exc)

            if not score_dict.get("fire"):
                log.debug(
                    "MicroRange skip %s %s  score=%.2f  width=%.3f%%  signals=%s",
                    score_dict["direction"], symbol,
                    score_dict["score"],
                    score_dict.get("range_width_pct", 0),
                    {k: v for k, v in score_dict["signals"].items() if not v},
                )
                continue

            if open_count >= max_positions:
                break

            if not _regime_allows(symbol, score_dict["direction"], cache):
                log.debug("MicroRange skip %s %s — blocked by regime direction",
                          score_dict["direction"], symbol)
                set_cooldown(symbol)
                continue

            log.info(
                "MicroRange FIRE %s %s  score=%.2f  width=%.3f%%  sl=%.6f  tp=%.6f",
                score_dict["direction"], symbol,
                score_dict["score"],
                score_dict.get("range_width_pct", 0),
                score_dict.get("mr_stop", 0),
                score_dict.get("mr_tp", 0),
            )

            order = await execute_signal(score_dict, cache)
            set_cooldown(symbol)   # always cool down after a fire attempt
            if order:
                open_count += 1
                log.info(
                    "MicroRange order placed: %s %s  entry=%.6f  sl=%.6f  tp=%.6f  qty=%.4f",
                    score_dict["direction"], symbol,
                    order.get("entry", 0), order.get("stop", 0),
                    order.get("take_profit", 0), order.get("qty", 0),
                )

    if open_count:
        log.info("MicroRange tick complete — %d position(s) entered", open_count)
