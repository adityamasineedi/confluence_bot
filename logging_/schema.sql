-- confluence_bot SQLite schema

CREATE TABLE IF NOT EXISTS signals (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT    NOT NULL,           -- ISO-8601 UTC timestamp
    symbol      TEXT    NOT NULL,
    regime      TEXT    NOT NULL,           -- TREND | RANGE | CRASH
    direction   TEXT    NOT NULL,           -- LONG | SHORT
    score       REAL    NOT NULL,           -- 0.0 – 1.0
    signals     TEXT    NOT NULL,           -- JSON object: {signal_name: bool}
    fire        INTEGER NOT NULL            -- 0 or 1
);

CREATE TABLE IF NOT EXISTS trades (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT    NOT NULL,           -- ISO-8601 UTC timestamp
    symbol      TEXT    NOT NULL,
    direction   TEXT    NOT NULL,           -- LONG | SHORT
    regime      TEXT    NOT NULL,
    entry       REAL    NOT NULL,
    stop_loss   REAL    NOT NULL,
    take_profit REAL    NOT NULL,
    size        REAL    NOT NULL,           -- base currency units
    order_id    TEXT,                       -- Binance order ID
    status      TEXT    DEFAULT 'OPEN',    -- OPEN | FILLED | CANCELLED
    exit_price  REAL,
    pnl_usdt    REAL,
    closed_ts   TEXT
);

CREATE TABLE IF NOT EXISTS regimes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT    NOT NULL,
    symbol      TEXT    NOT NULL,
    regime      TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_signals_symbol_ts  ON signals  (symbol, ts);
CREATE INDEX IF NOT EXISTS idx_trades_symbol_ts   ON trades   (symbol, ts);
CREATE INDEX IF NOT EXISTS idx_regimes_symbol_ts  ON regimes  (symbol, ts);
