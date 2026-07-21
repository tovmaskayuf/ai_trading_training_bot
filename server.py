"""FastAPI app: JSON API + SSE stream + the dashboard.

Runs the polling engine as a background task inside the same process, so a
single `uvicorn server:app` gives you both the always-on data engine and the
web interface.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
from typing import Any

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

import accounts
import config
import db
import engine
import portfolio as pf
import providers
from providers import coingecko
import settings
import userstore
from analytics import rating
from trading import manual

SESSION_COOKIE = "tt_session"

log = logging.getLogger("server")

# Chart ranges, in milliseconds. None means "everything we have".
RANGES: dict[str, int | None] = {
    "1h": 3_600_000,
    "24h": 86_400_000,
    "7d": 604_800_000,
    "30d": 2_592_000_000,
    "1y": 31_536_000_000,
    "all": None,
}

_engine_task: asyncio.Task | None = None


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    global _engine_task
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)-12s %(message)s",
        datefmt="%H:%M:%S",
    )
    # Quiet httpx's per-request logging; the engine already logs each cycle.
    logging.getLogger("httpx").setLevel(logging.WARNING)

    db.connect()
    _engine_task = asyncio.create_task(engine.loop())
    log.info("engine started (cycle=%ds)", config.CYCLE_SECONDS)
    try:
        yield
    finally:
        if _engine_task:
            _engine_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await _engine_task
        await providers.aclose()


app = FastAPI(title="AI Crypto Trading and Training by TT", lifespan=lifespan)


def _current_prices() -> dict[str, float]:
    out: dict[str, float] = {}
    for symbol in config.SYMBOLS:
        asset = engine.STATE["assets"].get(symbol)
        if asset and asset.get("price"):
            out[symbol] = asset["price"]
        else:
            snap = db.latest_snapshot(symbol)
            if snap and snap.get("price"):
                out[symbol] = snap["price"]
    return out


# --- Identity --------------------------------------------------------------


def _set_session(response: Response, token: str) -> None:
    response.set_cookie(
        SESSION_COOKIE, token,
        max_age=userstore.SESSION_DAYS * 86_400,
        httponly=True,        # not readable from JS, so XSS cannot lift it
        samesite="lax",
        # Render terminates TLS so production is always HTTPS, but marking the
        # cookie secure over plain http would stop it being set at all locally.
        secure=not _is_local(),
        path="/",
    )


def _is_local() -> bool:
    return not os.getenv("RENDER") and not os.getenv("DATABASE_URL")


def current_user(request: Request) -> dict[str, Any] | None:
    """The signed-in or guest user for this request, or None if neither."""
    return userstore.user_for_session(request.cookies.get(SESSION_COOKIE))


def require_user(request: Request, response: Response) -> dict[str, Any]:
    """Resolve the caller, creating a guest on first visit.

    Visitors trade before they ever sign up, so identity has to exist from the
    first request rather than starting at a login wall.
    """
    user = current_user(request)
    if user:
        return user
    guest = accounts.create_guest(settings.get()["starting_capital"])
    _set_session(response, userstore.new_session(guest["id"]))
    return guest


def _range_cutoff(range_key: str) -> int | None:
    if range_key not in RANGES:
        raise HTTPException(400, f"Unknown range: {range_key}. "
                                 f"Valid ranges: {', '.join(RANGES)}.")
    span = RANGES[range_key]
    return None if span is None else db.now_ms() - span


# --- Settings --------------------------------------------------------------


@app.get("/api/settings")
async def get_settings() -> dict[str, Any]:
    return {
        **settings.get(),
        "all_assets": [
            {"symbol": a.symbol, "name": a.name, "thesis": a.thesis}
            for a in config.ASSETS
        ],
        "capital_min": config.CAPITAL_MIN,
        "capital_max": config.CAPITAL_MAX,
        "languages": list(config.SUPPORTED_LANGUAGES),
    }


@app.post("/api/settings")
async def save_settings(payload: dict[str, Any], request: Request,
                        response: Response) -> dict[str, Any]:
    """Apply start-screen choices. Changing the starting capital resets the
    caller's own portfolio to the new amount; language and asset selection
    never do."""
    user = require_user(request, response)
    before = settings.get()
    try:
        after = settings.save(payload)
    except ValueError as e:
        raise HTTPException(400, str(e)) from None

    capital_changed = (
        "starting_capital" in payload
        and float(after["starting_capital"]) != float(before["starting_capital"])
    )
    if capital_changed or not before["initialized"]:
        pf.reset(user["id"], after["starting_capital"])

    return {"ok": True, "settings": after, "portfolio_reset": capital_changed}


# --- Market data -----------------------------------------------------------


@app.get("/api/overview")
async def overview(request: Request, response: Response) -> dict[str, Any]:
    """Everything the main screen needs in one call."""
    user = require_user(request, response)
    held_now = {h["symbol"] for h in pf.holdings(user["id"])}
    assets = engine.STATE.get("assets") or {}
    prefs = settings.get()

    # Before the first cycle completes, serve the last persisted state so the
    # dashboard renders immediately on a restart instead of sitting empty.
    if not assets:
        followed = set(prefs["followed"])
        assets = {}
        for symbol in config.SYMBOLS:
            snap = db.latest_snapshot(symbol) or {}
            rt = db.latest_rating(symbol) or {}
            asset = config.BY_SYMBOL[symbol]
            candles = db.get_candles(symbol, limit=48)
            assets[symbol] = {
                "symbol": symbol, "name": asset.name, "thesis": asset.thesis,
                "source": asset.price_source,
                "followed": symbol in followed,
                "held": symbol in held_now,
                "spark": [c["c"] for c in candles],
                "price": snap.get("price"), "chg_24h": snap.get("chg_24h"),
                "mcap": snap.get("mcap"), "rank": snap.get("rank"),
                "stale": bool(snap.get("stale")),
                "momentum": rt.get("momentum"), "risk": rt.get("risk"),
                "structure": rt.get("structure"), "relative": rt.get("relative"),
                "composite": rt.get("composite"), "grade": rt.get("grade"),
                "signal": rt.get("signal"), "detail": {},
            }

    # Stamp follow/held flags from live state at read time -- the engine only
    # refreshes its copy once per cycle, and a settings change or a trade
    # should reflect immediately, not up to a minute later.
    followed_now = set(prefs["followed"])
    asset_rows = [
        {**a,
         "followed": a["symbol"] in followed_now,
         "held": a["symbol"] in held_now}
        for a in assets.values()
    ]

    return {
        "assets": asset_rows,
        "cycle": engine.STATE.get("cycle", 0),
        "updated_at": engine.STATE.get("updated_at"),
        "errors": engine.STATE.get("errors", []),
        "weights": config.DEFAULT_WEIGHTS,
        "thresholds": {
            "buy": config.BUY_THRESHOLD,
            "strong_buy": config.STRONG_BUY_THRESHOLD,
            "exit": config.EXIT_THRESHOLD,
            "strong_sell": config.STRONG_SELL_THRESHOLD,
        },
        "cycle_seconds": config.CYCLE_SECONDS,
        "settings": prefs,
    }


@app.get("/api/asset/{symbol}")
async def asset_detail(symbol: str, request: Request,
                       response: Response) -> dict[str, Any]:
    symbol = symbol.upper()
    if symbol not in config.BY_SYMBOL:
        raise HTTPException(404, f"Unknown symbol: {symbol}.")

    user = require_user(request, response)
    asset = config.BY_SYMBOL[symbol]
    live = (engine.STATE.get("assets") or {}).get(symbol, {})

    return {
        "symbol": symbol,
        "name": asset.name,
        "thesis": asset.thesis,
        "source": asset.price_source,
        "live": live,
        "detail": live.get("detail", {}),
        "holding": pf.holding_for(user["id"], symbol),
    }


@app.get("/api/asset/{symbol}/prices")
async def asset_prices(symbol: str, range: str = "24h") -> dict[str, Any]:
    """Price series for the drawer chart, resolution matched to the range:
    per-minute snapshots for 1H, hourly candles up to a week, daily beyond."""
    symbol = symbol.upper()
    if symbol not in config.BY_SYMBOL:
        raise HTTPException(404, f"Unknown symbol: {symbol}.")
    cutoff = _range_cutoff(range)

    points: list[dict[str, Any]]
    if range == "1h":
        rows = db.query(
            "SELECT ts AS t, price AS p FROM snapshots "
            "WHERE symbol=? AND ts>=? AND price IS NOT NULL ORDER BY ts",
            (symbol, cutoff),
        )
        points = rows
    else:
        interval = config.CANDLE_INTERVAL if range in ("24h", "7d") else config.DAILY_INTERVAL
        if cutoff is None:
            rows = db.query(
                "SELECT open_time AS t, c AS p FROM candles "
                "WHERE symbol=? AND interval=? ORDER BY open_time",
                (symbol, interval),
            )
        else:
            rows = db.query(
                "SELECT open_time AS t, c AS p FROM candles "
                "WHERE symbol=? AND interval=? AND open_time>=? ORDER BY open_time",
                (symbol, interval, cutoff),
            )
        points = rows

    # Append the live price so the line always ends at "now".
    live = _current_prices().get(symbol)
    if live and points:
        points = points + [{"t": db.now_ms(), "p": live}]

    vals = [p["p"] for p in points]
    return {
        "symbol": symbol,
        "range": range,
        "points": points,
        "high": max(vals) if vals else None,
        "low": min(vals) if vals else None,
        "change_pct": ((vals[-1] / vals[0] - 1) * 100) if len(vals) >= 2 and vals[0] else None,
    }


@app.get("/api/asset/{symbol}/ratings")
async def asset_ratings(symbol: str, range: str = "24h") -> dict[str, Any]:
    """Composite-score history, downsampled to chart resolution. Ratings are
    retained for 90 days, so year-scale ranges show what exists."""
    symbol = symbol.upper()
    if symbol not in config.BY_SYMBOL:
        raise HTTPException(404, f"Unknown symbol: {symbol}.")
    cutoff = _range_cutoff(range)
    if cutoff is None:
        first = db.query_one(
            "SELECT MIN(ts) AS t FROM ratings WHERE symbol=?", (symbol,))
        cutoff = (first or {}).get("t") or 0

    span = max(db.now_ms() - cutoff, 1)
    bucket = max(60_000, span // 360)
    rows = db.query(
        "SELECT ts AS t, composite AS v FROM ratings "
        "WHERE symbol=? AND ts IN ("
        "  SELECT MAX(ts) FROM ratings WHERE symbol=? AND ts>=? GROUP BY ts / ?"
        ") ORDER BY ts",
        (symbol, symbol, cutoff, bucket),
    )
    return {"symbol": symbol, "range": range, "points": rows}


# --- Portfolio -------------------------------------------------------------


# --- Accounts --------------------------------------------------------------


def _public_user(user: dict[str, Any]) -> dict[str, Any]:
    """What the client is allowed to see. Never the hash, never the session."""
    return {
        "id": user["id"],
        "name": user["display_name"],
        "username": None if user.get("is_guest") else user["username"],
        "is_guest": bool(user.get("is_guest")),
    }


@app.get("/api/me")
async def me(request: Request, response: Response) -> dict[str, Any]:
    return {"user": _public_user(require_user(request, response))}


@app.post("/api/auth/signup")
async def signup(payload: dict[str, Any], request: Request,
                 response: Response) -> dict[str, Any]:
    """Create an account. A guest keeps the portfolio it already built."""
    username = str(payload.get("username", ""))
    password = str(payload.get("password", ""))
    existing = current_user(request)

    try:
        if existing and existing.get("is_guest"):
            user = accounts.claim_account(existing["id"], username, password)
        else:
            user = accounts.create_user(
                username, password, settings.get()["starting_capital"])
            _set_session(response, userstore.new_session(user["id"]))
    except accounts.AuthError as e:
        raise HTTPException(400, str(e)) from None

    return {"ok": True, "user": _public_user({**user, "display_name": user["display_name"]})}


@app.post("/api/auth/login")
async def login(payload: dict[str, Any], request: Request,
                response: Response) -> dict[str, Any]:
    try:
        user = accounts.authenticate(
            str(payload.get("username", "")), str(payload.get("password", "")))
    except accounts.AuthError as e:
        raise HTTPException(401, str(e)) from None

    # Drop the guest session rather than leaving it resolvable.
    userstore.end_session(request.cookies.get(SESSION_COOKIE))
    _set_session(response, userstore.new_session(user["id"]))
    return {"ok": True, "user": _public_user({**user, "is_guest": False})}


@app.post("/api/auth/logout")
async def logout(request: Request, response: Response) -> dict[str, Any]:
    userstore.end_session(request.cookies.get(SESSION_COOKIE))
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"ok": True}


@app.get("/api/leaderboard")
async def leaderboard(request: Request, response: Response,
                      limit: int = 100) -> dict[str, Any]:
    """Global standings. Everyone is marked to the same prices at read time."""
    user = require_user(request, response)
    rows = pf.leaderboard(_current_prices(), limit=max(1, min(limit, 500)))
    mine = next((r for r in rows if r["user_id"] == user["id"]), None)
    return {
        "rows": rows,
        "you": mine,
        "your_id": user["id"],
        "is_guest": bool(user.get("is_guest")),
        "total_players": len(rows),
    }


# --- Portfolio -------------------------------------------------------------


@app.get("/api/manual")
async def manual_view(request: Request, response: Response) -> dict[str, Any]:
    """The caller's own portfolio, marked to the latest prices."""
    user = require_user(request, response)
    return {**pf.snapshot(user["id"], _current_prices()),
            "user": _public_user(user)}


@app.get("/api/manual/history")
async def manual_history(request: Request, response: Response,
                         range: str = "24h") -> dict[str, Any]:
    """Portfolio value over time. Returns every metric column; the client
    chooses which one to chart."""
    user = require_user(request, response)
    cutoff = _range_cutoff(range)
    return {"range": range, "points": pf.equity_series(user["id"], cutoff)}


@app.post("/api/manual/trade")
async def manual_trade(payload: dict[str, Any], request: Request,
                       response: Response) -> dict[str, Any]:
    """Execute a simulated buy or sell at the current live price.

    Price is taken server-side rather than from the client, so a stale page
    cannot fill at an old quote.
    """
    user = require_user(request, response)
    symbol = str(payload.get("symbol", "")).upper()
    side = str(payload.get("side", "")).upper()

    if symbol not in config.BY_SYMBOL:
        raise HTTPException(400, f"Unknown symbol: {symbol}.")
    if side not in ("BUY", "SELL"):
        raise HTTPException(400, "The side must be either BUY or SELL.")
    if symbol not in settings.get()["followed"]:
        raise HTTPException(
            400, f"{symbol} is not in your followed assets. "
                 "You can update your selection on the start screen.")

    price = _current_prices().get(symbol)
    if not price:
        raise HTTPException(
            409, f"No live price is available for {symbol} yet. Please try again shortly.")

    ts = db.now_ms()
    try:
        if side == "BUY":
            result = pf.buy(
                user["id"], symbol, price, ts,
                usd=_opt_float(payload, "usd"),
                qty=_opt_float(payload, "qty"),
            )
        else:
            result = pf.sell(
                user["id"], symbol, price, ts,
                qty=_opt_float(payload, "qty"),
                fraction=_opt_float(payload, "fraction"),
            )
    except pf.TradeError as e:
        raise HTTPException(400, str(e)) from None

    # Record the post-trade value immediately so charts show the step.
    prices = _current_prices()
    pf.record_equity(user["id"], db.now_ms(), prices)

    return {"ok": True, "trade": result,
            "portfolio": {**pf.snapshot(user["id"], prices),
                          "user": _public_user(user)}}


def _opt_float(payload: dict[str, Any], key: str) -> float | None:
    v = payload.get(key)
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        raise HTTPException(400, f"The field '{key}' must be a number.") from None


@app.post("/api/manual/reset")
async def manual_reset(request: Request, response: Response) -> dict[str, Any]:
    """Reset only the caller's own portfolio.

    This used to reset the single shared portfolio for everyone, with no
    authentication -- any visitor could wipe the board.
    """
    user = require_user(request, response)
    pf.reset(user["id"])
    return {"status": "reset", "capital": pf.starting_capital(user["id"])}


# --- Ratings utilities -----------------------------------------------------


@app.post("/api/weights")
async def rescore(payload: dict[str, float]) -> dict[str, Any]:
    """Recompute composites under caller-supplied axis weights.

    The dashboard does this client-side for instant feedback; this endpoint
    exists for API consumers and to keep the two implementations honest.
    """
    weights = {k: float(payload.get(k, 0)) for k in config.DEFAULT_WEIGHTS}
    total = sum(weights.values())
    if total <= 0:
        raise HTTPException(400, "Weights must sum to more than zero.")
    weights = {k: v / total for k, v in weights.items()}

    out = []
    for symbol, a in (engine.STATE.get("assets") or {}).items():
        sub = {k: a.get(k) for k in config.DEFAULT_WEIGHTS}
        composite = rating.composite_score(sub, weights)
        out.append({
            "symbol": symbol,
            "composite": round(composite, 2) if composite is not None else None,
            "grade": rating.grade_for(composite) if composite is not None else "-",
        })
    out.sort(key=lambda r: r["composite"] or 0, reverse=True)
    return {"weights": weights, "assets": out}


# --- Infrastructure --------------------------------------------------------


@app.get("/api/stream")
async def stream(request: Request) -> StreamingResponse:
    """Server-sent events: one message per completed engine cycle."""
    async def gen():
        q = engine.subscribe()
        try:
            # Tell a reconnecting client where things stand immediately.
            yield f"data: {json.dumps({'cycle': engine.STATE.get('cycle', 0)})}\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    cycle = await asyncio.wait_for(q.get(), timeout=30.0)
                    payload = {
                        "cycle": cycle,
                        "updated_at": engine.STATE.get("updated_at"),
                    }
                    yield f"data: {json.dumps(payload)}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            engine.unsubscribe(q)

    return StreamingResponse(gen(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",
    })


@app.get("/api/health")
async def health() -> dict[str, Any]:
    return {
        "running": engine.STATE.get("running", False),
        "cycle": engine.STATE.get("cycle", 0),
        "updated_at": engine.STATE.get("updated_at"),
        "errors": engine.STATE.get("errors", []),
        "assets_tracked": len(config.SYMBOLS),
        # Auth mode only -- never the key or any part of it. Exposed because a
        # key that never reached the process is otherwise invisible: keyless
        # calls are throttled only intermittently, so "no errors" looks
        # identical whether the key loaded or not. (A *wrong* key is loud --
        # CoinGecko 401s with error_code 10002 -- it is a *missing* one that
        # hides.)
        "coingecko_auth": coingecko.auth_mode(),
        # Candle failures do not stop a cycle, but they empty three of the four
        # rating axes, so they need to be visible from outside the log.
        "candle_errors": engine.STATE.get("_candle_errors", []),
        "candles_stored": db.query_one(
            "SELECT COUNT(*) AS n FROM candles WHERE interval=?",
            (config.CANDLE_INTERVAL,))["n"],
    }


# --- Dashboard -------------------------------------------------------------

if config.STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=config.STATIC_DIR), name="static")


@app.get("/")
async def index() -> FileResponse:
    path = config.STATIC_DIR / "dashboard.html"
    if not path.exists():
        raise HTTPException(404, "dashboard.html not found")
    # The whole app is this one file, so a cached copy pins the user to an old
    # build across deploys -- including a broken one, which is exactly how a bad
    # release survived being reverted. Without Cache-Control browsers apply
    # their own heuristic freshness and can hold it for hours. "no-cache" still
    # allows the etag revalidation below, so an unchanged file costs a 304
    # rather than a re-download.
    return FileResponse(path, headers={"Cache-Control": "no-cache, must-revalidate"})
