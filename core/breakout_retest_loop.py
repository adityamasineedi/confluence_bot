"""core/breakout_retest_loop.py — Breakout retest strategy loop."""
import asyncio
import logging
import os
import yaml

log = logging.getLogger(__name__)
_cfg = yaml.safe_load(open(os.path.join(os.path.dirname(__file__), "..", "config.yaml")))


async def run_breakout_retest_loop(symbols: list[str], cache) -> None:
    from core.breakout_retest_scorer import score
    from core.executor import execute_signal
    from core.strategy_router import get_active_strategies
    from core.regime_detector import detect_regime
    from logging_.logger import TradeLogger

    interval  = float(_cfg.get("breakout_retest", {}).get("check_interval_secs", 30))
    max_pos   = int(  _cfg.get("breakout_retest", {}).get("max_positions", 3))
    logger    = TradeLogger()

    log.info("Breakout retest loop started — interval=%.0fs symbols=%s",
             interval, symbols)

    while True:
        await asyncio.sleep(interval)
        open_count = 0

        for symbol in symbols:
            try:
                regime = str(detect_regime(symbol, cache))
                active = get_active_strategies(symbol, regime)
                if "breakout_retest" not in active:
                    continue

                score_dicts = await score(symbol, cache)

                for sd in score_dicts:
                    # Log every evaluation — fire or not — for dashboard + debugging
                    try:
                        asyncio.create_task(logger.log_signal(sd))
                    except Exception as exc:
                        log.debug("BR log_signal failed %s: %s", symbol, exc)

                    if not sd.get("fire"):
                        log.debug(
                            "BR skip %s %s  score=%.2f  state=%s",
                            sd.get("direction", "?"), symbol,
                            sd.get("score", 0),
                            sd.get("signals", {})
                        )
                        continue

                    if open_count >= max_pos:
                        break

                    log.info(
                        "BR FIRE %s %s  score=%.2f  "
                        "flip=%.4f  sl=%.4f  tp=%.4f",
                        sd["direction"], symbol, sd["score"],
                        sd.get("signals", {}).get("flip_level", 0),
                        sd.get("br_stop", 0),
                        sd.get("br_tp",   0),
                    )

                    order = await execute_signal(sd, cache)
                    if order:
                        open_count += 1
                        log.info(
                            "BR order placed %s %s  "
                            "entry=%.4f  sl=%.4f  tp=%.4f  qty=%.4f",
                            sd["direction"], symbol,
                            order.get("entry", 0),
                            order.get("stop",  0),
                            order.get("take_profit", 0),
                            order.get("qty",   0),
                        )

            except Exception as exc:
                log.warning("BR loop error %s: %s", symbol, exc)

        if open_count:
            log.info("BR tick complete — %d position(s) entered", open_count)
