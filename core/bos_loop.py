"""BOS/CHoCH strategy loop — runs independently every check_interval_secs.

Sequence each tick
------------------
1. For each symbol, check regime — skip PUMP and CRASH (structure breaks in these
   regimes are unreliable: PUMP breaks mean re-entry into a parabola, CRASH breaks
   mean shorting into a falling knife rather than a clean structure break).
2. Score via bos_scorer.score().
3. Log every evaluation to the signals DB.
4. If fire=True and max_positions not exceeded: execute via executor.execute_signal().
5. Set per-symbol cooldown after each fire attempt.

Check interval: 60s (1H bar alignment).
Max simultaneous BOS positions: 2 (low-frequency, high-quality signal).
"""
import asyncio
import logging
import os
import yaml

log = logging.getLogger(__name__)

_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "config.yaml")
with open(_CONFIG_PATH) as _f:
    _cfg = yaml.safe_load(_f)

_BOS_CFG = _cfg.get("bos", {})

# Regimes where BOS/CHoCH entries are unreliable — skip scoring entirely
_SKIP_REGIMES = frozenset({"PUMP", "CRASH"})


async def run_bos_loop(symbols: list[str], cache) -> None:
    """Entry point — runs forever, called as an asyncio task from main.py."""
    interval      = float(_BOS_CFG.get("check_interval_secs", 60))
    max_positions = int(_BOS_CFG.get("max_positions", 2))

    log.info(
        "BOS/CHoCH loop started — interval=%.0fs  symbols=%s  max_positions=%d",
        interval, symbols, max_positions,
    )

    while True:
        try:
            await _tick(symbols, cache, max_positions)
        except Exception:
            log.exception("BOS tick error")
        await asyncio.sleep(interval)


async def _tick(symbols: list[str], cache, max_positions: int) -> None:
    from core.bos_scorer import score as bos_score, set_cooldown
    from core.regime_detector import detect_regime
    from core.executor import execute_signal
    from logging_.logger import TradeLogger

    logger     = TradeLogger()
    open_count = 0

    for symbol in symbols:
        if open_count >= max_positions:
            break

        # Regime pre-filter: skip symbols in PUMP or CRASH regimes
        try:
            regime_str = str(detect_regime(symbol, cache))
        except Exception:
            regime_str = ""

        if regime_str in _SKIP_REGIMES:
            log.debug("BOS skip %s — regime %s not suitable for BOS entries", symbol, regime_str)
            continue

        score_dicts = await bos_score(symbol, cache)

        for score_dict in score_dicts:
            try:
                asyncio.create_task(logger.log_signal(score_dict))
            except Exception as exc:
                log.debug("BOS log_signal failed for %s: %s", symbol, exc)

            if not score_dict.get("fire"):
                log.debug(
                    "BOS skip %s %s  score=%.2f  break=%.6f  signals=%s",
                    score_dict["direction"], symbol,
                    score_dict["score"],
                    score_dict.get("break_level", 0),
                    {k: v for k, v in score_dict["signals"].items() if not v},
                )
                continue

            if open_count >= max_positions:
                break

            log.info(
                "BOS FIRE %s %s  score=%.2f  break=%.6f  sl=%.6f  tp=%.6f",
                score_dict["direction"], symbol,
                score_dict["score"],
                score_dict.get("break_level", 0),
                score_dict.get("bos_stop", 0),
                score_dict.get("bos_tp", 0),
            )

            order = await execute_signal(score_dict, cache)
            set_cooldown(symbol)   # always cool down after a fire attempt
            if order:
                open_count += 1
                log.info(
                    "BOS order placed: %s %s  entry=%.6f  sl=%.6f  tp=%.6f  qty=%.4f",
                    score_dict["direction"], symbol,
                    order.get("entry", 0), order.get("stop", 0),
                    order.get("take_profit", 0), order.get("qty", 0),
                )

    if open_count:
        log.info("BOS tick complete — %d position(s) entered", open_count)
