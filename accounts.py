"""Account creation, login, and per-user portfolios.

Passwords are hashed with PBKDF2-HMAC-SHA256 from the standard library -- no
extra dependency, and the cost factor is tunable. Hashes are stored as
`pbkdf2_sha256$<iterations>$<salt_hex>$<hash_hex>` so the iteration count
travels with the hash and can be raised later without invalidating old ones.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import re
import secrets
from typing import Any

import config
import userstore
from userstore import now_ms

log = logging.getLogger("accounts")

PBKDF2_ITERATIONS = 240_000
USERNAME_RE = re.compile(r"^[a-zA-Z0-9_.-]{3,24}$")
MIN_PASSWORD = 8
MAX_PASSWORD = 200          # bcrypt-style truncation is not a concern, but an
                            # unbounded password is a cheap way to burn CPU.


class AuthError(ValueError):
    """A signup or login the user cannot complete. Message is user-facing."""


# --- Password hashing ------------------------------------------------------


def hash_password(password: str, *, iterations: int = PBKDF2_ITERATIONS) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"pbkdf2_sha256${iterations}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iters, salt_hex, hash_hex = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(iters)
        )
    except (ValueError, TypeError):
        return False
    # Constant-time: a timing side channel here leaks hash bytes.
    return hmac.compare_digest(dk.hex(), hash_hex)


# --- Validation ------------------------------------------------------------


def _validate(username: str, password: str) -> None:
    if not USERNAME_RE.match(username):
        raise AuthError(
            "Usernames must be 3-24 characters, using only letters, numbers, "
            "dots, dashes and underscores."
        )
    if len(password) < MIN_PASSWORD:
        raise AuthError(f"Passwords must be at least {MIN_PASSWORD} characters.")
    if len(password) > MAX_PASSWORD:
        raise AuthError(f"Passwords must be at most {MAX_PASSWORD} characters.")


# --- Signup / login --------------------------------------------------------


def _check_capital(starting_capital: float | None) -> float:
    capital = float(starting_capital or config.STARTING_CAPITAL)
    if not (config.CAPITAL_MIN <= capital <= config.CAPITAL_MAX):
        raise AuthError(
            f"Starting capital must be between ${config.CAPITAL_MIN:,.0f} "
            f"and ${config.CAPITAL_MAX:,.0f}."
        )
    return capital


def _seed_portfolio(user_id: int, capital: float, ts: int) -> None:
    userstore.execute(
        "INSERT INTO portfolios (user_id, cash, starting_capital, created_ts) "
        "VALUES (?,?,?,?)",
        (user_id, capital, capital, ts),
    )
    # Seed the curve so charts start at the opening balance rather than empty.
    userstore.execute(
        "INSERT INTO user_equity (user_id, ts, cash, invested, market_value, "
        "total, realized, fees) VALUES (?,?,?,?,?,?,?,?)",
        (user_id, ts, capital, 0.0, 0.0, capital, 0.0, 0.0),
    )


def _taken(username: str) -> bool:
    # Case-insensitive: "Tigran" and "tigran" must not both exist.
    return userstore.query_one(
        "SELECT id FROM users WHERE LOWER(username) = LOWER(?)", (username,)) is not None


def create_guest(starting_capital: float | None = None) -> dict[str, Any]:
    """An unnamed account so a first-time visitor can trade straight away."""
    capital = _check_capital(starting_capital)
    ts = now_ms()
    # Random suffix rather than a counter: the username is never shown, and a
    # sequential one would leak how many people have used the site.
    username = f"guest_{secrets.token_hex(8)}"
    user_id = userstore.insert_returning_id(
        "INSERT INTO users (username, display_name, password_hash, is_guest, created_ts) "
        "VALUES (?,?,?,?,?)",
        (username, "Guest", "!", 1, ts),   # "!" can never match a real hash
    )
    _seed_portfolio(user_id, capital, ts)
    return {"id": user_id, "username": username, "display_name": "Guest",
            "is_guest": True}


def claim_account(user_id: int, username: str, password: str) -> dict[str, Any]:
    """Convert a guest row into a real account, keeping its portfolio.

    Done in place rather than by creating a second row, so everything the guest
    already built -- holdings, trade history, equity curve -- carries over.
    """
    username = (username or "").strip()
    _validate(username, password or "")

    row = userstore.query_one("SELECT is_guest FROM users WHERE id = ?", (user_id,))
    if not row:
        raise AuthError("That session is no longer valid. Please reload the page.")
    if not row["is_guest"]:
        raise AuthError("You are already signed in to an account.")
    if _taken(username):
        raise AuthError("That username is already taken.")

    userstore.execute(
        "UPDATE users SET username = ?, display_name = ?, password_hash = ?, "
        "is_guest = 0 WHERE id = ?",
        (username, username, hash_password(password), user_id),
    )
    log.info("guest %s claimed as %s", user_id, username)
    return {"id": user_id, "username": username, "display_name": username,
            "is_guest": False}


def create_user(username: str, password: str,
                starting_capital: float | None = None) -> dict[str, Any]:
    username = (username or "").strip()
    display = username
    _validate(username, password or "")

    if _taken(username):
        raise AuthError("That username is already taken.")

    capital = _check_capital(starting_capital)
    ts = now_ms()
    user_id = userstore.insert_returning_id(
        "INSERT INTO users (username, display_name, password_hash, is_guest, created_ts) "
        "VALUES (?,?,?,?,?)",
        (username, display, hash_password(password), 0, ts),
    )
    _seed_portfolio(user_id, capital, ts)
    log.info("account created: %s (id=%s)", username, user_id)
    return {"id": user_id, "username": username, "display_name": display,
            "is_guest": False}


def _dummy_hash() -> str:
    """A real-cost hash to verify against when the account does not exist.

    Must use the production iteration count: a cheap placeholder returns almost
    instantly, so an unknown username answers far faster than a wrong password
    and the difference enumerates valid accounts. Computed once and cached,
    since the value itself is never checked -- only the work it forces.
    """
    global _DUMMY
    if _DUMMY is None:
        _DUMMY = hash_password(secrets.token_hex(16))
    return _DUMMY


_DUMMY: str | None = None


class BlockedError(AuthError):
    """A valid credential for an account that has been blocked."""


def authenticate(username: str, password: str) -> dict[str, Any]:
    row = userstore.query_one(
        "SELECT id, username, display_name, password_hash, is_blocked, is_admin "
        "FROM users WHERE LOWER(username) = LOWER(?)",
        ((username or "").strip(),),
    )
    # Hash even when the user does not exist, so a missing account and a wrong
    # password take the same time and cannot be told apart by timing.
    stored = row["password_hash"] if row else _dummy_hash()
    ok = verify_password(password or "", stored)
    if not row or not ok:
        raise AuthError("Incorrect username or password.")
    # Checked only after the password verifies: telling an unauthenticated
    # caller that an account is blocked would confirm the username exists.
    if row["is_blocked"]:
        raise BlockedError(
            "This account has been blocked. Please contact the administrator.")
    return {"id": row["id"], "username": row["username"],
            "display_name": row["display_name"],
            "is_admin": bool(row["is_admin"])}


def rename(user_id: int, display_name: str) -> str:
    display_name = (display_name or "").strip()
    if not 1 <= len(display_name) <= 24:
        raise AuthError("Display names must be 1-24 characters.")
    userstore.execute("UPDATE users SET display_name = ? WHERE id = ?",
                      (display_name, user_id))
    return display_name


def change_password(user_id: int, current: str, new: str) -> None:
    row = userstore.query_one(
        "SELECT username, password_hash FROM users WHERE id = ?", (user_id,))
    if not row or not verify_password(current or "", row["password_hash"]):
        raise AuthError("Your current password is incorrect.")
    _validate(row["username"], new or "")
    userstore.execute("UPDATE users SET password_hash = ? WHERE id = ?",
                      (hash_password(new), user_id))
    # Log every other device out; a password change should end stolen sessions.
    userstore.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
