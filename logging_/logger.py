"""SQLite trade logger — persists every signal evaluation and trade execution."""
import asyncio
import json
import logging
import os
import sqlite3
from datetime import datetime, timezone

_log = logging.getLogger(__name__)

_DB_PATH     = os.environ.get("DB_PATH", "confluence_bot.db")
_SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "schema.sql")
_PRUNE_DAYS  = int(os.environ.get("LOG_PRUNE_DAYS", "7"))
_pruned      = False   # prune runs once per process, not once per instantiation


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class TradeLogger:
    """Async-safe SQLite logger for signals, trades, and regime events."""

    def __init__(self, db_path: str = _DB_PATH) -> None:
        global _pruned
        self.db_path = db_path
        self._init_db()
        if not _pruned:
            self._prune(days=_PRUNE_DAYS)
            _pruned = True

    def _init_db(self) -> None:
        with open(_SCHEMA_PATH) as f:
            ddl = f.read()
        with sqlite3.connect(self.db_path) as conn:
            conn.executescript(ddl)

    def _prune(self, days: int = 7) -> None:
        try:
            with sqlite3.connect(self.db_path) as conn:
                sig_del = conn.execute(
                    "DELETE FROM signals WHERE ts < datetime('now', ?)",
                    (f"-{days} days",),
                ).rowcount
                reg_del = conn.execute(
                    "DELETE FROM regimes WHERE ts < datetime('now', ?)",
                    (f"-{days} days",),
                ).rowcount
                conn.commit()
            with sqlite3.connect(self.db_path) as conn:
                conn.execute("VACUUM")
            _log.info("DB pruned: %d signals, %d regimes older than %d days removed",
                      sig_del, reg_del, days)
        except sqlite3.OperationalError as exc:
            _log.warning("DB prune failed: %s", exc)

    async def log_signal(self, score_dict: dict) -> None:
        await asyncio.to_thread(self._insert_signal, score_dict)

    async def log_trade(self, score_dict: dict, order: dict) -> None:
        await asyncio.to_thread(self._insert_trade, score_dict, order)

    async def log_regime(self, symbol: str, regime: str) -> None:
        await asyncio.to_thread(self._insert_regime, symbol, regime)

    async def load_active_deals(self) -> list[tuple[str, str]]:
        return await asyncio.to_thread(self._query_open_deals)

    async def close_deal(self, symbol: str, direction: str, exit_price: float, pnl_usdt: float) -> None:
        await asyncio.to_thread(self._update_closed, symbol, direction, exit_price, pnl_usdt)

    async def log_trade_close(self, trade: dict, outcome: str,
                              exit_price: float, pnl: float) -> None:
        await asyncio.to_thread(self._close_trade, trade, outcome, exit_price, pnl)

    def _insert_signal(self, d: dict) -> None:
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute(
                    "INSERT INTO signals (ts, symbol, regime, direction, score, signals, fire) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (_utcnow(), d.get("symbol",""), d.get("regime",""), d.get("direction",""),
                     d.get("score", 0.0), json.dumps(d.get("signals", {})), 1 if d.get("fire") else 0),
                )
        except sqlite3.OperationalError as exc:
            _log.warning("log_signal skipped (%s): %s", d.get("symbol","?"), exc)

    def _insert_trade(self, score_dict: dict, order: dict) -> None:
        try:
            now = datetime.now(timezone.utc)
            entry_time = now.strftime("%H:%M")
            risk_usdt = order.get("risk_usdt", 0.0)
            with sqlite3.connect(self.db_path) as conn:
                conn.execute(
                    """INSERT INTO trades
                        (ts, symbol, direction, regime, entry, stop_loss, take_profit,
                         size, order_id, status, entry_time, risk_usdt)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'OPEN', ?, ?)""",
                    (_utcnow(), score_dict.get("symbol",""), score_dict.get("direction",""),
                     score_dict.get("regime",""), order.get("entry", 0.0), order.get("stop", 0.0),
                     order.get("take_profit", 0.0), order.get("qty", 0.0),
                     str(order.get("orderId","")), entry_time, risk_usdt),
                )
        except sqlite3.OperationalError as exc:
            _log.error("log_trade FAILED (%s): %s", score_dict.get("symbol","?"), exc)

    def _insert_regime(self, symbol: str, regime: str) -> None:
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute(
                    "INSERT INTO regimes (ts, symbol, regime) VALUES (?, ?, ?)",
                    (_utcnow(), symbol, regime),
                )
        except sqlite3.OperationalError as exc:
            _log.warning("log_regime skipped (%s): %s", symbol, exc)

    def _query_open_deals(self) -> list[tuple[str, str]]:
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT symbol, direction FROM trades WHERE status = 'OPEN'"
            ).fetchall()
        return [(r[0], r[1]) for r in rows]

    def _update_closed(self, symbol: str, direction: str, exit_price: float, pnl_usdt: float) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """UPDATE trades SET status='CLOSED', exit_price=?, pnl_usdt=?, closed_ts=?
                   WHERE symbol=? AND direction=? AND status='OPEN'""",
                (exit_price, pnl_usdt, _utcnow(), symbol, direction),
            )

    def _close_trade(self, trade: dict, outcome: str,
                     exit_price: float, pnl: float) -> None:
        """Close a trade with full P&L, duration, and running equity."""
        try:
            entry_ms = trade.get("entry_time_ms", 0)
            exit_ms  = trade.get("exit_time_ms", 0)

            if entry_ms and exit_ms:
                entry_dt = datetime.fromtimestamp(entry_ms / 1000, tz=timezone.utc)
                exit_dt  = datetime.fromtimestamp(exit_ms / 1000, tz=timezone.utc)
                exit_time    = exit_dt.strftime("%H:%M")
                duration_min = int((exit_ms - entry_ms) / 60000)
            else:
                exit_time    = datetime.now(timezone.utc).strftime("%H:%M")
                duration_min = 0

            with sqlite3.connect(self.db_path) as conn:
                # Fetch previous equity for running total
                row = conn.execute(
                    "SELECT equity_after FROM trades WHERE status='CLOSED' "
                    "ORDER BY closed_ts DESC LIMIT 1"
                ).fetchone()
                prev_equity = float(row[0]) if row else float(
                    os.environ.get("STARTING_CAPITAL", "5000")
                )
                equity_after = round(prev_equity + pnl, 2)

                conn.execute("""
                    UPDATE trades SET
                        exit_time      = ?,
                        exit_price     = ?,
                        pnl_usdt       = ?,
                        equity_after   = ?,
                        duration_min   = ?,
                        status         = 'CLOSED',
                        closed_ts      = ?
                    WHERE id = ?
                """, (
                    exit_time,
                    exit_price,
                    round(pnl, 2),
                    equity_after,
                    duration_min,
                    _utcnow(),
                    trade.get("id", 0),
                ))

            _log.info(
                "TRADE CLOSED | %s %s | %s | entry=%.2f exit=%.2f | "
                "risk=$%.2f pnl=$%.2f equity=$%.2f",
                trade.get("direction", "?"), trade.get("symbol", "?"), outcome,
                trade.get("entry_price", 0.0), exit_price,
                trade.get("risk_usdt", 0.0), pnl, equity_after,
            )
        except Exception as exc:
            _log.error("log_trade_close FAILED: %s", exc)
