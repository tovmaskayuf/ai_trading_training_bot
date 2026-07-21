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
    is_admin      INTEGER NOT NULL DEFAULT 0,
    is_blocked    INTEGER NOT NULL DEFAULT 0,
    blocked_ts    {d.big_int},
    last_seen_ts  {d.big_int},
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

# Re-entrant, and it guards statement execution as well as connect().
#
# There is one process-wide connection, and commit/rollback act on the whole
# connection rather than on a cursor: two threads interleaving inside tx()
# would let one thread's rollback discard another's uncommitted statements.
# That was harmless while every route was `async def` on a single event loop --
# nothing interleaved -- but the DB-touching routes are plain `def` now and
# FastAPI runs those in a threadpool, so the overlap is real.
#
# Re-entrant because the read-then-write helpers (portfolio.buy, admin.delete)
# query before opening their transaction, and tx() itself calls connect().
_lock = threading.RLock()


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


# Columns added after the first release. CREATE TABLE IF NOT EXISTS silently
# does nothing on an existing table, so new columns need an explicit migration
# or a live database keeps the old shape and every query naming them fails.
_ADDED_COLUMNS: list[tuple[str, str, str]] = [
    ("users", "is_admin", "INTEGER NOT NULL DEFAULT 0"),
    ("users", "is_blocked", "INTEGER NOT NULL DEFAULT 0"),
    ("users", "blocked_ts", "BIGINT"),
    ("users", "last_seen_ts", "BIGINT"),
]


def _existing_columns(cur: Any, table: str) -> set[str]:
    if IS_POSTGRES:
        # Scoped to the schema we actually write to. information_schema spans
        # every schema on the database, so an unrelated `users` table elsewhere
        # would report our columns as already present, the ALTER would be
        # skipped, and the failure would surface later as every admin query
        # erroring on a column that was never added.
        cur.execute("SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = %s AND table_schema = current_schema()",
                    (table,))
        return {r[0] for r in cur.fetchall()}
    cur.execute(f"PRAGMA table_info({table})")
    return {r[1] for r in cur.fetchall()}


def _migrate(conn: Any) -> None:
    cur = conn.cursor()
    try:
        for table, column, decl in _ADDED_COLUMNS:
            if column not in _existing_columns(cur, table):
                log.info("userstore: adding %s.%s", table, column)
                cur.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
        conn.commit()
    finally:
        cur.close()


def _init_schema(conn: Any) -> None:
    cur = conn.cursor()
    for stmt in _split_statements(_schema()):
        cur.execute(stmt)
    conn.commit()
    cur.close()
    _migrate(conn)


def backend() -> str:
    return "postgres" if IS_POSTGRES else "sqlite"


@contextmanager
def tx() -> Iterator[Any]:
    conn = connect()
    with _lock:
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
    with _lock:
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


# At most one activity write per user per hour. This sits on the read path of
# every single request, so an unconditional UPDATE would double the write load
# of the whole app to record a field only the admin console and the guest purge
# ever read -- an hour is far finer than either needs.
TOUCH_INTERVAL_MS = 3_600_000


def user_for_session(token: str | None) -> dict[str, Any] | None:
    if not token:
        return None
    row = query_one(
        "SELECT u.id, u.username, u.display_name, u.is_guest, u.is_admin, "
        "       u.is_blocked, u.last_seen_ts, u.created_ts "
        "FROM sessions s JOIN users u ON u.id = s.user_id "
        "WHERE s.token = ? AND s.expires_ts > ?",
        (token, now_ms()),
    )
    if row:
        # SQLite stores these as 0/1; normalise so callers can trust the type.
        for f in ("is_guest", "is_admin", "is_blocked"):
            row[f] = bool(row[f])
        _touch(row)
    return row


def _touch(row: dict[str, Any]) -> None:
    """Record that this user is active, throttled to TOUCH_INTERVAL_MS.

    Without this `last_seen_ts` stays NULL forever, and the guest purge has to
    fall back to created_ts -- which would evict someone who has been playing
    daily for a fortnight purely because their account is old.
    """
    ts = now_ms()
    last = row.get("last_seen_ts")
    if last is not None and ts - last < TOUCH_INTERVAL_MS:
        return
    execute("UPDATE users SET last_seen_ts = ? WHERE id = ?", (ts, row["id"]))
    row["last_seen_ts"] = ts


def end_session(token: str | None) -> None:
    if token:
        execute("DELETE FROM sessions WHERE token = ?", (token,))


def purge_expired_sessions() -> int:
    """Drop sessions past their expiry. Returns how many went.

    user_for_session already filters on expires_ts, so a stale row is never
    honoured -- but nothing deleted them either, and at a 90-day lifetime the
    table only ever grew.
    """
    with tx() as cur:
        cur.execute(DIALECT.convert("DELETE FROM sessions WHERE expires_ts <= ?"),
                    (now_ms(),))
        return cur.rowcount or 0


# --- Maintenance -----------------------------------------------------------


# Child tables keyed by user_id. Ordered so rows never outlive their user; no
# backend here declares foreign keys, so nothing cascades on its own.
_USER_TABLES = ("user_equity", "user_trades", "holdings", "portfolios", "sessions")


def purge_stale_guests(ttl_days: int = 0) -> int:
    """Delete guest accounts that were abandoned without ever being used.

    A guest row is created for *every* cookie-less request, so crawlers, uptime
    checks and one-off page loads each leave one behind permanently. Each was
    then charged an equity row every cycle for the lifetime of the deployment.

    Only guests that never traded and hold nothing are eligible: anyone who
    actually played keeps their portfolio until they claim an account, and a
    registered account is never touched regardless of age.
    """
    ttl_days = ttl_days or config.GUEST_TTL_DAYS
    cutoff = now_ms() - ttl_days * 86_400_000

    victims = [r["id"] for r in query(
        "SELECT u.id FROM users u "
        "WHERE u.is_guest = 1 "
        # NULL last_seen_ts means they predate activity tracking; fall back to
        # creation so those still age out instead of living forever.
        "  AND COALESCE(u.last_seen_ts, u.created_ts) < ? "
        "  AND NOT EXISTS (SELECT 1 FROM user_trades t WHERE t.user_id = u.id) "
        "  AND NOT EXISTS (SELECT 1 FROM holdings h WHERE h.user_id = u.id AND h.qty > 0)",
        (cutoff,))]
    if not victims:
        return 0

    with tx() as cur:
        c = DIALECT.convert
        # Chunked: some drivers cap how many placeholders one statement takes,
        # and a long-idle deployment can accumulate a lot of these at once.
        for i in range(0, len(victims), 500):
            batch = victims[i:i + 500]
            marks = ",".join("?" * len(batch))
            for table in _USER_TABLES:
                cur.execute(c(f"DELETE FROM {table} WHERE user_id IN ({marks})"),
                            tuple(batch))
            cur.execute(c(f"DELETE FROM users WHERE id IN ({marks})"), tuple(batch))

    log.info("purged %d abandoned guest accounts", len(victims))
    return len(victims)


def prune_equity(retention_days: int = 0) -> int:
    """Trim equity history past the retention window.

    The market-data store prunes itself, but this table lives in Postgres and
    was never trimmed at all -- one row per portfolio per cycle, on a free plan
    with a 1 GB ceiling holding the only copy of every account.
    """
    retention_days = retention_days or config.EQUITY_RETENTION_DAYS
    cutoff = now_ms() - retention_days * 86_400_000
    with tx() as cur:
        cur.execute(DIALECT.convert("DELETE FROM user_equity WHERE ts < ?"), (cutoff,))
        return cur.rowcount or 0


def maintenance() -> dict[str, int]:
    """One housekeeping pass over the durable store. Safe to call at any time."""
    return {
        "guests_purged": purge_stale_guests(),
        "sessions_purged": purge_expired_sessions(),
        "equity_pruned": prune_equity(),
    }
