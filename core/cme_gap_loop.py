"""core/cme_gap_loop.py — CME Gap fill strategy loop.

Active only Sunday 23:00 UTC → Wednesday 23:00 UTC (72h gap fill window).
Outside this window, sleeps without scoring.
"""
import asyncio
import logging
import os
import yaml
from datetime import datetime, timezone

log = logging.getLogger(__name__)

_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "config.yaml")
with open(_CONFIG_PATH) as _f:
    _cfg = yaml.safe_load(_f)

_CME_CFG  = _cfg.get("cme_gap", {})
_INTERVAL = float(_CME_CFG.get("check_interval_secs", 300))
_MAX_POS  = int(_CME_CFG.get("max_positions", 1))


def _in_gap_window() -> bool:
    """Return True if current UTC time is within the 72h CME gap fill window.

    Window: Sunday 23:00 UTC → Wednesday 23:00 UTC
    weekday(): Monday=0 ... Sunday=6
    """
    now = datetime.now(timezone.utc)
    wd  = now.weekday()
    hr  = now.hour

    # Sunday (6) from 23:00 onward
    if wd == 6 and hr >= 23:
        return True
    # Monday (0), Tuesday (1) — all day
    if wd in (0, 1):
        return True
    # Wednesday (2) until 23:00
    if wd == 2 and hr < 23:
        return True

    return False


async def run_cme_gap_loop(symbols: list[str], cache) -> None:
    """Run CME gap scorer on a 5-minute loop during the fill window."""
    from core.cme_gap_scorer import score
    from core.executor import execute_signal

    log.info("CME Gap loop started — interval=%.0fs symbols=%s", _INTERVAL, symbols)

    while True:
        await asyncio.sleep(_INTERVAL)

        if not _in_gap_window():
            log.debug("CME Gap loop — outside fill window, sleeping")
            continue

        for symbol in symbols:
            try:
                score_dicts = await score(symbol, cache)
                for sd in score_dicts:
                    log.info("CME Gap eval %s: gap=%.3f%% dir=%s score=%.2f fire=%s",
                             symbol,
                             sd["signals"].get("gap_pct", 0) * 100,
                             sd["direction"],
                             sd["score"],
                             sd["fire"])
                    if not sd.get("fire"):
                        continue
                    order = await execute_signal(sd, cache)
                    if order:
                        log.info("CME Gap order placed: %s %s sl=%.2f tp=%.2f",
                                 sd["direction"], symbol,
                                 sd.get("cg_stop", 0), sd.get("cg_tp", 0))
            except Exception as exc:
                log.warning("CME Gap loop %s: %s", symbol, exc)
