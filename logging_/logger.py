"""SQLite trade logger — persists every signal evaluation and trade execution."""
import asyncio
import json
import os
import sqlite3
from datetime import datetime, timezone

_DB_PATH     = os.environ.get("DB_PATH", "confluence_bot.db")
_SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "schema.sql")


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class TradeLogger:
    """Async-safe SQLite logger for signals, trades, and regime events.

    All blocking sqlite3 calls are offloaded to a thread via asyncio.to_thread
    so the event loop is never blocked.
    """

    def __init__(self, db_path: str = _DB_PATH) -> None:
        self.db_path = db_path
        self._init_db()

    # ── Initialisation ────────────────────────────────────────────────────────

    def _init_db(self) -> None:
        """Create tables from schema.sql if they do not exist."""
        with open(_SCHEMA_PATH) as f:
            ddl = f.read()
        with sqlite3.connect(self.db_path) as conn:
            conn.executescript(ddl)

    # ── Public async API ──────────────────────────────────────────────────────

    async def log_signal(self, score_dict: dict) -> None:
        """Persist a scorer result to the signals table."""
        await asyncio.to_thread(self._insert_signal, score_dict)

    async def log_trade(self, score_dict: dict, order: dict) -> None:
        """Persist an executed trade to the trades table."""
        await asyncio.to_thread(self._insert_trade, score_dict, order)

    async def log_regime(self, symbol: str, regime: str) -> None:
        """Persist a regime classification event."""
        await asyncio.to_thread(self._insert_regime, symbol, regime)

    async def load_active_deals(self) -> list[tuple[str, str]]:
        """Return [(symbol, direction)] for all trades with status='OPEN'."""
        return await asyncio.to_thread(self._query_open_deals)

    async def close_deal(
        self, symbol: str, direction: str, exit_price: float, pnl_usdt: float
    ) -> None:
        """Mark all OPEN trades for (symbol, direction) as FILLED."""
        await asyncio.to_thread(self._update_closed, symbol, direction, exit_price, pnl_usdt)

    # ── Blocking helpers (run in thread) ──────────────────────────────────────

    def _insert_signal(self, d: dict) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO signals (ts, symbol, regime, direction, score, signals, fire)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _utcnow(),
                    d.get("symbol", ""),
                    d.get("regime", ""),
                    d.get("direction", ""),
                    d.get("score", 0.0),
                    json.dumps(d.get("signals", {})),
                    1 if d.get("fire") else 0,
                ),
            )

    def _insert_trade(self, score_dict: dict, order: dict) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO trades
                    (ts, symbol, direction, regime, entry, stop_loss, take_profit,
                     size, order_id, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'OPEN')
                """,
                (
                    _utcnow(),
                    score_dict.get("symbol", ""),
                    score_dict.get("direction", ""),
                    score_dict.get("regime", ""),
                    order.get("entry", 0.0),
                    order.get("stop", 0.0),
                    order.get("take_profit", 0.0),
                    order.get("qty", 0.0),
                    str(order.get("orderId", "")),
                ),
            )

    def _insert_regime(self, symbol: str, regime: str) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT INTO regimes (ts, symbol, regime) VALUES (?, ?, ?)",
                (_utcnow(), symbol, regime),
            )

    def _query_open_deals(self) -> list[tuple[str, str]]:
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT symbol, direction FROM trades WHERE status = 'OPEN'"
            ).fetchall()
        return [(r[0], r[1]) for r in rows]

    def _update_closed(
        self, symbol: str, direction: str, exit_price: float, pnl_usdt: float
    ) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """UPDATE trades SET status='FILLED', exit_price=?, pnl_usdt=?, closed_ts=?
                   WHERE symbol=? AND direction=? AND status='OPEN'""",
                (exit_price, pnl_usdt, _utcnow(), symbol, direction),
            )
