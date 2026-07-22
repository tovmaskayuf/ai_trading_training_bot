"""Per-user paper-trading portfolios.

Same accounting as the original single-portfolio module: holdings with an
average cost basis (fees included in the basis), buys interpreted as all-in,
and a flat round trip losing exactly the fees. Everything is scoped to a
user_id and stored in userstore, so visitors no longer share one portfolio.

Cash lives in the `portfolios` row and every trade writes it inside the same
transaction as the holding and the trade row -- a crash between them would
otherwise book an asset without its payment.
"""

from __future__ import annotations

import logging
from typing import Any

import config
import userstore
from userstore import now_ms

log = logging.getLogger("portfolio")


class TradeError(ValueError):
    """A trade the user cannot make. The message is shown to them verbatim."""


# --- Reads -----------------------------------------------------------------


def account(user_id: int) -> dict[str, Any]:
    row = userstore.query_one(
        "SELECT cash, starting_capital FROM portfolios WHERE user_id = ?", (user_id,))
    if row:
        return row
    # A session can outlive its portfolio row only if something went wrong;
    # rebuild at the default rather than failing the request.
    capital = float(config.STARTING_CAPITAL)
    userstore.execute(
        "INSERT INTO portfolios (user_id, cash, starting_capital, created_ts) "
        "VALUES (?,?,?,?)", (user_id, capital, capital, now_ms()))
    return {"cash": capital, "starting_capital": capital}


def cash(user_id: int) -> float:
    return float(account(user_id)["cash"])


def starting_capital(user_id: int) -> float:
    return float(account(user_id)["starting_capital"])


def holdings(user_id: int) -> list[dict[str, Any]]:
    return userstore.query(
        "SELECT * FROM holdings WHERE user_id = ? AND qty > 0 ORDER BY symbol",
        (user_id,))


def holding_for(user_id: int, symbol: str) -> dict[str, Any] | None:
    return userstore.query_one(
        "SELECT * FROM holdings WHERE user_id = ? AND symbol = ? AND qty > 0",
        (user_id, symbol))


def trades(user_id: int, limit: int = 200) -> list[dict[str, Any]]:
    return userstore.query(
        "SELECT * FROM user_trades WHERE user_id = ? ORDER BY ts DESC LIMIT ?",
        (user_id, limit))


# --- Trading ---------------------------------------------------------------
#
# Both trade paths read cash and the holding *through the open transaction's
# cursor*, not through the module-level helpers above, and derive every written
# value from that read.
#
# Reading outside the transaction and writing an absolute value back is a lost
# update: two concurrent buys both observe the opening balance, both compute the
# same closing one, and the second silently overwrites the first -- one payment
# vanishes and the buyer keeps both assets. Measured at 24 concurrent $10 buys
# taking $110 instead of $240 out of cash. The same applies to `qty`, which is
# also written as a precomputed absolute.
#
# This was unreachable while every route was `async def` on a single event loop,
# because nothing interleaved. Moving the DB-touching routes into a threadpool
# made it reachable on the first concurrent request, so it is fixed here rather
# than left as a property of the old scheduling.


def _cash_in(cur: Any, user_id: int) -> float:
    """Cash balance read through an open transaction."""
    cur.execute(userstore.DIALECT.convert(
        "SELECT cash FROM portfolios WHERE user_id = ?"), (user_id,))
    row = cur.fetchone()
    return float(row[0]) if row else 0.0


def _holding_in(cur: Any, user_id: int, symbol: str) -> tuple[float, float] | None:
    """(qty, avg_cost) read through an open transaction, or None if not held."""
    cur.execute(userstore.DIALECT.convert(
        "SELECT qty, avg_cost FROM holdings "
        "WHERE user_id = ? AND symbol = ? AND qty > 0"), (user_id, symbol))
    row = cur.fetchone()
    return (float(row[0]), float(row[1])) if row else None


def buy(user_id: int, symbol: str, price: float, ts: int, *,
        usd: float | None = None, qty: float | None = None) -> dict[str, Any]:
    """Buy by dollar amount or quantity. Fees come out of cash on top of cost."""
    if symbol not in config.BY_SYMBOL:
        raise TradeError(f"Unknown symbol: {symbol}.")
    if price <= 0:
        raise TradeError("No live price is available for this asset yet. Please try again shortly.")

    if usd is not None:
        # The dollar amount is all-in (cost + fee), so "spend $1,000" removes
        # exactly $1,000 from cash rather than $1,000 plus fees.
        if usd <= 0:
            raise TradeError("The amount must be greater than zero.")
        gross = usd / (1 + config.FEE_RATE)
        qty = gross / price
    elif qty is not None:
        if qty <= 0:
            raise TradeError("The quantity must be greater than zero.")
        gross = qty * price
    else:
        raise TradeError("Specify either a dollar amount or a quantity.")

    fee = gross * config.FEE_RATE
    total = gross + fee

    # Make sure the row exists before opening the transaction: account() can
    # insert one and it runs its own transaction to do it.
    account(user_id)

    with userstore.tx() as cur:
        c = userstore.DIALECT.convert
        available = _cash_in(cur, user_id)

        # Absorb float dust so a "Max" button does not fail by a fraction of a cent.
        if total > available + 1e-6:
            raise TradeError(
                f"Insufficient cash: this order requires ${total:,.2f}, "
                f"but only ${available:,.2f} is available."
            )
        total = min(total, available)

        existing = _holding_in(cur, user_id, symbol)
        if existing:
            held_qty, held_cost = existing
            new_qty = held_qty + qty
            # Average cost includes fees, so realised P&L reflects what was paid.
            new_cost = (held_qty * held_cost + total) / new_qty
        else:
            new_qty, new_cost = qty, total / qty

        cur.execute(c(
            "INSERT INTO holdings (user_id, symbol, qty, avg_cost, updated_ts) "
            "VALUES (?,?,?,?,?) ON CONFLICT (user_id, symbol) DO UPDATE SET "
            "qty = excluded.qty, avg_cost = excluded.avg_cost, "
            "updated_ts = excluded.updated_ts"),
            (user_id, symbol, new_qty, new_cost, ts))
        cur.execute(c(
            "INSERT INTO user_trades (user_id, symbol, side, qty, price, value, fee, ts) "
            "VALUES (?,?,'BUY',?,?,?,?,?)"),
            (user_id, symbol, qty, price, gross, fee, ts))
        cur.execute(c("UPDATE portfolios SET cash = ? WHERE user_id = ?"),
                    (available - total, user_id))

    return {"symbol": symbol, "side": "BUY", "qty": qty, "price": price,
            "value": gross, "fee": fee, "cash": available - total}


def sell(user_id: int, symbol: str, price: float, ts: int, *,
         qty: float | None = None, fraction: float | None = None) -> dict[str, Any]:
    """Sell all or part of a holding. Realised P&L is booked against avg cost."""
    if price <= 0:
        raise TradeError("No live price is available for this asset yet. Please try again shortly.")

    account(user_id)

    with userstore.tx() as cur:
        c = userstore.DIALECT.convert
        existing = _holding_in(cur, user_id, symbol)
        if not existing:
            raise TradeError(f"You do not hold any {symbol}.")
        held_qty, held_cost = existing

        if fraction is not None:
            if not 0 < fraction <= 1:
                raise TradeError("The fraction must be between 0 and 1.")
            qty = held_qty * fraction
        if qty is None:
            raise TradeError("Specify either a quantity or a fraction.")
        if qty <= 0:
            raise TradeError("The quantity must be greater than zero.")

        # Tolerate float dust on a full sell rather than rejecting it.
        if qty > held_qty + 1e-9:
            raise TradeError(f"You only hold {held_qty:.8f} {symbol}.")
        qty = min(qty, held_qty)

        gross = qty * price
        fee = gross * config.FEE_RATE
        proceeds = gross - fee

        cost_basis = qty * held_cost
        pnl = proceeds - cost_basis
        pnl_pct = (pnl / cost_basis * 100) if cost_basis else 0.0

        remaining = held_qty - qty
        new_cash = _cash_in(cur, user_id) + proceeds

        if remaining <= 1e-12:
            cur.execute(c("DELETE FROM holdings WHERE user_id = ? AND symbol = ?"),
                        (user_id, symbol))
        else:
            cur.execute(c("UPDATE holdings SET qty = ?, updated_ts = ? "
                          "WHERE user_id = ? AND symbol = ?"),
                        (remaining, ts, user_id, symbol))
        cur.execute(c(
            "INSERT INTO user_trades (user_id, symbol, side, qty, price, value, fee, ts, pnl, pnl_pct) "
            "VALUES (?,?,'SELL',?,?,?,?,?,?,?)"),
            (user_id, symbol, qty, price, gross, fee, ts, pnl, pnl_pct))
        cur.execute(c("UPDATE portfolios SET cash = ? WHERE user_id = ?"),
                    (new_cash, user_id))

    return {"symbol": symbol, "side": "SELL", "qty": qty, "price": price,
            "value": gross, "fee": fee, "pnl": pnl, "pnl_pct": pnl_pct,
            "cash": new_cash}


# --- Reporting -------------------------------------------------------------


def snapshot(user_id: int, prices: dict[str, float]) -> dict[str, Any]:
    """Full portfolio view: holdings marked to market, plus headline stats."""
    acct = account(user_id)
    c = float(acct["cash"])
    capital = float(acct["starting_capital"])

    rows = []
    invested = 0.0
    market_value = 0.0
    for h in holdings(user_id):
        price = prices.get(h["symbol"])
        cost = h["qty"] * h["avg_cost"]
        value = h["qty"] * price if price else cost
        invested += cost
        market_value += value
        asset = config.BY_SYMBOL.get(h["symbol"])
        rows.append({
            "symbol": h["symbol"],
            "name": asset.name if asset else h["symbol"],
            "qty": h["qty"], "avg_cost": h["avg_cost"], "price": price,
            "cost_basis": cost, "value": value,
            "unrealized_pnl": value - cost,
            "unrealized_pct": ((value / cost - 1) * 100) if cost else 0.0,
            "updated_ts": h["updated_ts"],
        })
    rows.sort(key=lambda r: r["value"], reverse=True)

    # Aggregate in SQL: this runs per cycle per connected client, so pulling
    # every trade row into Python would scale with history times viewers.
    agg = userstore.query_one(
        "SELECT COUNT(*) AS trade_count, "
        "       COALESCE(SUM(fee), 0) AS fees_paid, "
        "       COALESCE(SUM(CASE WHEN side='SELL' THEN pnl END), 0) AS realized, "
        "       COALESCE(SUM(CASE WHEN side='SELL' THEN 1 ELSE 0 END), 0) AS closed_count, "
        "       COALESCE(SUM(CASE WHEN side='SELL' AND pnl > 0 THEN 1 ELSE 0 END), 0) AS wins "
        "FROM user_trades WHERE user_id = ?", (user_id,)) or {}

    closed = agg.get("closed_count") or 0
    total = c + market_value

    for r in rows:
        r["weight_pct"] = (r["value"] / total * 100) if total else 0.0

    return {
        "cash": c,
        "invested": invested,
        "market_value": market_value,
        "total": total,
        "starting_capital": capital,
        "total_return_pct": (total / capital - 1) * 100 if capital else 0.0,
        "total_pnl": total - capital,
        "unrealized_pnl": market_value - invested,
        "realized_pnl": agg.get("realized") or 0.0,
        "fees_paid": agg.get("fees_paid") or 0.0,
        "holdings": rows,
        "trades": trades(user_id, limit=200),
        "trade_count": agg.get("trade_count") or 0,
        "closed_count": closed,
        "win_rate": ((agg.get("wins") or 0) / closed * 100) if closed else 0.0,
        "fee_rate": config.FEE_RATE,
    }


def record_equity(user_id: int, ts: int, prices: dict[str, float]) -> None:
    s = snapshot(user_id, prices)
    with userstore.tx() as cur:
        cur.execute(userstore.DIALECT.convert(
            "INSERT INTO user_equity (user_id, ts, cash, invested, market_value, "
            "total, realized, fees) VALUES (?,?,?,?,?,?,?,?) "
            "ON CONFLICT (user_id, ts) DO UPDATE SET "
            "cash = excluded.cash, invested = excluded.invested, "
            "market_value = excluded.market_value, total = excluded.total, "
            "realized = excluded.realized, fees = excluded.fees"),
            (user_id, ts, s["cash"], s["invested"], s["market_value"],
             s["total"], s["realized_pnl"], s["fees_paid"]))


def record_equity_all(ts: int, prices: dict[str, float]) -> int:
    """Append one equity point for every portfolio, in a single pass.

    Called once per engine cycle. Doing it with a snapshot() per user would run
    several queries each and scale badly with player count, so holdings and
    realised totals are fetched in bulk and joined in memory. Returns how many
    rows were written.

    Portfolios that hold nothing and have never traded are skipped. Their curve
    is flat at the starting balance and `_seed_portfolio` already recorded that
    point, so a row per cycle would restate a constant. This matters because a
    guest row is created for every cookie-less visitor: without the filter,
    every abandoned page load bought itself 1,440 identical rows a day, for as
    long as the deployment lived.
    """
    accts = userstore.query(
        "SELECT p.user_id, p.cash FROM portfolios p "
        "WHERE EXISTS (SELECT 1 FROM holdings h "
        "              WHERE h.user_id = p.user_id AND h.qty > 0) "
        "   OR EXISTS (SELECT 1 FROM user_trades t WHERE t.user_id = p.user_id)")
    if not accts:
        return 0

    by_user: dict[int, list[dict[str, Any]]] = {}
    for h in userstore.query(
            "SELECT user_id, symbol, qty, avg_cost FROM holdings WHERE qty > 0"):
        by_user.setdefault(h["user_id"], []).append(h)

    totals = {
        r["user_id"]: r for r in userstore.query(
            "SELECT user_id, "
            "       COALESCE(SUM(fee), 0) AS fees, "
            "       COALESCE(SUM(CASE WHEN side='SELL' THEN pnl END), 0) AS realized "
            "FROM user_trades GROUP BY user_id")
    }

    rows = []
    for a in accts:
        uid = a["user_id"]
        invested = market_value = 0.0
        for h in by_user.get(uid, []):
            cost = h["qty"] * h["avg_cost"]
            price = prices.get(h["symbol"])
            invested += cost
            market_value += h["qty"] * price if price else cost
        cash_ = float(a["cash"])
        t = totals.get(uid, {})
        rows.append((uid, ts, cash_, invested, market_value,
                     cash_ + market_value,
                     t.get("realized") or 0.0, t.get("fees") or 0.0))

    with userstore.tx() as cur:
        stmt = userstore.DIALECT.convert(
            "INSERT INTO user_equity (user_id, ts, cash, invested, market_value, "
            "total, realized, fees) VALUES (?,?,?,?,?,?,?,?) "
            "ON CONFLICT (user_id, ts) DO UPDATE SET "
            "cash = excluded.cash, invested = excluded.invested, "
            "market_value = excluded.market_value, total = excluded.total, "
            "realized = excluded.realized, fees = excluded.fees")
        cur.executemany(stmt, rows)
    return len(rows)


def equity_series(user_id: int, since: int | None, max_points: int = 360) -> list[dict[str, Any]]:
    """Equity rows since `since`, downsampled by keeping the last row per time
    bucket. Last-in-bucket is right for a value curve; averaging would smooth
    away the very moves the chart exists to show."""
    if since is None:
        first = userstore.query_one(
            "SELECT MIN(ts) AS t FROM user_equity WHERE user_id = ?", (user_id,))
        since = (first or {}).get("t")
        if since is None:
            # No history at all. Falling through with since = 0 would size the
            # bucket off the whole epoch -- ~57 days per bucket -- which is
            # nonsense for the next caller that does have rows in range.
            return []
    span = max(now_ms() - since, 1)
    bucket = max(60_000, span // max_points)
    return userstore.query(
        "SELECT * FROM user_equity WHERE user_id = ? AND ts IN ("
        "  SELECT MAX(ts) FROM user_equity WHERE user_id = ? AND ts >= ? "
        "  GROUP BY ts / ?"
        ") ORDER BY ts",
        (user_id, user_id, since, bucket))


def reset(user_id: int, capital: float | None = None) -> None:
    """Clear holdings, trades and history, and restart from `capital`."""
    if capital is None:
        capital = starting_capital(user_id)
    ts = now_ms()
    with userstore.tx() as cur:
        c = userstore.DIALECT.convert
        cur.execute(c("DELETE FROM holdings WHERE user_id = ?"), (user_id,))
        cur.execute(c("DELETE FROM user_trades WHERE user_id = ?"), (user_id,))
        cur.execute(c("DELETE FROM user_equity WHERE user_id = ?"), (user_id,))
        cur.execute(c("UPDATE portfolios SET cash = ?, starting_capital = ? "
                      "WHERE user_id = ?"), (capital, capital, user_id))
        cur.execute(c(
            "INSERT INTO user_equity (user_id, ts, cash, invested, market_value, "
            "total, realized, fees) VALUES (?,?,?,?,?,?,?,?)"),
            (user_id, ts, capital, 0.0, 0.0, capital, 0.0, 0.0))


# --- Leaderboard -----------------------------------------------------------


def leaderboard(prices: dict[str, float], limit: int = 100) -> list[dict[str, Any]]:
    """Every registered player ranked by total return.

    Marked to live prices at read time rather than from stored equity rows, so
    the ranking reflects the current market for everyone simultaneously --
    otherwise whoever traded most recently would have the freshest valuation.
    Guests are excluded: they are anonymous and would clutter the board.
    Administrators are excluded too: the master account exists to run the game,
    and ensure_master() hands it a portfolio like anyone else, so without this
    the operator shows up ranked against the players they administer.
    """
    rows = userstore.query(
        "SELECT u.id, u.display_name, p.cash, p.starting_capital "
        "FROM users u JOIN portfolios p ON p.user_id = u.id "
        "WHERE u.is_guest = 0 AND u.is_admin = 0")
    if not rows:
        return []

    all_holdings = userstore.query(
        "SELECT h.user_id, h.symbol, h.qty, h.avg_cost FROM holdings h "
        "JOIN users u ON u.id = h.user_id "
        "WHERE u.is_guest = 0 AND u.is_admin = 0 AND h.qty > 0")
    by_user: dict[int, list[dict[str, Any]]] = {}
    for h in all_holdings:
        by_user.setdefault(h["user_id"], []).append(h)

    counts = {
        r["user_id"]: r["n"] for r in userstore.query(
            "SELECT user_id, COUNT(*) AS n FROM user_trades GROUP BY user_id")
    }

    out = []
    for r in rows:
        mv = 0.0
        for h in by_user.get(r["id"], []):
            price = prices.get(h["symbol"])
            mv += h["qty"] * price if price else h["qty"] * h["avg_cost"]
        capital = float(r["starting_capital"]) or 1.0
        total = float(r["cash"]) + mv
        out.append({
            "user_id": r["id"],
            "name": r["display_name"],
            "total": total,
            # Published alongside the total so the board can show what each
            # player actually chose to start with. Ranking is by percentage
            # return, but a bare total still reads as a score, and someone who
            # opened with $10M looks like they are winning while flat.
            "starting_capital": float(r["starting_capital"]),
            # Money made or lost, which is the honest currency figure: it is
            # zero for an untouched account no matter how large that account is.
            "pnl": total - float(r["starting_capital"]),
            "return_pct": (total / capital - 1) * 100,
            "trade_count": counts.get(r["id"], 0),
        })

    out.sort(key=lambda x: x["return_pct"], reverse=True)
    for i, row in enumerate(out, start=1):
        row["rank"] = i
    return out[:limit]
