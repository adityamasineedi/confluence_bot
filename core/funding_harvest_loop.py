"""Funding rate harvest loop — wakes up before each settlement window.

Wakes up `entry_mins_before` minutes before each Binance funding settlement
(00:00, 08:00, 16:00 UTC), then polls every `poll_interval_secs` seconds
until `exit_mins_after` after settlement.  Outside of windows it sleeps.
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

_FH_CFG = _cfg.get("funding_harvest", {})

_SETTLEMENT_HOURS = [0, 8, 16]
_ENTRY_MINS  = int(_FH_CFG.get("entry_mins_before",   30))
_EXIT_MINS   = int(_FH_CFG.get("exit_mins_after",     15))
_POLL_SECS   = float(_FH_CFG.get("poll_interval_secs", 60))
_MAX_ENTRIES = int(_FH_CFG.get("max_entries_per_window", 4))


def _next_window_start() -> float:
    """Unix seconds when the next harvest window opens (= settlement - entry_mins)."""
    now = datetime.now(tz=timezone.utc)
    candidates = []
    for h in _SETTLEMENT_HOURS:
        settle = now.replace(hour=h, minute=0, second=0, microsecond=0)
        window_start = settle - timedelta(minutes=_ENTRY_MINS)
        if window_start <= now:
            settle += timedelta(days=1)
            window_start = settle - timedelta(minutes=_ENTRY_MINS)
        candidates.append(window_start.timestamp())
    return min(candidates)


def _window_end_ts() -> float:
    """Unix seconds when the current (or nearest) harvest window closes."""
    now = datetime.now(tz=timezone.utc)
    for h in _SETTLEMENT_HOURS:
        settle = now.replace(hour=h, minute=0, second=0, microsecond=0)
        window_end = settle + timedelta(minutes=_EXIT_MINS)
        if window_end >= now:
            return window_end.timestamp()
    # Next day's first settlement
    tomorrow_settle = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    return (tomorrow_settle + timedelta(minutes=_EXIT_MINS)).timestamp()


async def run_funding_harvest_loop(symbols: list[str], cache) -> None:
    """Entry point — runs forever, called as an asyncio task from main.py."""
    log.info(
        "FundingHarvest loop started — entry=%dmin before, exit=%dmin after, symbols=%s",
        _ENTRY_MINS, _EXIT_MINS, symbols,
    )

    while True:
        # Sleep until the window opens
        now       = datetime.now(tz=timezone.utc).timestamp()
        sleep_secs = max(0.0, _next_window_start() - now)

        log.info("FundingHarvest: next window in %.0f min", sleep_secs / 60)
        await asyncio.sleep(sleep_secs)

        # Actively poll for the duration of the window
        while datetime.now(tz=timezone.utc).timestamp() < _window_end_ts():
            try:
                await _tick(symbols, cache)
            except Exception:
                log.exception("FundingHarvest tick error")
            await asyncio.sleep(_POLL_SECS)

        # Small buffer before going back to sleep
        await asyncio.sleep(30)


async def _tick(symbols: list[str], cache) -> None:
    from core.funding_harvest_scorer import score as fh_score, set_cooldown
    from core.executor import execute_signal
    from core.regime_detector import detect_regime, get_trend_bias
    from logging_.logger import TradeLogger

    _TREND_FILTER = bool(_FH_CFG.get("trend_filter", True))

    logger = TradeLogger()
    fired  = 0

    for symbol in symbols:
        if fired >= _MAX_ENTRIES:
            break

        score_dict = await fh_score(symbol, cache)
        if score_dict is None:
            continue

        # Trend direction gate: never fade the macro trend for funding
        if _TREND_FILTER:
            direction = score_dict.get("direction")
            regime    = str(detect_regime(symbol, cache))
            if regime == "TREND":
                bias = get_trend_bias(symbol, cache)
                if bias == "LONG" and direction == "SHORT":
                    log.debug("FundingHarvest skip %s SHORT — TREND bias is LONG", symbol)
                    continue
                if bias == "SHORT" and direction == "LONG":
                    log.debug("FundingHarvest skip %s LONG — TREND bias is SHORT", symbol)
                    continue

        try:
            asyncio.create_task(logger.log_signal(score_dict))
        except Exception as exc:
            log.debug("FundingHarvest log_signal failed for %s: %s", symbol, exc)

        if not score_dict.get("fire"):
            log.debug(
                "FundingHarvest skip %s %s  rate=%.4f%%  score=%.2f  signals=%s",
                score_dict["direction"], symbol,
                score_dict.get("funding_rate", 0),
                score_dict["score"],
                {k: v for k, v in score_dict["signals"].items() if not v},
            )
            continue

        log.info(
            "FundingHarvest FIRE %s %s  rate=%.4f%%  score=%.2f  sl=%.6f  tp=%.6f",
            score_dict["direction"], symbol,
            score_dict.get("funding_rate", 0),
            score_dict["score"],
            score_dict.get("fh_stop", 0),
            score_dict.get("fh_tp", 0),
        )

        order = await execute_signal(score_dict, cache)
        if order:
            set_cooldown(symbol)
            fired += 1

    if fired:
        log.info("FundingHarvest tick complete — %d position(s) entered", fired)
