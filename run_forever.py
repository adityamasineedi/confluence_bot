"""Run the bot forever — auto-restart on crash.

Usage:
    python run_forever.py

Keeps main.py running 24/7. If it crashes, waits 30s and restarts.
Logs restarts to logs/restarts.log.
Press Ctrl+C twice to fully stop.
"""
import subprocess
import sys
import time
import os
from datetime import datetime, timezone

LOG_DIR = os.path.join(os.path.dirname(__file__), "logs")
os.makedirs(LOG_DIR, exist_ok=True)
RESTART_LOG = os.path.join(LOG_DIR, "restarts.log")
MAIN_SCRIPT = os.path.join(os.path.dirname(__file__), "main.py")
PYTHON = sys.executable
RESTART_DELAY = 30  # seconds between crash and restart
MAX_FAST_RESTARTS = 5  # if crashes 5 times in 10 min, wait longer
FAST_WINDOW = 600  # 10 minutes


def log_restart(reason: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    line = f"[{ts}] {reason}\n"
    print(line.strip())
    with open(RESTART_LOG, "a") as f:
        f.write(line)


def main():
    restart_times: list[float] = []

    log_restart("Bot supervisor started — will auto-restart on crash")

    while True:
        log_restart(f"Starting main.py (pid will follow)...")

        try:
            proc = subprocess.Popen(
                [PYTHON, MAIN_SCRIPT],
                cwd=os.path.dirname(__file__),
            )
            log_restart(f"main.py started with PID {proc.pid}")
            exit_code = proc.wait()
            log_restart(f"main.py exited with code {exit_code}")

        except KeyboardInterrupt:
            log_restart("Ctrl+C received — stopping bot")
            try:
                proc.terminate()
                proc.wait(timeout=10)
            except Exception:
                proc.kill()
            break

        except Exception as exc:
            log_restart(f"Supervisor error: {exc}")

        # Track fast restarts
        now = time.time()
        restart_times.append(now)
        restart_times = [t for t in restart_times if now - t < FAST_WINDOW]

        if len(restart_times) >= MAX_FAST_RESTARTS:
            delay = 300  # 5 min cooldown if crashing too fast
            log_restart(
                f"WARNING: {len(restart_times)} crashes in {FAST_WINDOW}s — "
                f"waiting {delay}s before restart"
            )
        else:
            delay = RESTART_DELAY

        log_restart(f"Restarting in {delay}s...")
        try:
            time.sleep(delay)
        except KeyboardInterrupt:
            log_restart("Ctrl+C during restart delay — stopping")
            break

    log_restart("Bot supervisor stopped")


if __name__ == "__main__":
    main()
