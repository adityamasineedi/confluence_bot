"""VWAP Band Reversion strategy loop — runs independently every check_interval_secs.

Sequence each tick
------------------
1. For each symbol, check regime — skip PUMP and CRASH entirely.
   · In PUMP: upper bands keep expanding upward; shorting = fading momentum.
   · In CRASH: lower bands keep expanding downward; longing = catching falling knives.
2. Score via vwap_band_scorer.score().
3. Log every evaluation to the signals DB.
4. If fire=True and max_positions not exceeded: execute via executor.execute_signal().
5. Set per-symbol cooldown after each fire attempt.

Check interval: 30s (15m bars close every 15 min; 30s catches the rejection close
quickly without over-polling).
Max simultaneous VWAP Band positions: 3.
"""
import asyncio
import logging
import os
import yaml

log = logging.getLogger(__name__)

_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "config.yaml")
with open(_CONFIG_PATH) as _f:
    _cfg = yaml.safe_load(_f)

_VB_CFG = _cfg.get("vwap_band", {})

_SKIP_REGIMES = frozenset({"PUMP", "CRASH"})


async def run_vwap_band_loop(symbols: list[str], cache) -> None:
    """Entry point — runs forever, called as an asyncio task from main.py."""
    interval      = float(_VB_CFG.get("check_interval_secs", 30))
    max_positions = int(_VB_CFG.get("max_positions", 3))

    log.info(
        "VWAP Band loop started — interval=%.0fs  symbols=%s  max_positions=%d",
        interval, symbols, max_positions,
    )

    while True:
        try:
            await _tick(symbols, cache, max_positions)
        except Exception:
            log.exception("VWAP Band tick error")
        await asyncio.sleep(interval)


async def _tick(symbols: list[str], cache, max_positions: int) -> None:
    from core.vwap_band_scorer import score as vb_score, set_cooldown
    from core.regime_detector import detect_regime
    from core.strategy_router import get_active_strategies
    from core.executor import execute_signal
    from logging_.logger import TradeLogger

    logger     = TradeLogger()
    open_count = 0

    for symbol in symbols:
        if open_count >= max_positions:
            break

        # Regime pre-filter: PUMP/CRASH make band reversion unreliable
        try:
            regime_str = str(detect_regime(symbol, cache))
        except Exception:
            regime_str = ""

        if regime_str in _SKIP_REGIMES:
            log.debug(
                "VWAP Band skip %s — regime %s unsuitable for mean reversion",
                symbol, regime_str,
            )
            continue

        active = get_active_strategies(symbol, regime_str)
        if "vwap_band" not in active:
            log.debug("vwap_band skipped for %s in %s regime — not in routing", symbol, regime_str)
            continue

        score_dicts = await vb_score(symbol, cache)

        for score_dict in score_dicts:
            try:
                asyncio.create_task(logger.log_signal(score_dict))
            except Exception as exc:
                log.debug("VWAP Band log_signal failed for %s: %s", symbol, exc)

            if not score_dict.get("fire"):
                log.debug(
                    "VWAP Band skip %s %s  score=%.2f  signals=%s",
                    score_dict["direction"], symbol,
                    score_dict["score"],
                    {k: v for k, v in score_dict["signals"].items() if not v},
                )
                continue

            if open_count >= max_positions:
                break

            log.info(
                "VWAP Band FIRE %s %s  score=%.2f  sl=%.6f  tp=%.6f",
                score_dict["direction"], symbol,
                score_dict["score"],
                score_dict.get("vb_stop", 0),
                score_dict.get("vb_tp", 0),
            )

            order = await execute_signal(score_dict, cache)
            set_cooldown(symbol)   # always cool down after a fire attempt
            if order:
                open_count += 1
                log.info(
                    "VWAP Band order placed: %s %s  entry=%.6f  sl=%.6f  tp=%.6f  qty=%.4f",
                    score_dict["direction"], symbol,
                    order.get("entry", 0), order.get("stop", 0),
                    order.get("take_profit", 0), order.get("qty", 0),
                )

    if open_count:
        log.info("VWAP Band tick complete — %d position(s) entered", open_count)
