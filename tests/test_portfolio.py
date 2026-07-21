"""Per-user portfolio accounting, isolation, and the leaderboard.

Runs against whichever backend userstore is configured for, so it doubles as
the Postgres verification: set DATABASE_URL to a scratch database and run it.

    .venv/bin/python tests/test_portfolio.py                    # sqlite
    DATABASE_URL=postgresql://... .venv/bin/python tests/test_portfolio.py

With SQLite it points at a temp file and never touches real state. With
Postgres it uses the database you name -- point it at a scratch one, because
it deletes the rows it creates on the way in.
"""

import os
import sys
import tempfile
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config

# SQLite backend writes to config.BASE_DIR/data/users.db, so redirect it before
# userstore is imported and resolves the path.
if not os.getenv("DATABASE_URL"):
    config.BASE_DIR = Path(tempfile.mkdtemp())

import accounts  # noqa: E402
import portfolio as pf  # noqa: E402
import userstore  # noqa: E402

failures: list[str] = []


def check(name: str, cond: bool) -> None:
    print(("PASS  " if cond else "FAIL  ") + name)
    if not cond:
        failures.append(name)


def main() -> None:
    userstore.connect()
    print(f"backend: {userstore.backend()}\n")

    # Unique names so a shared Postgres can be re-run without collisions.
    tag = uuid.uuid4().hex[:8]
    alice = accounts.create_user(f"alice_{tag}", "password12345", 10_000)
    bob = accounts.create_user(f"bob_{tag}", "password12345", 10_000)
    A, B = alice["id"], bob["id"]

    # --- Isolation: the reason this module exists ---
    pf.buy(A, "BTC", 100.0, 1, usd=5000)
    check("buyer's cash moves", abs(pf.cash(A) - 5000) < 1e-6)
    check("other user's cash is untouched", abs(pf.cash(B) - 10_000) < 1e-6)
    check("buyer holds the asset", pf.holding_for(A, "BTC") is not None)
    check("other user holds nothing", pf.holding_for(B, "BTC") is None)
    try:
        pf.sell(B, "BTC", 100.0, 2, fraction=1.0)
        check("cannot sell another user's holding", False)
    except pf.TradeError:
        check("cannot sell another user's holding", True)

    # --- Accounting ---
    start = pf.cash(B)
    pf.buy(B, "ETH", 50.0, 10, usd=1000)
    pf.sell(B, "ETH", 50.0, 11, fraction=1.0)
    s = pf.snapshot(B, {"ETH": 50.0})
    check("flat round trip loses exactly the fees",
          abs((start - pf.cash(B)) - s["fees_paid"]) < 1e-9)

    # --- Guards ---
    for name, fn in [
        ("rejects overspend", lambda: pf.buy(A, "BTC", 100.0, 3, usd=1e9)),
        ("rejects unknown symbol", lambda: pf.buy(A, "NOPE", 100.0, 3, usd=10)),
        ("rejects zero price", lambda: pf.buy(A, "BTC", 0, 3, usd=10)),
        ("rejects oversell", lambda: pf.sell(A, "BTC", 100.0, 3, qty=1e9)),
        ("rejects negative amount", lambda: pf.buy(A, "BTC", 100.0, 3, usd=-5)),
    ]:
        try:
            fn()
            check(name, False)
        except pf.TradeError:
            check(name, True)

    # --- Average cost basis, fees included ---
    pf.reset(A, 10_000)
    pf.buy(A, "SOL", 10.0, 20, qty=100)
    pf.buy(A, "SOL", 20.0, 21, qty=100)
    h = pf.holding_for(A, "SOL")
    check("buys average into one line", abs(h["qty"] - 200) < 1e-9)
    expected = (1000 * (1 + config.FEE_RATE) + 2000 * (1 + config.FEE_RATE)) / 200
    check("average cost includes fees", abs(h["avg_cost"] - expected) < 1e-6)

    # --- Equity + reset ---
    pf.record_equity(A, 1000, {"SOL": 20.0})
    pf.record_equity(A, 2000, {"SOL": 20.0})
    check("equity series returns rows", len(pf.equity_series(A, 0)) >= 1)
    before_other = pf.snapshot(B, {})["trade_count"]
    pf.reset(A, 7500)
    check("reset restores capital", abs(pf.cash(A) - 7500) < 1e-9)
    check("reset clears holdings", pf.holdings(A) == [])
    check("reset clears trades", pf.trades(A) == [])
    check("reset does not affect other users",
          pf.snapshot(B, {})["trade_count"] == before_other)

    # --- Leaderboard ---
    pf.buy(B, "BTC", 100.0, 30, usd=1000)
    board = {r["user_id"]: r for r in pf.leaderboard({"BTC": 200.0})}
    check("leaderboard includes registered users", A in board and B in board)
    ranks = [r["rank"] for r in pf.leaderboard({"BTC": 200.0})]
    check("ranks are dense and ordered", ranks == sorted(ranks))
    returns = [r["return_pct"] for r in pf.leaderboard({"BTC": 200.0})]
    check("ordered by return descending", returns == sorted(returns, reverse=True))

    guest = accounts.create_guest(10_000)
    guest_ids = {r["user_id"] for r in pf.leaderboard({"BTC": 200.0})}
    check("guests are excluded from the leaderboard", guest["id"] not in guest_ids)
    check("guest still gets a working portfolio", pf.cash(guest["id"]) == 10_000)

    # --- Claiming a guest keeps its progress ---
    pf.buy(guest["id"], "BTC", 100.0, 40, usd=2500)
    before = pf.snapshot(guest["id"], {"BTC": 100.0})
    claimed = accounts.claim_account(guest["id"], f"charlie_{tag}", "password12345")
    after = pf.snapshot(guest["id"], {"BTC": 100.0})
    check("claim reuses the same user id", claimed["id"] == guest["id"])
    check("claim preserves cash", abs(before["cash"] - after["cash"]) < 1e-9)
    check("claim preserves holdings", len(after["holdings"]) == 1)
    check("claimed account can log in",
          accounts.authenticate(f"charlie_{tag}", "password12345")["id"] == guest["id"])
    check("claimed account appears on the leaderboard",
          guest["id"] in {r["user_id"] for r in pf.leaderboard({"BTC": 100.0})})
    try:
        accounts.claim_account(A, f"dave_{tag}", "password12345")
        check("cannot claim an already-registered account", False)
    except accounts.AuthError:
        check("cannot claim an already-registered account", True)

    print()
    if failures:
        print(f"{len(failures)} FAILURE(S): {failures}")
        sys.exit(1)
    print("all portfolio checks passed")


if __name__ == "__main__":
    main()
