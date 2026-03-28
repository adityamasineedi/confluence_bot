"""15m EMA Pullback loop — trend-continuation scalps at EMA21.

Runs every 30s. Uses 4H trend direction as macro filter.
Fires 2-5× per symbol per day in trending markets.
"""
import asyncio
import logging
import os
import yaml

log = logging.getLogger(__name__)

_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "config.yaml")
with open(_CONFIG_PATH) as _f:
    _cfg = yaml.safe_load(_f)

_EP_CFG = _cfg.get("ema_pullback", {})


async def run_ema_pullback_loop(symbols: list[str], cache) -> None:
    """Entry point — runs forever, called as an asyncio task from main.py."""
    _exclude = [s.upper() for s in _EP_CFG.get("exclude_symbols", [])]
    symbols  = [s for s in symbols if s not in _exclude]
    log.info("EMA Pullback symbols after exclusion: %s", symbols)

    interval      = float(_EP_CFG.get("check_interval_secs", 30))
    max_positions = int(_EP_CFG.get("max_positions", 3))

    log.info(
        "EMA Pullback loop started — interval=%.0fs  symbols=%s  max_positions=%d",
        interval, symbols, max_positions,
    )

    while True:
        try:
            await _tick(symbols, cache, max_positions)
        except Exception:
            log.exception("EMA Pullback tick error")
        await asyncio.sleep(interval)


async def _tick(symbols: list[str], cache, max_positions: int) -> None:
    from core.ema_pullback_scorer import score as ep_score, set_cooldown
    from core.executor import execute_signal
    from logging_.logger import TradeLogger

    logger     = TradeLogger()
    open_count = 0

    for symbol in symbols:
        if open_count >= max_positions:
            break

        score_dicts = await ep_score(symbol, cache)

        for score_dict in score_dicts:
            try:
                asyncio.create_task(logger.log_signal(score_dict))
            except Exception as exc:
                log.debug("EMA Pullback log_signal failed %s: %s", symbol, exc)

            if not score_dict.get("fire"):
                log.debug(
                    "EMA Pullback skip %s %s  score=%.2f  signals=%s",
                    score_dict["direction"], symbol,
                    score_dict["score"],
                    {k: v for k, v in score_dict["signals"].items() if not v},
                )
                continue

            if open_count >= max_positions:
                break

            log.info(
                "EMA Pullback FIRE %s %s  score=%.2f  sl=%.6f  tp=%.6f",
                score_dict["direction"], symbol,
                score_dict["score"],
                score_dict.get("ep_stop", 0),
                score_dict.get("ep_tp", 0),
            )

            order = await execute_signal(score_dict, cache)
            set_cooldown(symbol)
            if order:
                open_count += 1
                log.info(
                    "EMA Pullback order placed: %s %s  entry=%.6f  sl=%.6f  tp=%.6f  qty=%.4f",
                    score_dict["direction"], symbol,
                    order.get("entry", 0), order.get("stop", 0),
                    order.get("take_profit", 0), order.get("qty", 0),
                )

    if open_count:
        log.info("EMA Pullback tick complete — %d position(s) entered", open_count)
