"""Durable storage for accounts and per-user portfolios.

Deliberately separate from db.py. Market data (candles, snapshots, ratings)
stays in the ephemeral SQLite file because it is fully regenerable -- a cold
start rebuilds all 15 assets in a few seconds -- so losing it on restart costs
nothing. Accounts, holdings, trades and leaderboard standings are *not*
regenerable, so they live here and go to Postgres when DATABASE_URL is set.

With no DATABASE_URL (local development) this falls back to its own SQLite
file, so the app runs identically without a database server. Both backends
speak `ON CONFLICT ... DO UPDATE`, which is what makes one set of statements
work for each; the differences are confined to _Dialect below.
"""

from __future__ import annotations

import logging
import os
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from typing import Any, Iterator

import config

log = logging.getLogger("userstore")

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
IS_POSTGRES = DATABASE_URL.startswith(("postgres://", "postgresql://"))

SESSION_DAYS = 90


def now_ms() -> int:
    return int(time.time() * 1000)


# --- Dialect ---------------------------------------------------------------


class _Dialect:
    """The only places the two backends actually differ."""

    def __init__(self, postgres: bool):
        self.postgres = postgres

    @property
    def serial_pk(self) -> str:
        return "BIGSERIAL PRIMARY KEY" if self.postgres else "INTEGER PRIMARY KEY AUTOINCREMENT"

    @property
    def big_int(self) -> str:
        return "BIGINT"

    def convert(self, sql: str) -> str:
        """SQLite uses ?, psycopg uses %s. Statements are written with ?.

        Literal percent signs are doubled first, because psycopg treats a bare
        % as the start of a placeholder. Order matters: escaping before the
        substitution means the %s we introduce are the only real placeholders.
        """
        if not self.postgres:
            return sql
        return sql.replace("%", "%%").replace("?", "%s")


DIALECT = _Dialect(IS_POSTGRES)


def _schema() -> str:
    d = DIALECT
    return f"""
-- Guests are real rows with is_guest = 1 and no usable password. A visitor can
-- trade immediately, and signing up later converts their existing row in place
-- so the portfolio they already built carries over instead of being discarded.
-- Guests are excluded from the leaderboard.
CREATE TABLE IF NOT EXISTS users (
    id            {d.serial_pk},
    username      TEXT NOT NULL UNIQUE,
    display_name  TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    is_guest      INTEGER NOT NULL DEFAULT 0,
    created_ts    {d.big_int} NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    token      TEXT PRIMARY KEY,
    user_id    {d.big_int} NOT NULL,
    created_ts {d.big_int} NOT NULL,
    expires_ts {d.big_int} NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);

-- One row per user. `cash` is the uninvested balance; starting_capital is what
-- the leaderboard measures return against.
CREATE TABLE IF NOT EXISTS portfolios (
    user_id          {d.big_int} PRIMARY KEY,
    cash             DOUBLE PRECISION NOT NULL,
    starting_capital DOUBLE PRECISION NOT NULL,
    created_ts       {d.big_int} NOT NULL
);

CREATE TABLE IF NOT EXISTS holdings (
    user_id    {d.big_int} NOT NULL,
    symbol     TEXT NOT NULL,
    qty        DOUBLE PRECISION NOT NULL,
    avg_cost   DOUBLE PRECISION NOT NULL,
    updated_ts {d.big_int} NOT NULL,
    PRIMARY KEY (user_id, symbol)
);

CREATE TABLE IF NOT EXISTS user_trades (
    id      {d.serial_pk},
    user_id {d.big_int} NOT NULL,
    symbol  TEXT NOT NULL,
    side    TEXT NOT NULL,
    qty     DOUBLE PRECISION NOT NULL,
    price   DOUBLE PRECISION NOT NULL,
    value   DOUBLE PRECISION NOT NULL,
    fee     DOUBLE PRECISION NOT NULL DEFAULT 0,
    ts      {d.big_int} NOT NULL,
    pnl     DOUBLE PRECISION,
    pnl_pct DOUBLE PRECISION
);
CREATE INDEX IF NOT EXISTS idx_user_trades ON user_trades(user_id, ts);

CREATE TABLE IF NOT EXISTS user_equity (
    user_id      {d.big_int} NOT NULL,
    ts           {d.big_int} NOT NULL,
    cash         DOUBLE PRECISION,
    invested     DOUBLE PRECISION,
    market_value DOUBLE PRECISION,
    total        DOUBLE PRECISION,
    realized     DOUBLE PRECISION,
    fees         DOUBLE PRECISION,
    PRIMARY KEY (user_id, ts)
);
"""


# --- Connection ------------------------------------------------------------

_conn: Any = None
_lock = threading.Lock()


def connect() -> Any:
    global _conn
    if _conn is not None:
        return _conn

    with _lock:
        if _conn is not None:
            return _conn
        if IS_POSTGRES:
            import psycopg
            # autocommit off; tx() owns commit/rollback.
            _conn = psycopg.connect(DATABASE_URL, autocommit=False)
            log.info("userstore: postgres")
        else:
            path = config.BASE_DIR / "data" / "users.db"
            path.parent.mkdir(parents=True, exist_ok=True)
            _conn = sqlite3.connect(path, check_same_thread=False)
            _conn.row_factory = sqlite3.Row
            _conn.execute("PRAGMA journal_mode=WAL")
            log.info("userstore: sqlite (%s) -- set DATABASE_URL for durable storage", path)

        _init_schema(_conn)
    return _conn


def _split_statements(sql: str) -> list[str]:
    """Split on statement boundaries, ignoring semicolons inside -- comments.

    Splitting the raw text would cut a comment containing a semicolon in half
    and leave its second half parsed as SQL.
    """
    stripped = "\n".join(
        line.split("--", 1)[0] if "--" in line else line
        for line in sql.splitlines()
    )
    return [s.strip() for s in stripped.split(";") if s.strip()]


def _init_schema(conn: Any) -> None:
    cur = conn.cursor()
    for stmt in _split_statements(_schema()):
        cur.execute(stmt)
    conn.commit()
    cur.close()


def backend() -> str:
    return "postgres" if IS_POSTGRES else "sqlite"


@contextmanager
def tx() -> Iterator[Any]:
    conn = connect()
    cur = conn.cursor()
    try:
        yield cur
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()


def _rows_to_dicts(cur: Any) -> list[dict[str, Any]]:
    if cur.description is None:
        return []
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def query(sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    conn = connect()
    cur = conn.cursor()
    try:
        cur.execute(DIALECT.convert(sql), params)
        return _rows_to_dicts(cur)
    finally:
        cur.close()


def query_one(sql: str, params: tuple = ()) -> dict[str, Any] | None:
    rows = query(sql, params)
    return rows[0] if rows else None


def execute(sql: str, params: tuple = ()) -> None:
    with tx() as cur:
        cur.execute(DIALECT.convert(sql), params)


def insert_returning_id(sql: str, params: tuple = ()) -> int:
    """INSERT that yields the new row id on either backend."""
    with tx() as cur:
        if IS_POSTGRES:
            cur.execute(DIALECT.convert(sql + " RETURNING id"), params)
            return int(cur.fetchone()[0])
        cur.execute(DIALECT.convert(sql), params)
        return int(cur.lastrowid)


# --- Sessions --------------------------------------------------------------


def new_session(user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    ts = now_ms()
    execute(
        "INSERT INTO sessions (token, user_id, created_ts, expires_ts) VALUES (?,?,?,?)",
        (token, user_id, ts, ts + SESSION_DAYS * 86_400_000),
    )
    return token


def user_for_session(token: str | None) -> dict[str, Any] | None:
    if not token:
        return None
    row = query_one(
        "SELECT u.id, u.username, u.display_name, u.is_guest, u.created_ts "
        "FROM sessions s JOIN users u ON u.id = s.user_id "
        "WHERE s.token = ? AND s.expires_ts > ?",
        (token, now_ms()),
    )
    if row:
        # SQLite stores this as 0/1; normalise so callers can trust the type.
        row["is_guest"] = bool(row["is_guest"])
    return row


def end_session(token: str | None) -> None:
    if token:
        execute("DELETE FROM sessions WHERE token = ?", (token,))


def purge_expired_sessions() -> None:
    execute("DELETE FROM sessions WHERE expires_ts <= ?", (now_ms(),))
