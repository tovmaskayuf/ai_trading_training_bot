"""Retention and housekeeping for the durable store.

Covers the growth bounds rather than the accounting: which rows the engine is
allowed to write every cycle, and which rows eventually go away again. These
are *rates*, so nothing here fails on a fresh database -- the bugs they cover
only appear after a deployment has been up for days with real visitors, which
is exactly why they need a test rather than a look.

Runs against whichever backend userstore is configured for:

    .venv/bin/python tests/test_maintenance.py                    # sqlite
    DATABASE_URL=postgresql://... .venv/bin/python tests/test_maintenance.py
"""

import os
import sys
import tempfile
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config

# Redirect the SQLite path before userstore imports and resolves it.
if not os.getenv("DATABASE_URL"):
    config.BASE_DIR = Path(tempfile.mkdtemp())

import accounts  # noqa: E402
import portfolio as pf  # noqa: E402
import userstore  # noqa: E402
from userstore import now_ms  # noqa: E402

failures: list[str] = []

DAY = 86_400_000


def check(name: str, cond: bool, extra: str = "") -> None:
    print(("PASS  " if cond else "FAIL  ") + name + (f"  [{extra}]" if extra else ""))
    if not cond:
        failures.append(name)


def age(user_id: int, days: int) -> None:
    """Backdate a user so the TTL applies without waiting days for it."""
    t = now_ms() - days * DAY
    userstore.execute(
        "UPDATE users SET created_ts = ?, last_seen_ts = ? WHERE id = ?",
        (t, t, user_id))


def exists(user_id: int) -> bool:
    return userstore.query_one(
        "SELECT id FROM users WHERE id = ?", (user_id,)) is not None


def main() -> None:
    userstore.connect()
    print(f"backend: {userstore.backend()}\n")
    tag = uuid.uuid4().hex[:8]
    prices = {"BTC": 50_000.0}

    # --- Idle portfolios are not charged an equity row every cycle ----------
    #
    # The engine calls record_equity_all once a minute. A guest that never
    # traded has a flat curve already seeded at signup, so restating it 1,440
    # times a day is pure growth for no information.
    idle = accounts.create_guest(10_000.0)
    active = accounts.create_user(f"active_{tag}", "password12345")
    pf.buy(active["id"], "BTC", 50_000.0, now_ms(), usd=500.0)

    before_idle = userstore.query_one(
        "SELECT COUNT(*) AS n FROM user_equity WHERE user_id = ?", (idle["id"],))["n"]
    pf.record_equity_all(now_ms(), prices)
    pf.record_equity_all(now_ms() + 1, prices)
    after_idle = userstore.query_one(
        "SELECT COUNT(*) AS n FROM user_equity WHERE user_id = ?", (idle["id"],))["n"]
    after_active = userstore.query_one(
        "SELECT COUNT(*) AS n FROM user_equity WHERE user_id = ?", (active["id"],))["n"]

    check("idle portfolio gets no new equity rows", after_idle == before_idle,
          f"{before_idle} -> {after_idle}")
    check("a portfolio that traded still gets them", after_active >= 2,
          f"rows={after_active}")

    # A guest that actually traded is a real player and must keep recording.
    traded = accounts.create_guest(10_000.0)
    pf.buy(traded["id"], "BTC", 50_000.0, now_ms(), usd=100.0)
    n0 = userstore.query_one(
        "SELECT COUNT(*) AS n FROM user_equity WHERE user_id = ?", (traded["id"],))["n"]
    pf.record_equity_all(now_ms() + 2, prices)
    n1 = userstore.query_one(
        "SELECT COUNT(*) AS n FROM user_equity WHERE user_id = ?", (traded["id"],))["n"]
    check("a guest that traded keeps recording", n1 > n0, f"{n0} -> {n1}")

    # --- Abandoned guests are purged, real players are not ------------------
    fresh = accounts.create_guest(10_000.0)
    stale = accounts.create_guest(10_000.0)
    age(stale["id"], config.GUEST_TTL_DAYS + 1)

    old_player = accounts.create_guest(10_000.0)
    pf.buy(old_player["id"], "BTC", 50_000.0, now_ms(), usd=250.0)
    age(old_player["id"], config.GUEST_TTL_DAYS + 30)

    old_account = accounts.create_user(f"veteran_{tag}", "password12345")
    age(old_account["id"], config.GUEST_TTL_DAYS + 365)

    purged = userstore.purge_stale_guests()
    check("abandoned guest is purged", not exists(stale["id"]))
    check("recent guest is kept", exists(fresh["id"]))
    check("old guest WITH a portfolio is kept", exists(old_player["id"]))
    check("registered account is never purged", exists(old_account["id"]))
    check("purge reports a count", purged >= 1, f"purged={purged}")

    leftovers = sum(
        userstore.query_one(
            f"SELECT COUNT(*) AS n FROM {t} WHERE user_id = ?", (stale["id"],))["n"]
        for t in ("user_equity", "user_trades", "holdings", "portfolios", "sessions"))
    check("purged guest leaves no orphan rows", leftovers == 0, f"left={leftovers}")

    # --- Equity retention ---------------------------------------------------
    keep_ts = now_ms() - 1 * DAY
    drop_ts = now_ms() - (config.EQUITY_RETENTION_DAYS + 5) * DAY
    for ts in (keep_ts, drop_ts):
        userstore.execute(
            "INSERT INTO user_equity (user_id, ts, cash, invested, market_value, "
            "total, realized, fees) VALUES (?,?,?,?,?,?,?,?)",
            (active["id"], ts, 1.0, 0.0, 0.0, 1.0, 0.0, 0.0))

    userstore.prune_equity()
    remaining = {r["ts"] for r in userstore.query(
        "SELECT ts FROM user_equity WHERE user_id = ?", (active["id"],))}
    check("equity past retention is pruned", drop_ts not in remaining)
    check("recent equity is retained", keep_ts in remaining)

    # --- Sessions -----------------------------------------------------------
    live = userstore.new_session(active["id"])
    dead = userstore.new_session(active["id"])
    userstore.execute("UPDATE sessions SET expires_ts = ? WHERE token = ?",
                      (now_ms() - DAY, dead))
    userstore.purge_expired_sessions()
    check("expired session row is deleted",
          userstore.query_one("SELECT token FROM sessions WHERE token = ?",
                              (dead,)) is None)
    check("live session survives", userstore.user_for_session(live) is not None)

    # --- Activity tracking --------------------------------------------------
    # last_seen_ts drives the guest purge; if nothing writes it, the purge
    # silently falls back to created_ts and evicts active players.
    userstore.execute("UPDATE users SET last_seen_ts = NULL WHERE id = ?",
                      (active["id"],))
    userstore.user_for_session(live)
    seen = userstore.query_one(
        "SELECT last_seen_ts FROM users WHERE id = ?", (active["id"],))["last_seen_ts"]
    check("resolving a session records activity", seen is not None)

    # Throttled: a second resolve inside the window must not write again.
    userstore.user_for_session(live)
    seen2 = userstore.query_one(
        "SELECT last_seen_ts FROM users WHERE id = ?", (active["id"],))["last_seen_ts"]
    check("activity writes are throttled", seen2 == seen)

    # --- equity_series with no history --------------------------------------
    empty = accounts.create_user(f"empty_{tag}", "password12345")
    userstore.execute("DELETE FROM user_equity WHERE user_id = ?", (empty["id"],))
    check("equity_series copes with no history",
          pf.equity_series(empty["id"], None) == [])

    # --- maintenance() runs everything --------------------------------------
    done = userstore.maintenance()
    check("maintenance reports all three passes",
          set(done) == {"guests_purged", "sessions_purged", "equity_pruned"},
          str(done))

    print()
    if failures:
        print(f"{len(failures)} FAILURE(S): {failures}")
        sys.exit(1)
    print("all maintenance checks passed")


if __name__ == "__main__":
    main()
