"""Administration: full visibility over players, plus block and delete.

Deliberately **not** able to reveal passwords. They are stored as PBKDF2
hashes, which are one-way by construction -- there is nothing to read back, and
storing them reversibly would expose every player's password (people reuse them
across sites) for no operational gain. `reset_password()` is the answer to
"I need to get this user back in": set a new one and tell them.

Everything else about a player is visible here: identity, portfolio, holdings,
trade history, equity curve, sessions and activity.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import accounts
import config
import portfolio as pf
import userstore
from userstore import now_ms

log = logging.getLogger("admin")

MASTER_USERNAME = os.getenv("MASTER_USERNAME", "master").strip()
MASTER_PASSWORD = os.getenv("MASTER_PASSWORD", "").strip()


class AdminError(ValueError):
    """An admin action that cannot be completed. Message is user-facing."""


# --- Bootstrap -------------------------------------------------------------


def _demote_other_admins(keep_id: int | None) -> int:
    """Strip admin from every account except the configured one.

    Without this, changing MASTER_USERNAME grants admin to the new name but
    leaves it on the old one, so the previous holder keeps full access to every
    player's data indefinitely. Exactly one admin is the invariant; renaming
    must move the privilege, not copy it.
    """
    others = userstore.query(
        "SELECT id, username FROM users WHERE is_admin = 1 AND id <> ?",
        (keep_id if keep_id is not None else -1,))
    for row in others:
        userstore.execute("UPDATE users SET is_admin = 0 WHERE id = ?", (row["id"],))
        # Their sessions were admin sessions; end them rather than letting an
        # already-open console keep working until the cookie expires.
        userstore.execute("DELETE FROM sessions WHERE user_id = ?", (row["id"],))
        log.warning("revoked admin from %r (no longer MASTER_USERNAME)", row["username"])
    return len(others)


def ensure_master() -> None:
    """Create or repair the master account from the environment.

    The password is never stored in the repository -- this is a public
    repo, and a committed admin credential is readable by anyone. Set
    MASTER_PASSWORD in the host's environment instead; with it unset, no admin
    account is created at all rather than one with a guessable password.
    """
    if not MASTER_PASSWORD:
        existing = userstore.query_one(
            "SELECT id FROM users WHERE is_admin = 1")
        if not existing:
            log.warning("MASTER_PASSWORD is not set -- no admin account exists")
        return

    row = userstore.query_one(
        "SELECT id, is_admin FROM users WHERE LOWER(username) = LOWER(?)",
        (MASTER_USERNAME,))

    if row:
        # Re-apply on every boot so rotating the env var rotates the password,
        # and so the account cannot be left non-admin by an earlier state.
        userstore.execute(
            "UPDATE users SET password_hash = ?, is_admin = 1, is_blocked = 0, "
            "is_guest = 0 WHERE id = ?",
            (accounts.hash_password(MASTER_PASSWORD), row["id"]))
        _demote_other_admins(row["id"])
        log.info("master account refreshed (id=%s)", row["id"])
        return

    ts = now_ms()
    uid = userstore.insert_returning_id(
        "INSERT INTO users (username, display_name, password_hash, is_guest, "
        "is_admin, is_blocked, created_ts) VALUES (?,?,?,?,?,?,?)",
        (MASTER_USERNAME, MASTER_USERNAME,
         accounts.hash_password(MASTER_PASSWORD), 0, 1, 0, ts))
    # The admin plays too, so it needs a portfolio like anyone else.
    userstore.execute(
        "INSERT INTO portfolios (user_id, cash, starting_capital, created_ts) "
        "VALUES (?,?,?,?)",
        (uid, config.STARTING_CAPITAL, config.STARTING_CAPITAL, ts))
    userstore.execute(
        "INSERT INTO user_equity (user_id, ts, cash, invested, market_value, "
        "total, realized, fees) VALUES (?,?,?,?,?,?,?,?)",
        (uid, ts, config.STARTING_CAPITAL, 0.0, 0.0,
         config.STARTING_CAPITAL, 0.0, 0.0))
    _demote_other_admins(uid)
    log.info("master account created (id=%s)", uid)


def is_admin(user: dict[str, Any] | None) -> bool:
    return bool(user and user.get("is_admin"))


# --- Read ------------------------------------------------------------------


def _visible_sql(alias: str = "u") -> str:
    """SQL predicate for the accounts the console is allowed to show.

    Registered players always; a guest only once they have traded. Every
    cookie-less request mints a guest row -- crawlers, uptime probes and
    one-off page loads each leave one behind -- so untraded guests are
    overwhelmingly not people, and listing them buries the players who are.
    Placing a trade is the first deliberate thing a visitor does, which makes
    it a better line than age or session count.

    A cookie round-trip (last_seen_ts) was tried here as a "real device" test
    and reverted: it admits anyone who merely opened the page, which is most
    of the noise this filter exists to remove.

    Shared by list_players() and player_detail() so the two agree exactly. If
    they drifted, a guest hidden from the list would still be fetchable by id,
    which is hiding rather than protecting.
    """
    return (f"({alias}.is_guest = 0 OR EXISTS "
            f"(SELECT 1 FROM user_trades t WHERE t.user_id = {alias}.id))")


def list_players(prices: dict[str, float]) -> list[dict[str, Any]]:
    """Accounts the console tracks, with live standing. Guests flagged.

    Untraded guests are excluded -- see _visible_sql(). stats() still counts
    every guest, so the headline number stays honest about total traffic.
    """
    rows = userstore.query(
        "SELECT u.id, u.username, u.display_name, u.is_guest, u.is_admin, "
        "       u.is_blocked, u.blocked_ts, u.last_seen_ts, u.created_ts, "
        "       p.cash, p.starting_capital "
        "FROM users u LEFT JOIN portfolios p ON p.user_id = u.id "
        f"WHERE {_visible_sql('u')} "
        "ORDER BY u.created_ts DESC")

    holdings: dict[int, list[dict[str, Any]]] = {}
    for h in userstore.query(
            "SELECT user_id, symbol, qty, avg_cost FROM holdings WHERE qty > 0"):
        holdings.setdefault(h["user_id"], []).append(h)

    stats = {
        r["user_id"]: r for r in userstore.query(
            "SELECT user_id, COUNT(*) AS trades, "
            "       COALESCE(SUM(fee), 0) AS fees, "
            "       COALESCE(SUM(CASE WHEN side='SELL' THEN pnl END), 0) AS realized, "
            "       MAX(ts) AS last_trade_ts "
            "FROM user_trades GROUP BY user_id")
    }
    sessions = {
        r["user_id"]: r["n"] for r in userstore.query(
            "SELECT user_id, COUNT(*) AS n FROM sessions "
            "WHERE expires_ts > ? GROUP BY user_id", (now_ms(),))
    }

    out = []
    for r in rows:
        mv = 0.0
        held = []
        for h in holdings.get(r["id"], []):
            price = prices.get(h["symbol"])
            value = h["qty"] * price if price else h["qty"] * h["avg_cost"]
            mv += value
            held.append({"symbol": h["symbol"], "qty": h["qty"],
                         "avg_cost": h["avg_cost"], "value": value})
        cash = float(r["cash"] or 0)
        capital = float(r["starting_capital"] or 0) or 1.0
        total = cash + mv
        s = stats.get(r["id"], {})
        out.append({
            "id": r["id"],
            "username": r["username"],
            "name": r["display_name"],
            "is_guest": bool(r["is_guest"]),
            "is_admin": bool(r["is_admin"]),
            "is_blocked": bool(r["is_blocked"]),
            "blocked_ts": r["blocked_ts"],
            "created_ts": r["created_ts"],
            "last_seen_ts": r["last_seen_ts"],
            "last_trade_ts": s.get("last_trade_ts"),
            "active_sessions": sessions.get(r["id"], 0),
            "cash": cash,
            "market_value": mv,
            "total": total,
            # What they opened the account with, and what they have made on top
            # of it. The console ranks nothing, but a total on its own is still
            # misleading: starting capital is chosen freely on the landing
            # screen, so a large balance says nothing about how they played.
            "starting_capital": float(r["starting_capital"] or 0),
            "pnl": total - float(r["starting_capital"] or 0),
            "return_pct": (total / capital - 1) * 100,
            "trades": s.get("trades", 0),
            "fees_paid": float(s.get("fees") or 0),
            "realized_pnl": float(s.get("realized") or 0),
            "holdings": held,
            # Stated explicitly so the UI never implies a password is retrievable.
            "password": None,
            "password_note": "Stored as a one-way PBKDF2 hash; it cannot be read back. Use reset.",
        })
    return out


def player_detail(user_id: int, prices: dict[str, float]) -> dict[str, Any]:
    """Everything held about one player.

    Refuses anyone list_players() would not show, so the list is the whole
    surface rather than a filtered view over a fetchable one.
    """
    row = userstore.query_one(
        "SELECT u.id, u.username, u.display_name, u.is_guest, u.is_admin, "
        "       u.is_blocked, u.blocked_ts, u.last_seen_ts, u.created_ts, "
        f"       CASE WHEN {_visible_sql('u')} THEN 1 ELSE 0 END AS visible "
        "FROM users u WHERE u.id = ?",
        (user_id,))
    if not row:
        raise AdminError("That player no longer exists.")
    if not row.pop("visible"):
        raise AdminError(
            "That visitor is a guest who has not traded. The console does not "
            "track them until they place a trade or create an account.")

    for f in ("is_guest", "is_admin", "is_blocked"):
        row[f] = bool(row[f])

    return {
        **row,
        "portfolio": pf.snapshot(user_id, prices),
        "trades": userstore.query(
            "SELECT * FROM user_trades WHERE user_id = ? ORDER BY ts DESC LIMIT 500",
            (user_id,)),
        "equity": pf.equity_series(user_id, None),
        "sessions": userstore.query(
            "SELECT created_ts, expires_ts FROM sessions WHERE user_id = ? "
            "ORDER BY created_ts DESC", (user_id,)),
        "password": None,
        "password_note": "Stored as a one-way PBKDF2 hash; it cannot be read back. Use reset.",
    }


# --- Write -----------------------------------------------------------------


def _target(user_id: int) -> dict[str, Any]:
    row = userstore.query_one(
        "SELECT id, username, is_admin, is_guest FROM users WHERE id = ?",
        (user_id,))
    if not row:
        raise AdminError("That player no longer exists.")
    if row["is_admin"]:
        # Guards against an admin locking themselves out, and against one admin
        # removing another in a future multi-admin setup.
        raise AdminError("Administrator accounts cannot be blocked or deleted.")
    if row["is_guest"]:
        # Neither action does anything to a guest. There is no credential to
        # block -- clearing the cookie mints a fresh guest row on the next
        # request -- and purge_stale_guests() already removes untraded guests
        # after GUEST_TTL_DAYS. Signing up is what makes someone administrable,
        # which is the same line the leaderboard already draws.
        raise AdminError(
            "Guest accounts cannot be blocked or deleted. A guest has no "
            "login to block, and untraded guests are removed automatically "
            f"after {config.GUEST_TTL_DAYS} days.")
    return row


def set_blocked(user_id: int, blocked: bool) -> dict[str, Any]:
    row = _target(user_id)
    userstore.execute(
        "UPDATE users SET is_blocked = ?, blocked_ts = ? WHERE id = ?",
        (1 if blocked else 0, now_ms() if blocked else None, user_id))
    # Sessions are deliberately left in place. Deleting them would make the
    # blocked user anonymous, and require_user would hand them a fresh guest
    # account -- so they would silently keep playing instead of being told.
    # Keeping the session means every request resolves to the blocked user and
    # returns an explicit 403 they can actually read.
    log.info("%s %s", "blocked" if blocked else "unblocked", row["username"])
    return {"id": user_id, "username": row["username"], "is_blocked": blocked}


def delete_player(user_id: int) -> dict[str, Any]:
    """Remove a player entirely. The username becomes available again.

    Rows are deleted rather than flagged: a tombstoned row would keep the
    username reserved, and freeing it is the explicit requirement here.
    """
    row = _target(user_id)
    with userstore.tx() as cur:
        c = userstore.DIALECT.convert
        for table in ("user_equity", "user_trades", "holdings",
                      "portfolios", "sessions"):
            cur.execute(c(f"DELETE FROM {table} WHERE user_id = ?"), (user_id,))
        cur.execute(c("DELETE FROM users WHERE id = ?"), (user_id,))
    log.info("deleted player %s (username %r released)", user_id, row["username"])
    return {"id": user_id, "username": row["username"], "deleted": True}


def reset_password(user_id: int, new_password: str) -> dict[str, Any]:
    """Set a new password for a player. The only way to restore access, since
    the existing one cannot be read."""
    row = userstore.query_one(
        "SELECT id, username, is_guest FROM users WHERE id = ?", (user_id,))
    if not row:
        raise AdminError("That player no longer exists.")
    if row["is_guest"]:
        raise AdminError("Guest accounts do not have a password.")
    if len(new_password or "") < accounts.MIN_PASSWORD:
        raise AdminError(
            f"Passwords must be at least {accounts.MIN_PASSWORD} characters.")

    userstore.execute("UPDATE users SET password_hash = ? WHERE id = ?",
                      (accounts.hash_password(new_password), user_id))
    userstore.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
    log.info("password reset for %s", row["username"])
    return {"id": user_id, "username": row["username"], "reset": True}


def stats(prices: dict[str, float]) -> dict[str, Any]:
    """Headline numbers for the admin dashboard."""
    counts = userstore.query_one(
        "SELECT COUNT(*) AS total, "
        "       COALESCE(SUM(CASE WHEN is_guest = 1 THEN 1 ELSE 0 END), 0) AS guests, "
        "       COALESCE(SUM(CASE WHEN is_blocked = 1 THEN 1 ELSE 0 END), 0) AS blocked "
        "FROM users") or {}
    trades = userstore.query_one(
        "SELECT COUNT(*) AS n, COALESCE(SUM(fee), 0) AS fees FROM user_trades") or {}
    return {
        "total_accounts": counts.get("total", 0),
        "guests": counts.get("guests", 0),
        "registered": (counts.get("total", 0) or 0) - (counts.get("guests", 0) or 0),
        "blocked": counts.get("blocked", 0),
        "total_trades": trades.get("n", 0),
        "total_fees": float(trades.get("fees") or 0),
        "store_backend": userstore.backend(),
    }
