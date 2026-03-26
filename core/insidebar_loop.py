"""1H inside bar flip loop — runs every check_interval_secs."""
import asyncio
import logging
import os
import yaml

log = logging.getLogger(__name__)

_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "config.yaml")
with open(_CONFIG_PATH) as _f:
    _cfg = yaml.safe_load(_f)

_IB_CFG = _cfg.get("insidebar", {})


async def run_insidebar_loop(symbols: list[str], cache) -> None:
    """Entry point — runs forever, called as an asyncio task from main.py."""
    interval      = float(_IB_CFG.get("check_interval_secs", 60))
    max_positions = int(_IB_CFG.get("max_positions", 3))

    log.info(
        "InsideBar loop started — interval=%.0fs  symbols=%s  max_positions=%d",
        interval, symbols, max_positions,
    )

    while True:
        try:
            await _tick(symbols, cache, max_positions)
        except Exception:
            log.exception("InsideBar tick error")
        await asyncio.sleep(interval)


def _regime_allows(symbol: str, direction: str, cache) -> bool:
    """Block trades that contradict the current macro regime direction.

    TREND symbols: only LONG insidebar entries allowed (don't short a bull trend).
    CRASH symbols: only SHORT allowed.
    RANGE / other: both directions OK.
    """
    from core.regime_detector import detect_regime, Regime
    regime = detect_regime(symbol, cache)
    r = str(regime)
    if r == "TREND"   and direction == "SHORT": return False
    if r == "CRASH"   and direction == "LONG":  return False
    if r == "PUMP"    and direction == "SHORT": return False
    return True


async def _tick(symbols: list[str], cache, max_positions: int) -> None:
    from core.insidebar_scorer import score as ib_score, set_cooldown
    from core.executor import execute_signal
    from logging_.logger import TradeLogger

    logger     = TradeLogger()
    open_count = 0

    for symbol in symbols:
        if open_count >= max_positions:
            break

        score_dicts = await ib_score(symbol, cache)

        for score_dict in score_dicts:
            try:
                asyncio.create_task(logger.log_signal(score_dict))
            except Exception as exc:
                log.debug("InsideBar log_signal failed for %s: %s", symbol, exc)

            if not score_dict.get("fire"):
                log.debug(
                    "InsideBar skip %s %s  score=%.2f  zone=%.3f%%  bars=%d",
                    score_dict["direction"], symbol,
                    score_dict["score"],
                    score_dict.get("zone_pct", 0),
                    score_dict.get("bar_count", 0),
                )
                continue

            # Regime alignment gate: don't trade against the macro trend
            if not _regime_allows(symbol, score_dict["direction"], cache):
                log.debug(
                    "InsideBar skip %s %s — blocked by regime direction",
                    score_dict["direction"], symbol,
                )
                set_cooldown(symbol)   # prevent refiring this tick's signal every interval
                continue

            if open_count >= max_positions:
                break

            log.info(
                "InsideBar FIRE %s %s  score=%.2f  zone=%.3f%%  bars=%d  sl=%.6f  tp=%.6f",
                score_dict["direction"], symbol,
                score_dict["score"],
                score_dict.get("zone_pct", 0),
                score_dict.get("bar_count", 0),
                score_dict.get("ib_stop", 0),
                score_dict.get("ib_tp", 0),
            )

            order = await execute_signal(score_dict, cache)
            set_cooldown(symbol)   # always cool down after a fire attempt
            if order:
                open_count += 1

    if open_count:
        log.info("InsideBar tick complete — %d position(s) entered", open_count)
