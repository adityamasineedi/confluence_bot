"""Health check script — verifies bot components are alive. Run by systemd or cron."""
import asyncio
import aiohttp
import sys
import os

_METRICS_URL = os.environ.get("METRICS_URL", "http://localhost:8000/health")
_BINANCE_WS_CHECK = True   # TODO: ping Binance WS connection status
_DB_PATH = os.environ.get("DB_PATH", "confluence_bot.db")


async def check_metrics_api() -> bool:
    """Verify the FastAPI metrics server is responding.

    TODO: GET /health and check {"status": "ok"}
    """
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(_METRICS_URL, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                data = await resp.json()
                return data.get("status") == "ok"
    except Exception as e:
        print(f"[FAIL] metrics API unreachable: {e}", file=sys.stderr)
        return False


def check_db() -> bool:
    """Verify SQLite DB is accessible and has recent signal rows.

    TODO: check last signal row is within last N minutes
    """
    import sqlite3
    try:
        with sqlite3.connect(_DB_PATH) as conn:
            row = conn.execute("SELECT ts FROM signals ORDER BY ts DESC LIMIT 1").fetchone()
            if row:
                print(f"[OK] last signal: {row[0]}")
            else:
                print("[WARN] no signals in DB yet")
        return True
    except Exception as e:
        print(f"[FAIL] DB check failed: {e}", file=sys.stderr)
        return False


async def run_checks() -> int:
    """Run all health checks. Returns exit code 0 (ok) or 1 (failure)."""
    results = {
        "metrics_api": await check_metrics_api(),
        "db": check_db(),
        # TODO: "ws_connected": check_ws_connection(),
    }
    all_ok = all(results.values())
    for name, ok in results.items():
        status = "OK  " if ok else "FAIL"
        print(f"[{status}] {name}")
    return 0 if all_ok else 1


if __name__ == "__main__":
    exit_code = asyncio.run(run_checks())
    sys.exit(exit_code)
