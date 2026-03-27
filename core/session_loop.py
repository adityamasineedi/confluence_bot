"""Session open trap loop — fires exactly at session open + 15 min.

Sequence each session
---------------------
1. At T+15 min after Asia / London / NY session open, wake up.
2. Score all symbols for a session trap setup.
3. Execute up to max_entries per session.
4. Sleep until the next session window.

The loop uses asyncio.sleep to wait precisely for each session rather than
polling every N seconds, to avoid processing the signal at the wrong bar.
"""
import asyncio
import logging
import os
from datetime import datetime, timezone, timedelta
import yaml

log = logging.getLogger(__name__)

_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "config.yaml")
with open(_CONFIG_PATH) as _f:
    _cfg = yaml.safe_load(_f)

_SS_CFG      = _cfg.get("session_trap", {})
_MAX_ENTRIES = int(_SS_CFG.get("max_entries_per_session", 3))
_ENABLED_SESSIONS = _SS_CFG.get("sessions", [1, 8, 13])   # UTC hours


def _next_session_fire_ts() -> tuple[float, int]:
    """Return (unix_seconds_of_next_fire, session_hour).

    Fire time = session_open + 15 min (after the 3-bar window closes).
    """
    now = datetime.now(tz=timezone.utc)
    candidates = []
    for h in _ENABLED_SESSIONS:
        # Today's session + 15 min
        fire_today = now.replace(hour=h, minute=15, second=0, microsecond=0)
        if fire_today <= now:
            fire_today += timedelta(days=1)
        candidates.append((fire_today.timestamp(), h))
    candidates.sort()
    return candidates[0]


async def run_session_loop(symbols: list[str], cache) -> None:
    """Entry point — runs forever, called as an asyncio task from main.py."""
    log.info(
        "SessionTrap loop started — sessions=%s  max_entries=%d",
        _ENABLED_SESSIONS, _MAX_ENTRIES,
    )

    while True:
        fire_ts, session_hour = _next_session_fire_ts()
        sleep_secs = max(0.0, fire_ts - datetime.now(tz=timezone.utc).timestamp())

        log.info(
            "SessionTrap: next fire in %.0f min for session %d:00 UTC",
            sleep_secs / 60, session_hour,
        )
        await asyncio.sleep(sleep_secs)

        # Lateness guard: if the event loop was blocked or the bot was restarting,
        # we may wake up well past T+15 min — the session data is stale and any
        # entry would be at the wrong bar. Skip and warn rather than fire blind.
        actual_late_secs = datetime.now(tz=timezone.utc).timestamp() - fire_ts
        _LATE_TOLERANCE_SECS = 5 * 60   # 5 minutes
        if actual_late_secs > _LATE_TOLERANCE_SECS:
            log.warning(
                "SessionTrap window MISSED for session %d:00 UTC — woke %.0f min late "
                "(event loop delay or restart). Skipping this session.",
                session_hour, actual_late_secs / 60,
            )
            await asyncio.sleep(30)
            continue

        try:
            await _tick(symbols, cache, session_hour)
        except Exception:
            log.exception("SessionTrap tick error (session=%d)", session_hour)

        # Small buffer to avoid re-firing the same session
        await asyncio.sleep(30)


async def _tick(symbols: list[str], cache, session_hour: int) -> None:
    from core.session_scorer import score as ss_score, set_cooldown
    from core.executor import execute_signal
    from logging_.logger import TradeLogger

    logger  = TradeLogger()
    fired   = 0

    for symbol in symbols:
        if fired >= _MAX_ENTRIES:
            break

        score_dict = await ss_score(symbol, cache, session_hour)
        if score_dict is None:
            continue

        try:
            asyncio.create_task(logger.log_signal(score_dict))
        except Exception as exc:
            log.debug("SessionTrap log_signal failed for %s: %s", symbol, exc)

        if not score_dict.get("fire"):
            log.debug(
                "SessionTrap skip %s %s  score=%.2f  fake=%.3f%%  signals=%s",
                score_dict["direction"], symbol,
                score_dict["score"],
                score_dict.get("fake_move", 0),
                {k: v for k, v in score_dict["signals"].items() if not v},
            )
            continue

        log.info(
            "SessionTrap FIRE %s %s  session=%d  score=%.2f  fake=%.3f%%  sl=%.6f  tp=%.6f",
            score_dict["direction"], symbol, session_hour,
            score_dict["score"],
            score_dict.get("fake_move", 0),
            score_dict.get("ss_stop", 0),
            score_dict.get("ss_tp", 0),
        )

        order = await execute_signal(score_dict, cache)
        if order:
            set_cooldown(symbol, session_hour)
            fired += 1

    if fired:
        log.info("SessionTrap session=%d complete — %d position(s) entered", session_hour, fired)
