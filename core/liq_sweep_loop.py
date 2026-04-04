"""core/liq_sweep_loop.py — Liquidity sweep loop."""
import asyncio
import logging
import yaml
import os

log = logging.getLogger(__name__)
_cfg = yaml.safe_load(open(os.path.join(os.path.dirname(__file__), "..", "config.yaml")))


async def run_liq_sweep_loop(symbols: list[str], cache) -> None:
    from core.liq_sweep_scorer import score
    from core.executor import execute_signal
    from core.strategy_router import get_active_strategies
    from core.regime_detector import detect_regime

    interval = float(_cfg.get("liq_sweep", {}).get("check_interval_secs", 60))
    max_pos   = int(_cfg.get("liq_sweep", {}).get("max_positions", 2))
    log.info("Liq sweep loop started — interval=%.0fs symbols=%s", interval, symbols)

    while True:
        await asyncio.sleep(interval)
        open_count = 0

        for symbol in symbols:
            try:
                regime = str(detect_regime(symbol, cache))
                active = get_active_strategies(symbol, regime)
                if "liq_sweep" not in active and "liq_sweep_short" not in active:
                    continue

                score_dicts = await score(symbol, cache)
                for sd in score_dicts:
                    if not sd.get("fire"):
                        continue
                    # Respect per-direction routing
                    direction = sd.get("direction", "")
                    if direction == "LONG" and "liq_sweep" not in active:
                        continue
                    if direction == "SHORT" and "liq_sweep_short" not in active:
                        continue
                    if open_count >= max_pos:
                        break
                    log.info("Liq sweep FIRE %s %s score=%.2f sl=%.6f tp=%.6f",
                             direction, symbol, sd["score"],
                             sd.get("ls_stop", 0), sd.get("ls_tp", 0))
                    order = await execute_signal(sd, cache)
                    if order:
                        open_count += 1
            except Exception as exc:
                log.warning("Liq sweep loop %s: %s", symbol, exc)
