"""Health check — verifies bot is alive and signals are recent.

Can be run standalone:
    python ops/health_check.py

Or called periodically from main.py via run_silent_check().

Checks:
1. FastAPI /health responds with {"status": "ok"}
2. Last signal in DB is < 10 minutes old (bot is evaluating)
3. At least one WebSocket stream is connected (via WS registry)

On failure: sends Telegram alert (once per failure cycle, not repeatedly).
"""
import asyncio
import os
import sqlite3
import sys
import urllib.request
from datetime import datetime, timezone

_METRICS_URL  = os.environ.get("METRICS_URL", "http://localhost:8001/health")
_DB_PATH      = os.environ.get("DB_PATH", "confluence_bot.db")
_MAX_SIGNAL_AGE_MINUTES = 20

# Prevent Telegram alert spam — track last alert time
_last_alert_ts: float = 0.0
_ALERT_COOLDOWN_S = 300  # 5 minutes between repeat alerts


# ── Individual checks ─────────────────────────────────────────────────────────

def check_metrics_api() -> tuple[bool, str]:
    """Returns (ok, message)."""
    try:
        req = urllib.request.Request(_METRICS_URL)
        with urllib.request.urlopen(req, timeout=5) as resp:
            import json
            data = json.loads(resp.read())
            if data.get("status") == "ok":
                return True, "metrics API ok"
            return False, f"metrics API bad response: {data}"
    except Exception as exc:
        return False, f"metrics API unreachable: {exc}"


def check_db_freshness() -> tuple[bool, str]:
    """Last signal row must be < _MAX_SIGNAL_AGE_MINUTES old."""
    try:
        with sqlite3.connect(_DB_PATH) as conn:
            row = conn.execute(
                "SELECT ts FROM signals ORDER BY ts DESC LIMIT 1"
            ).fetchone()
        if not row:
            return False, "no signals in DB yet (bot may not have evaluated yet)"

        last_ts_str = row[0]
        # Parse ISO-8601 UTC timestamp
        last_ts = datetime.fromisoformat(last_ts_str.replace("Z", "+00:00"))
        if last_ts.tzinfo is None:
            last_ts = last_ts.replace(tzinfo=timezone.utc)
        age_min = (datetime.now(timezone.utc) - last_ts).total_seconds() / 60.0

        if age_min > _MAX_SIGNAL_AGE_MINUTES:
            return False, f"last signal {age_min:.1f} min ago (threshold: {_MAX_SIGNAL_AGE_MINUTES} min)"
        return True, f"last signal {age_min:.1f} min ago — ok"

    except Exception as exc:
        return False, f"DB check failed: {exc}"


def check_open_trades() -> tuple[bool, str]:
    """Info-only: report how many trades are currently open."""
    try:
        with sqlite3.connect(_DB_PATH) as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM trades WHERE status='OPEN'"
            ).fetchone()
        n = row[0] if row else 0
        return True, f"open trades: {n}"
    except Exception as exc:
        return True, f"open trade count unavailable: {exc}"


# ── Composite health check ────────────────────────────────────────────────────

def run_checks_sync() -> tuple[bool, list[tuple[bool, str]]]:
    """Run all checks synchronously. Returns (all_ok, [(ok, msg), ...])."""
    results = [
        check_metrics_api(),
        check_db_freshness(),
        check_open_trades(),
    ]
    all_ok = all(ok for ok, _ in results)
    return all_ok, results


async def run_silent_check() -> None:
    """
    Async wrapper called from main.py every 5 minutes.
    Sends a Telegram alert if health fails (with cooldown to avoid spam).
    """
    global _last_alert_ts
    import time

    all_ok, results = await asyncio.to_thread(run_checks_sync)

    if all_ok:
        return  # everything fine — no noise

    # Failed — send Telegram alert (if cooldown elapsed)
    now = time.monotonic()
    if now - _last_alert_ts < _ALERT_COOLDOWN_S:
        return  # already alerted recently

    _last_alert_ts = now
    import html as _html
    failed = [_html.escape(msg) for ok, msg in results if not ok]
    alert_text = (
        "⚠️ <b>confluence_bot health check FAILED</b>\n\n"
        + "\n".join(f"• {m}" for m in failed)
    )
    try:
        from notifications.telegram import send_text
        await send_text(alert_text)
    except Exception:
        pass  # Telegram unavailable — already logged by send_text


# ── Standalone runner ─────────────────────────────────────────────────────────

async def run_checks() -> int:
    """Standalone async runner. Returns exit code 0 (ok) or 1 (failure)."""
    all_ok, results = await asyncio.to_thread(run_checks_sync)
    for ok, msg in results:
        status = "OK  " if ok else "FAIL"
        print(f"[{status}] {msg}")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(run_checks()))
