"""core/wyckoff_upthrust_loop.py — Wyckoff upthrust SHORT loop."""
import asyncio
import logging
import yaml
import os

log = logging.getLogger(__name__)
_cfg = yaml.safe_load(open(os.path.join(os.path.dirname(__file__), "..", "config.yaml")))


async def run_wyckoff_upthrust_loop(symbols: list[str], cache) -> None:
    from core.wyckoff_upthrust_scorer import score
    from core.executor import execute_signal
    from core.strategy_router import get_active_strategies
    from core.regime_detector import detect_regime

    interval = float(_cfg.get("wyckoff_upthrust", {}).get("check_interval_secs", 60))
    max_pos  = int(_cfg.get("wyckoff_upthrust", {}).get("max_positions", 2))
    log.info("Wyckoff upthrust loop started — interval=%.0fs symbols=%s", interval, symbols)

    while True:
        await asyncio.sleep(interval)
        open_count = 0

        for symbol in symbols:
            try:
                regime = str(detect_regime(symbol, cache))
                if "wyckoff_upthrust_v2" not in get_active_strategies(symbol, regime):
                    continue

                score_dicts = await score(symbol, cache)
                for sd in score_dicts:
                    if not sd.get("fire"):
                        continue
                    if open_count >= max_pos:
                        break
                    log.info("Wyckoff upthrust FIRE %s score=%.2f sl=%.6f tp=%.6f",
                             symbol, sd["score"], sd.get("wu_stop", 0), sd.get("wu_tp", 0))
                    order = await execute_signal(sd, cache)
                    if order:
                        open_count += 1
            except Exception as exc:
                log.warning("Wyckoff upthrust loop %s: %s", symbol, exc)
