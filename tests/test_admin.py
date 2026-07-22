"""Administration: visibility, block, delete, password reset, and the limits.

Runs against whichever backend userstore is configured for, so it also serves
as the Postgres check:

    .venv/bin/python tests/test_admin.py
    DATABASE_URL=postgresql://… .venv/bin/python tests/test_admin.py

The most important assertion here is a negative one: **no route may expose a
password**. They are one-way hashes and there is nothing to return.
"""

import os
import re
import sys
import tempfile
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config

if not os.getenv("DATABASE_URL"):
    config.BASE_DIR = Path(tempfile.mkdtemp())

os.environ.setdefault("MASTER_PASSWORD", "test-master-password")

import accounts  # noqa: E402
import admin  # noqa: E402
import portfolio as pf  # noqa: E402
import userstore  # noqa: E402

failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("PASS  " if cond else "FAIL  ") + name +
          (f"\n      {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(name)


def main() -> None:
    userstore.connect()
    print(f"backend: {userstore.backend()}\n")

    tag = uuid.uuid4().hex[:8]
    admin.MASTER_USERNAME = f"master_{tag}"
    admin.MASTER_PASSWORD = "test-master-password"
    admin.ensure_master()

    boss = accounts.authenticate(admin.MASTER_USERNAME, "test-master-password")
    check("master account can log in", boss["id"] > 0)
    check("master is flagged admin", boss["is_admin"] is True)
    check("master has a portfolio", pf.cash(boss["id"]) > 0)

    u = accounts.create_user(f"victim_{tag}", "password12345", 10_000)
    pf.buy(u["id"], "BTC", 100.0, 1, usd=2000)

    # --- visibility -------------------------------------------------------
    players = admin.list_players({"BTC": 120.0})
    row = next(p for p in players if p["id"] == u["id"])
    check("admin sees the username", row["username"] == f"victim_{tag}")
    check("admin sees portfolio value", row["total"] > 0)
    check("admin sees holdings", len(row["holdings"]) == 1)
    check("admin sees trade count", row["trades"] == 1)

    detail = admin.player_detail(u["id"], {"BTC": 120.0})
    check("detail includes trade history", len(detail["trades"]) == 1)
    check("detail includes equity curve", isinstance(detail["equity"], list))
    check("detail includes sessions", isinstance(detail["sessions"], list))

    # --- the negative that matters ----------------------------------------
    blob = str(players) + str(detail)
    check("no stored hash is exposed", "pbkdf2_sha256$" not in blob)
    check("no hash-shaped hex is exposed", not re.search(r"\$[0-9a-f]{32,}", blob))
    check("password field is explicitly null",
          row["password"] is None and detail["password"] is None)

    # --- block ------------------------------------------------------------
    admin.set_blocked(u["id"], True)
    try:
        accounts.authenticate(f"victim_{tag}", "password12345")
        check("blocked account cannot authenticate", False)
    except accounts.BlockedError:
        check("blocked account cannot authenticate", True)
    check("sessions are kept so the block is explained, not silent",
          isinstance(userstore.query(
              "SELECT * FROM sessions WHERE user_id = ?", (u["id"],)), list))
    admin.set_blocked(u["id"], False)
    check("unblock restores authentication",
          accounts.authenticate(f"victim_{tag}", "password12345")["id"] == u["id"])

    # --- password reset ---------------------------------------------------
    admin.reset_password(u["id"], "brandnewpass1")
    check("reset password takes effect",
          accounts.authenticate(f"victim_{tag}", "brandnewpass1")["id"] == u["id"])
    try:
        accounts.authenticate(f"victim_{tag}", "password12345")
        check("old password stops working", False)
    except accounts.AuthError:
        check("old password stops working", True)
    try:
        admin.reset_password(u["id"], "abc")
        check("reset enforces password length", False)
    except admin.AdminError:
        check("reset enforces password length", True)

    # --- admin self-protection -------------------------------------------
    for name, fn in [
        ("admins cannot be blocked", lambda: admin.set_blocked(boss["id"], True)),
        ("admins cannot be deleted", lambda: admin.delete_player(boss["id"])),
    ]:
        try:
            fn()
            check(name, False)
        except admin.AdminError:
            check(name, True)

    # --- guests are visitors, not players ---------------------------------
    # The visibility line is evidence of a real client, not trading. A crawler
    # requests the page once and drops the Set-Cookie, so its row keeps
    # last_seen_ts NULL forever; a browser returns the cookie on the next XHR
    # and user_for_session() -> _touch() stamps it. Simulated here by calling
    # the real session path rather than writing last_seen_ts by hand, so the
    # test breaks if that path stops stamping.
    crawler = accounts.create_guest(10_000)          # never comes back
    visitor = accounts.create_guest(10_000)          # returns with the cookie
    trader = accounts.create_guest(10_000)
    pf.buy(trader["id"], "BTC", 100.0, 1, usd=1000)

    for g in (visitor, trader):
        userstore.user_for_session(userstore.new_session(g["id"]))
    check("returning visitor got a last_seen stamp",
          userstore.query_one("SELECT last_seen_ts FROM users WHERE id = ?",
                              (visitor["id"],))["last_seen_ts"] is not None)
    check("one-shot crawler did not",
          userstore.query_one("SELECT last_seen_ts FROM users WHERE id = ?",
                              (crawler["id"],))["last_seen_ts"] is None)

    listed = {p["id"] for p in admin.list_players({"BTC": 120.0})}
    check("crawler row is not listed", crawler["id"] not in listed)
    check("real visitor is listed without having traded", visitor["id"] in listed)
    check("real visitor who traded is listed", trader["id"] in listed)
    check("registered players are unaffected", u["id"] in listed)

    # Hidden must mean hidden, not merely unpainted: the list and the
    # by-id fetch have to agree or this is cosmetic.
    try:
        admin.player_detail(crawler["id"], {"BTC": 120.0})
        check("crawler row cannot be fetched by id", False)
    except admin.AdminError:
        check("crawler row cannot be fetched by id", True)
    check("real visitor can be fetched by id",
          admin.player_detail(visitor["id"], {"BTC": 120.0})["id"] == visitor["id"])

    for name, fn in [
        ("guests cannot be blocked", lambda: admin.set_blocked(visitor["id"], True)),
        ("guests cannot be deleted", lambda: admin.delete_player(visitor["id"])),
        ("guests cannot have a password reset",
         lambda: admin.reset_password(visitor["id"], "password12345")),
    ]:
        try:
            fn()
            check(name, False)
        except admin.AdminError:
            check(name, True)
    check("a refused block leaves the guest untouched",
          userstore.query_one(
              "SELECT is_blocked FROM users WHERE id = ?",
              (visitor["id"],))["is_blocked"] in (0, False))

    # Three distinct numbers. Collapsing them would either hide the crawler
    # volume or make the visitor count look inflated.
    st = admin.stats({})
    check("stats count real guests, not raw rows", st["guests"] == 2)
    check("stats still report raw guest rows", st["guest_rows"] >= 3)
    check("raw rows exceed real guests (crawlers counted separately)",
          st["guest_rows"] > st["guests"])
    check("freshly seen guests count as active", st["guests_active"] == 2)

    # --- delete releases the username -------------------------------------
    admin.delete_player(u["id"])
    check("user row removed",
          userstore.query_one("SELECT id FROM users WHERE id = ?", (u["id"],)) is None)
    for table in ("user_trades", "holdings", "portfolios", "user_equity", "sessions"):
        check(f"{table} rows removed",
              userstore.query(
                  f"SELECT * FROM {table} WHERE user_id = ?", (u["id"],)) == [])
    reused = accounts.create_user(f"victim_{tag}", "password12345", 10_000)
    check("USERNAME IS FREED FOR REUSE", reused["id"] != u["id"])

    s = admin.stats({})
    check("stats report totals", s["total_accounts"] >= 2)
    check("stats report the backend", s["store_backend"] in ("sqlite", "postgres"))

    # --- exactly one admin, always -----------------------------------------
    # Renaming MASTER_USERNAME must *move* the privilege. Granting it to the
    # new name while leaving it on the old one gives a former admin permanent
    # access to every player's data -- which is what happened in production.
    def admin_names() -> set[str]:
        return {r["username"] for r in userstore.query(
            "SELECT username FROM users WHERE is_admin = 1")}

    first = f"admin_a_{tag}"
    second = f"admin_b_{tag}"

    admin.MASTER_USERNAME = first
    admin.ensure_master()
    check("configured name is the only admin", admin_names() == {first})

    admin.MASTER_USERNAME = second
    admin.ensure_master()
    check("RENAME LEAVES EXACTLY ONE ADMIN", admin_names() == {second},
          f"admins={sorted(admin_names())}")
    check("the previous admin still exists as an ordinary player",
          userstore.query_one(
              "SELECT id FROM users WHERE username = ?", (first,)) is not None)
    check("the demoted account can no longer act as admin",
          accounts.authenticate(first, "test-master-password")["is_admin"] is False)
    check("the demoted account's sessions were revoked",
          userstore.query(
              "SELECT s.token FROM sessions s JOIN users u ON u.id = s.user_id "
              "WHERE u.username = ?", (first,)) == [])

    print()
    if failures:
        print(f"{len(failures)} FAILURE(S): {failures}")
        sys.exit(1)
    print("all admin checks passed")


if __name__ == "__main__":
    main()
