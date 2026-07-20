"""The polling engine.

One asyncio loop drives everything on a 60-second tick: fetch -> persist ->
rate -> record portfolio value. A failure in any single provider degrades that
cycle rather than killing the service, because this is expected to run
unattended for weeks.

There is no automated trading here. The engine's job is data and ratings; all
trading decisions belong to the user.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Callable

import config
import db
import portfolio
import providers
import settings
import userstore
from analytics import indicators as ind
from analytics import rating
from providers import coingecko

# Recording equity is one bulk pass, but it still grows with player count.
# Warn rather than fail so the limit is visible before it becomes a stall.
EQUITY_SLOW_WARN = 500

log = logging.getLogger("engine")

# In-memory view of the latest cycle, served directly to the dashboard so a
# page load never has to recompute anything.
STATE: dict[str, Any] = {
    "cycle": 0,
    "updated_at": None,
    "assets": {},
    "errors": [],
    "running": False,
}

_subscribers: list[asyncio.Queue] = []


def subscribe() -> asyncio.Queue:
    q: asyncio.Queue = asyncio.Queue(maxsize=8)
    _subscribers.append(q)
    return q


def unsubscribe(q: asyncio.Queue) -> None:
    if q in _subscribers:
        _subscribers.remove(q)


def _publish() -> None:
    """Push the cycle marker to SSE listeners; drop for slow consumers."""
    for q in list(_subscribers):
        try:
            q.put_nowait(STATE["cycle"])
        except asyncio.QueueFull:
            pass


def _candles_as_dicts(rows: list[tuple]) -> list[dict]:
    return [dict(zip(("open_time", "o", "h", "l", "c", "v"), r)) for r in rows]


CANDLE_CONCURRENCY = 5


async def refresh_candles(interval: str = config.CANDLE_INTERVAL,
                          limit: int = config.CANDLE_LIMIT) -> dict[str, list[dict]]:
    """Pull fresh OHLC for every asset at the given interval and persist it.

    Fetched concurrently: serially this was 15 round trips per interval, and on
    a cold start (ephemeral disk, so every restart) that ran twice before the
    first cycle could publish anything, leaving the dashboard empty for minutes
    on a small instance. The semaphore keeps us from opening 15 sockets at once
    against the same host.
    """
    sem = asyncio.Semaphore(CANDLE_CONCURRENCY)

    async def one(symbol: str) -> tuple[str, list[dict]] | None:
        async with sem:
            try:
                rows = await providers.fetch_candles(symbol, interval=interval, limit=limit)
            except Exception as e:
                log.warning("candle refresh (%s) failed for %s: %s", interval, symbol, e)
                return None
        if not rows:
            return None
        # DB write stays outside the semaphore-held network section but on the
        # loop thread, so it remains serialised with every other writer.
        db.upsert_candles(symbol, interval, rows)
        return symbol, _candles_as_dicts(rows)

    results = await asyncio.gather(*(one(s) for s in config.SYMBOLS))
    return {sym: rows for r in results if r for sym, rows in (r,)}


def load_candles() -> dict[str, list[dict]]:
    """Read hourly candles back out of the database."""
    return {s: db.get_candles(s) for s in config.SYMBOLS}


async def run_cycle(cycle: int) -> None:
    ts = db.now_ms()
    errors: list[str] = []
    prefs = settings.get()
    followed = set(prefs["followed"])

    # --- Fetch -------------------------------------------------------------
    try:
        prices = await providers.fetch_prices()
    except Exception as e:
        log.error("price fetch failed entirely: %s", e)
        STATE["errors"] = [f"Price fetch failed: {e}"]
        return

    if cycle % config.KLINE_EVERY_N_CYCLES == 0:
        candles = await refresh_candles()
        if len(candles) < len(config.SYMBOLS):
            candles = {**load_candles(), **candles}
    else:
        candles = load_candles()

    if cycle % config.DAILY_EVERY_N_CYCLES == 0:
        await refresh_candles(config.DAILY_INTERVAL, config.DAILY_LIMIT)

    refresh_market = cycle % config.MARKET_EVERY_N_CYCLES == 0
    market: dict[str, Any] = STATE.get("_market") or {}
    if refresh_market or not market:
        try:
            market = await providers.fetch_market()
            STATE["_market"] = market
        except Exception as e:
            errors.append(f"Market data: {e}")
            log.warning("market fetch failed: %s", e)

    book: dict[str, Any] = STATE.get("_book") or {}
    if refresh_market or not book:
        try:
            book = await providers.fetch_book()
            STATE["_book"] = book
        except Exception as e:
            errors.append(f"Order book: {e}")

    # --- Persist snapshots -------------------------------------------------
    for symbol in config.SYMBOLS:
        p = prices.get(symbol)
        if not p:
            continue
        m = market.get(symbol, {})
        chg = p.get("chg_24h")
        # Hyperliquid's allMids gives price only, so backfill 24h change from
        # CoinGecko (or from candles) rather than showing a blank cell.
        if chg is None:
            chg = m.get("chg_24h")
        if chg is None and symbol in candles and len(candles[symbol]) > 24:
            closes = [c["c"] for c in candles[symbol]]
            chg = ind.pct_change(closes, 24)

        db.insert_snapshot(
            symbol, ts, p.get("price"), chg,
            p.get("quote_volume") or m.get("volume_24h_usd"),
            m.get("mcap"), m.get("rank"), bool(p.get("stale")),
        )

    # --- Rate --------------------------------------------------------------
    baskets = rating.build_baskets(candles, market)
    assets_view: dict[str, Any] = {}

    for symbol in config.SYMBOLS:
        cs = candles.get(symbol, [])
        prev = db.latest_rating(symbol)
        # Ratings are shared by every viewer, so they cannot depend on whose
        # holdings these are. The signal is now the market view; each request
        # stamps that caller's own `held` flag in /api/overview.
        holding = False
        try:
            r = rating.rate_asset(
                symbol, cs, market.get(symbol, {}), book.get(symbol),
                baskets["risk"], baskets["structure"], baskets["returns"],
                baskets["benchmark"],
                prev_signal=(prev or {}).get("signal"), holding=holding,
            )
        except Exception as e:
            log.exception("rating failed for %s", symbol)
            errors.append(f"Rating {symbol}: {e}")
            continue

        db.insert_rating(symbol, ts, r)

        p = prices.get(symbol, {})
        asset = config.BY_SYMBOL[symbol]
        assets_view[symbol] = {
            "symbol": symbol,
            "name": asset.name,
            "thesis": asset.thesis,
            "source": asset.price_source,
            "followed": symbol in followed,
            # Placeholder: re-stamped per caller at read time.
            "held": False,
            # Last 48h of closes, for the inline sparkline in the grid.
            "spark": [round(c["c"], 8) for c in cs[-48:]],
            "price": p.get("price"),
            "chg_24h": p.get("chg_24h") or market.get(symbol, {}).get("chg_24h"),
            "mcap": market.get(symbol, {}).get("mcap"),
            "rank": market.get(symbol, {}).get("rank"),
            "stale": bool(p.get("stale")),
            **{k: r.get(k) for k in
               ("momentum", "risk", "structure", "relative",
                "composite", "grade", "signal")},
            "detail": r.get("detail", {}),
        }

    # --- Record portfolio value --------------------------------------------
    price_map = {s: p["price"] for s, p in prices.items() if p.get("price")}
    try:
        n = portfolio.record_equity_all(ts, price_map)
        if n > EQUITY_SLOW_WARN:
            log.warning("recorded equity for %d portfolios; consider moving "
                        "this off the cycle if it keeps growing", n)
    except Exception as e:
        log.exception("equity recording failed")
        errors.append(f"Equity history: {e}")

    # --- Publish -----------------------------------------------------------
    STATE.update({
        "cycle": cycle,
        "updated_at": ts,
        "assets": assets_view,
        "errors": errors,
        "running": True,
    })
    _publish()

    log.info("cycle %d: %d assets rated%s",
             cycle, len(assets_view),
             f", {len(errors)} errors" if errors else "")


async def loop(stop: Callable[[], bool] | None = None) -> None:
    """Run until `stop()` returns True (or forever)."""
    db.connect()
    db.prune()
    userstore.connect()  # create the account/portfolio schema on first run

    # Logged because a key that failed to load looks exactly like an ordinary
    # rate limit from the outside -- 429s with no hint that auth never applied.
    log.info("coingecko auth: %s", coingecko.key_status())

    # Reported before the first cycle finishes: the engine really is up, and a
    # health check that says otherwise for the whole warm-up is just wrong.
    # There was no separate bootstrap pass here; cycle 0 already refreshes both
    # candle intervals (0 % KLINE_EVERY == 0 and 0 % DAILY_EVERY == 0), so
    # backfilling first only did the same ~30 fetches twice.
    STATE["running"] = True

    cycle = 0
    while not (stop and stop()):
        started = time.monotonic()
        try:
            await run_cycle(cycle)
        except Exception:
            log.exception("cycle %d failed", cycle)
            STATE["errors"] = ["Cycle failed; see the server log."]

        cycle += 1
        elapsed = time.monotonic() - started
        await asyncio.sleep(max(1.0, config.CYCLE_SECONDS - elapsed))


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)-12s %(message)s",
        datefmt="%H:%M:%S",
    )
    try:
        await loop()
    finally:
        await providers.aclose()


if __name__ == "__main__":
    asyncio.run(main())
