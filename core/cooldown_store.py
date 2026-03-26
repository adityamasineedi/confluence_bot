"""Persistent cooldown store — survives bot restarts.

Uses the `cooldowns` SQLite table (wall-clock expiry timestamps).
Strategies call set() after a trade and check() before entering.

Usage:
    from core.cooldown_store import CooldownStore
    cd = CooldownStore("MICRORANGE")
    if cd.is_active("BTCUSDT"): ...
    cd.set("BTCUSDT", cooldown_secs=1200)
"""
import logging
import os
import sqlite3
import time

log = logging.getLogger(__name__)

_DB_PATH = os.environ.get("DB_PATH", "confluence_bot.db")


class CooldownStore:
    def __init__(self, strategy: str) -> None:
        self._strategy = strategy.upper()
        self._cache: dict[str, float] = {}   # in-memory fast path

    def is_active(self, symbol: str) -> bool:
        """Return True if symbol is still on cooldown."""
        now = time.time()
        # Fast path: check in-memory cache first
        expires = self._cache.get(symbol, 0.0)
        if expires > now:
            return True
        # Slow path: check DB (handles restarts)
        db_expires = self._load(symbol)
        if db_expires > now:
            self._cache[symbol] = db_expires
            return True
        # Expired — clear cache entry
        self._cache.pop(symbol, None)
        return False

    def set(self, symbol: str, cooldown_secs: float) -> None:
        """Set cooldown for symbol, persisted to DB."""
        expires = time.time() + cooldown_secs
        self._cache[symbol] = expires
        try:
            with sqlite3.connect(_DB_PATH) as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO cooldowns(symbol, strategy, expires_ts) "
                    "VALUES (?, ?, ?)",
                    (symbol.upper(), self._strategy, expires)
                )
        except Exception as exc:
            log.debug("CooldownStore.set(%s, %s) DB write failed: %s",
                      symbol, self._strategy, exc)

    def remaining(self, symbol: str) -> float:
        """Seconds remaining on cooldown (0.0 if not active)."""
        now = time.time()
        expires = max(self._cache.get(symbol, 0.0), self._load(symbol))
        return max(0.0, expires - now)

    def _load(self, symbol: str) -> float:
        try:
            with sqlite3.connect(_DB_PATH) as conn:
                row = conn.execute(
                    "SELECT expires_ts FROM cooldowns WHERE symbol=? AND strategy=?",
                    (symbol.upper(), self._strategy)
                ).fetchone()
            return float(row[0]) if row else 0.0
        except Exception:
            return 0.0
