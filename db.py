"""SQLite persistence.

WAL mode so the engine can write while the HTTP server reads concurrently.
All timestamps are epoch milliseconds (UTC) to match the upstream APIs.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from contextlib import contextmanager
from typing import Any, Iterator

import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS candles (
    symbol     TEXT NOT NULL,
    interval   TEXT NOT NULL,
    open_time  INTEGER NOT NULL,
    o REAL, h REAL, l REAL, c REAL, v REAL,
    PRIMARY KEY (symbol, interval, open_time)
);

CREATE TABLE IF NOT EXISTS snapshots (
    symbol     TEXT NOT NULL,
    ts         INTEGER NOT NULL,
    price      REAL,
    chg_24h    REAL,
    volume_24h REAL,
    mcap       REAL,
    rank       INTEGER,
    stale      INTEGER DEFAULT 0,
    PRIMARY KEY (symbol, ts)
);
CREATE INDEX IF NOT EXISTS idx_snapshots_ts ON snapshots(ts);

CREATE TABLE IF NOT EXISTS ratings (
    symbol     TEXT NOT NULL,
    ts         INTEGER NOT NULL,
    momentum   REAL,
    risk       REAL,
    structure  REAL,
    relative   REAL,
    composite  REAL,
    grade      TEXT,
    signal     TEXT,
    PRIMARY KEY (symbol, ts)
);
CREATE INDEX IF NOT EXISTS idx_ratings_ts ON ratings(ts);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- The user's paper-trading portfolio. This is a holdings model, not a
-- positions model: buying more of something you already own averages into one
-- line rather than opening a second lot, which is how a brokerage behaves.
CREATE TABLE IF NOT EXISTS manual_holdings (
    symbol     TEXT PRIMARY KEY,
    qty        REAL NOT NULL,
    avg_cost   REAL NOT NULL,
    updated_ts INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS manual_trades (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol  TEXT NOT NULL,
    side    TEXT NOT NULL,
    qty     REAL NOT NULL,
    price   REAL NOT NULL,
    value   REAL NOT NULL,
    fee     REAL NOT NULL DEFAULT 0,
    ts      INTEGER NOT NULL,
    pnl     REAL,
    pnl_pct REAL
);
CREATE INDEX IF NOT EXISTS idx_manual_trades_ts ON manual_trades(ts);

-- Portfolio value over time, one row per engine cycle plus one per trade.
-- Never pruned: the long-range charts are the product.
CREATE TABLE IF NOT EXISTS manual_equity (
    ts           INTEGER PRIMARY KEY,
    cash         REAL,
    invested     REAL,
    market_value REAL,
    total        REAL,
    realized     REAL,
    fees         REAL
);
"""

# Tables from the retired auto-trading bot. Dropped on connect so old databases
# migrate cleanly; the user's own portfolio tables are untouched.
MIGRATIONS = """
DROP TABLE IF EXISTS positions;
DROP TABLE IF EXISTS trades;
DROP TABLE IF EXISTS equity;
DELETE FROM meta WHERE key='cash';
"""


def now_ms() -> int:
    return int(time.time() * 1000)


_conn: sqlite3.Connection | None = None

# Guards tx(). commit() and rollback() act on the connection, not on a single
# statement, so two threads inside tx() at once would let one thread's rollback
# throw away the other's uncommitted writes. The read routes are plain `def`
# now, which means FastAPI runs them in a threadpool alongside the engine.
# Re-entrant so a writer that reads first cannot deadlock against itself.
_lock = threading.RLock()


def connect() -> sqlite3.Connection:
    """Return the process-wide connection, creating the schema on first call."""
    global _conn
    if _conn is None:
        with _lock:
            if _conn is None:
                config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
                conn = sqlite3.connect(config.DB_PATH, check_same_thread=False)
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=NORMAL")
                conn.executescript(SCHEMA)
                conn.executescript(MIGRATIONS)
                conn.commit()
                # Published only once it is fully migrated: another thread
                # reading _conn must never see a half-built schema.
                _conn = conn
    return _conn


@contextmanager
def tx() -> Iterator[sqlite3.Connection]:
    conn = connect()
    with _lock:
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def query(sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    return [dict(r) for r in connect().execute(sql, params).fetchall()]


def query_one(sql: str, params: tuple = ()) -> dict[str, Any] | None:
    row = connect().execute(sql, params).fetchone()
    return dict(row) if row else None


# --- Writers ---------------------------------------------------------------


def upsert_candles(symbol: str, interval: str, rows: list[tuple]) -> None:
    """rows: (open_time, o, h, l, c, v). Idempotent -- the in-progress candle
    gets rewritten with fresh values on each refresh."""
    if not rows:
        return
    with tx() as conn:
        conn.executemany(
            "INSERT INTO candles (symbol, interval, open_time, o, h, l, c, v) "
            "VALUES (?,?,?,?,?,?,?,?) "
            "ON CONFLICT(symbol, interval, open_time) DO UPDATE SET "
            "o=excluded.o, h=excluded.h, l=excluded.l, c=excluded.c, v=excluded.v",
            [(symbol, interval, *r) for r in rows],
        )


def insert_snapshot(symbol: str, ts: int, price: float | None, chg_24h: float | None,
                    volume_24h: float | None, mcap: float | None, rank: int | None,
                    stale: bool = False) -> None:
    with tx() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO snapshots "
            "(symbol, ts, price, chg_24h, volume_24h, mcap, rank, stale) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (symbol, ts, price, chg_24h, volume_24h, mcap, rank, int(stale)),
        )


def insert_rating(symbol: str, ts: int, r: dict[str, Any]) -> None:
    with tx() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO ratings "
            "(symbol, ts, momentum, risk, structure, relative, composite, grade, signal) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (symbol, ts, r.get("momentum"), r.get("risk"), r.get("structure"),
             r.get("relative"), r.get("composite"), r.get("grade"), r.get("signal")),
        )


def insert_manual_equity(ts: int, cash: float, invested: float, market_value: float,
                         total: float, realized: float, fees: float) -> None:
    with tx() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO manual_equity "
            "(ts, cash, invested, market_value, total, realized, fees) "
            "VALUES (?,?,?,?,?,?,?)",
            (ts, cash, invested, market_value, total, realized, fees),
        )


def manual_equity_series(since: int | None, max_points: int = 360) -> list[dict[str, Any]]:
    """Equity rows since `since` (None = all time), downsampled to about
    `max_points` by keeping the last row of each time bucket. Last-in-bucket is
    the right reduction for a value curve -- an average would smooth away the
    very moves the chart exists to show."""
    if since is None:
        first = query_one("SELECT MIN(ts) AS t FROM manual_equity")
        since = (first or {}).get("t") or 0
    span = max(now_ms() - since, 1)
    bucket = max(60_000, span // max_points)
    return query(
        "SELECT * FROM manual_equity WHERE ts IN ("
        "  SELECT MAX(ts) FROM manual_equity WHERE ts >= ? GROUP BY ts / ?"
        ") ORDER BY ts",
        (since, bucket),
    )


# --- Readers ---------------------------------------------------------------


def get_candles(symbol: str, interval: str = config.CANDLE_INTERVAL,
                limit: int = config.CANDLE_LIMIT) -> list[dict[str, Any]]:
    rows = query(
        "SELECT open_time, o, h, l, c, v FROM candles "
        "WHERE symbol=? AND interval=? ORDER BY open_time DESC LIMIT ?",
        (symbol, interval, limit),
    )
    return list(reversed(rows))


def latest_snapshot(symbol: str) -> dict[str, Any] | None:
    return query_one(
        "SELECT * FROM snapshots WHERE symbol=? ORDER BY ts DESC LIMIT 1", (symbol,)
    )


def latest_rating(symbol: str) -> dict[str, Any] | None:
    return query_one(
        "SELECT * FROM ratings WHERE symbol=? ORDER BY ts DESC LIMIT 1", (symbol,)
    )


def rating_history(symbol: str, limit: int = 500) -> list[dict[str, Any]]:
    rows = query(
        "SELECT ts, momentum, risk, structure, relative, composite, signal "
        "FROM ratings WHERE symbol=? ORDER BY ts DESC LIMIT ?",
        (symbol, limit),
    )
    return list(reversed(rows))


def price_history(symbol: str, limit: int = 200) -> list[dict[str, Any]]:
    rows = query(
        "SELECT ts, price FROM snapshots WHERE symbol=? AND price IS NOT NULL "
        "ORDER BY ts DESC LIMIT ?",
        (symbol, limit),
    )
    return list(reversed(rows))


def get_meta(key: str, default: str | None = None) -> str | None:
    row = query_one("SELECT value FROM meta WHERE key=?", (key,))
    return row["value"] if row else default


def set_meta(key: str, value: str) -> None:
    with tx() as conn:
        conn.execute(
            "INSERT INTO meta (key, value) VALUES (?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )


# --- Maintenance -----------------------------------------------------------


def prune(retention_days: int = config.RETENTION_DAYS) -> int:
    """Drop high-frequency rows past the retention window. Candles, trades and
    equity are kept indefinitely -- they are low-volume and historically useful."""
    cutoff = now_ms() - retention_days * 86_400_000
    with tx() as conn:
        n = conn.execute("DELETE FROM snapshots WHERE ts < ?", (cutoff,)).rowcount
        n += conn.execute("DELETE FROM ratings WHERE ts < ?", (cutoff,)).rowcount
    return n


def reset_manual() -> None:
    """Wipe the user's portfolio and its history. Market data is preserved."""
    with tx() as conn:
        conn.execute("DELETE FROM manual_holdings")
        conn.execute("DELETE FROM manual_trades")
        conn.execute("DELETE FROM manual_equity")
