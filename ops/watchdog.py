"""confluence_bot watchdog — auto-restarts the bot on crash.

Usage:
    python ops/watchdog.py

Keeps running forever, restarting main.py whenever it exits.
Logs restart events to logs/watchdog.log.
Bot stdout/stderr is appended to logs/bot.log.
Press Ctrl+C to stop both the watchdog and the bot.
"""
import subprocess
import sys
import time
import os
import logging
from datetime import datetime

_BOT_DIR  = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PYTHON   = os.path.join(_BOT_DIR, "venv", "Scripts", "python.exe")
_MAIN     = os.path.join(_BOT_DIR, "main.py")
_LOG_DIR  = os.path.join(_BOT_DIR, "logs")

os.makedirs(_LOG_DIR, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] watchdog: %(message)s",
    handlers=[
        logging.FileHandler(os.path.join(_LOG_DIR, "watchdog.log")),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("watchdog")

_RESTART_DELAY_S  = 5    # seconds before restarting after crash
_CRASH_THRESHOLD  = 30   # seconds — exits faster than this = crash
_MAX_RESTARTS     = 20   # max restarts in a rolling window before giving up
_RESTART_WINDOW_S = 3600 # rolling window in seconds


def run() -> None:
    restart_times: list[float] = []

    while True:
        log.info("Starting confluence_bot  (python=%s)", _PYTHON)
        start_ts = time.monotonic()

        bot_log_path = os.path.join(_LOG_DIR, "bot.log")
        try:
            with open(bot_log_path, "a") as bot_log_fh:
                proc = subprocess.Popen(
                    [_PYTHON, _MAIN],
                    cwd=_BOT_DIR,
                    stdout=bot_log_fh,
                    stderr=bot_log_fh,
                )
                proc.wait()
        except KeyboardInterrupt:
            log.info("Watchdog stopped by user (Ctrl+C).")
            try:
                proc.terminate()
            except Exception:
                pass
            sys.exit(0)

        exit_code = proc.returncode
        elapsed   = time.monotonic() - start_ts

        if exit_code == 0:
            log.info("Bot exited cleanly (code 0). Stopping watchdog.")
            sys.exit(0)

        log.warning("Bot exited with code %s after %.0fs.", exit_code, elapsed)

        # Track restart times within the rolling window
        now = time.monotonic()
        restart_times = [t for t in restart_times if now - t < _RESTART_WINDOW_S]
        restart_times.append(now)

        if len(restart_times) >= _MAX_RESTARTS:
            log.error(
                "Bot crashed %d times in %d minutes. Stopping watchdog to prevent loop.",
                _MAX_RESTARTS, _RESTART_WINDOW_S // 60,
            )
            sys.exit(1)

        log.info("Restarting in %ds  (restart #%d this hour)...", _RESTART_DELAY_S, len(restart_times))
        time.sleep(_RESTART_DELAY_S)


if __name__ == "__main__":
    run()
