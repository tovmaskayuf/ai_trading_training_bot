# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

**AI Crypto Trading and Training by TT** — a paper-trading trainer for 15
cryptocurrencies. A 60-second polling engine collects live prices and computes
a four-axis AI rating per asset; the user trades a virtual portfolio by hand
through a Polymarket-styled web UI with an animated start screen, four
languages, and time-range charts everywhere.

**There is no automated trading.** An earlier iteration had a bot that traded
its own portfolio against the user's ("versus bot"); that was removed
deliberately at the user's request. The engine only collects data and rates.
`trading/strategy.py`, `trading/portfolio.py`, the bot's `positions`/`trades`/
`equity` tables, and `tests/test_strategy.py` are gone — do not resurrect them.
`db.MIGRATIONS` drops the old bot tables from any pre-existing database.

## Git workflow — commit and push as you go

**Commit each logical unit of work as soon as it is complete, and push to
`origin` in the same pass.** Do not batch a session's work into one commit at
the end — the point is that there is always a restore point and the session's
state survives even if the conversation is lost.

Repository: `tovmaskayuf/ai-trading-bot` on GitHub (**public**), branch `main`,
HTTPS remote (`gh` is authenticated).

- Commit when a module, fix, or coherent feature slice works — not per file.
- Run both test scripts before committing anything under `analytics/`,
  `trading/`, or `settings.py`.
- Subject: imperative, under ~72 chars. Body: explain **why** — the constraint
  or upstream quirk that motivated the change. Match existing commits.
- Never commit `.venv/`, `data/`, `*.log`, or `.claude/settings.local.json` —
  `.gitignore` covers these.

Files may change between your tool calls — this project is developed with
parallel edits happening outside your own writes, and stale processes have
bitten before (two uvicorns once shared one log file and one crashed against a
migrated schema). Re-check `git status` before assuming your view is current;
`pkill -9 -f uvicorn` before restarting the server; never force-push.

## Environment

Python 3.14 in a local venv. `requirements.txt` exists (fastapi, uvicorn,
httpx). No pytest/ruff/black — the test files are **plain scripts** that print
PASS/FAIL per assertion and exit non-zero on failure.

```bash
.venv/bin/python tests/test_indicators.py    # indicator correctness
.venv/bin/python tests/test_manual.py        # portfolio accounting + settings guards
.venv/bin/python -m uvicorn server:app --port 8000   # run everything
```

Always run from the project root. Tests point `config.DB_PATH` at a scratch
temp file **before** importing `db`, so they never touch real state — preserve
that pattern in new tests.

**The directory name contains `}{`** (`ai_trading}{bot`). Braces break
unquoted shell paths — always quote it. This is why the GitHub repo is named
`ai-trading-bot`; GitHub rejects brace characters.

## Architecture

### Asset registry drives everything

`config.ASSETS` maps each internal symbol (`BTC`) onto per-provider
identifiers; provider selection derives from which identifiers are populated
via `Asset.price_source`. No module branches on a symbol name. HYPE is the
asset that exercises this: not listed on Binance spot (`HYPEUSDT` → error
`-1121`), so it routes to Hyperliquid. Any code assuming "all assets are on
Binance" breaks on HYPE specifically.

### Settings (`settings.py`)

Start-screen choices — `followed` assets, `starting_capital`, `language` —
stored as one JSON blob in the `meta` table. `settings.save()` validates
(capital $100–$10M, followed ⊆ registry and non-empty, language in
`config.SUPPORTED_LANGUAGES`) and always stamps `initialized=True`. **Changing
`starting_capital` resets the portfolio** (`server.save_settings` compares
before/after); changing language or followed assets never does. The engine
tracks and rates **all 15** regardless of selection — `followed` only gates the
UI and trade permission — so switching an asset back on has full history.

### Providers (`providers/`)

Stateless async modules over one shared `httpx.AsyncClient`. Retry policy in
`base.request` is deliberate: transport errors, 429 and 5xx retry with
backoff; other 4xx raise `ProviderError(permanent=True)` immediately.
Binance batch endpoints (`symbols=` param) need **compact JSON** —
`json.dumps` with its default `", "` separator triggers error `-1100`
(`_encode` in `providers/binance.py` exists for this).

Hyperliquid quirks: one POST URL discriminated by a `type` field; `allMids`
gives price only (no 24h change — the engine backfills it from CoinGecko or
candles); its candle endpoint takes a time range, not a `limit`.

CoinGecko `rank` is computed **within our 15-asset basket**, not globally.

### Storage (`db.py`)

SQLite in WAL mode; epoch-millisecond timestamps everywhere. Tables:
`candles` (1h and 1d rows distinguished by `interval`), `snapshots`,
`ratings`, `manual_holdings`, `manual_trades`, `manual_equity`, `meta`.
`manual_equity` is the portfolio-over-time series behind every chart — one row
per cycle plus one per trade, **never pruned**. `manual_equity_series()`
downsamples by keeping the *last* row per time bucket (right for value curves;
averaging would smooth away the moves). `prune()` trims only
`snapshots`/`ratings` past 90 days.

### Engine (`engine.py`)

One asyncio loop: fetch → snapshot → rate → `manual.record_equity` → publish
to SSE subscribers. Failure in any provider degrades that cycle rather than
killing the service. `bootstrap()` backfills 300×1h and 365×1d candles on
first run. The rating's `holding` flag comes from `manual.holding_for()` — the
*user's* holdings — which is what makes the HOLD signal mean "you own this."

### Rating engine (`analytics/`)

`indicators.py` is **deliberately dependency-free** (no numpy — new-Python
wheel risk) and every function returns `None` on insufficient data; preserve
both contracts. `rating.py` scores four axes 0–100. Risk and parts of
Structure are **percentile-ranked within the basket** — absolute bands proved
regime-dependent (in a quiet market every composite collapsed and BUY never
fired; this was a real calibration bug). Sub-scores are stored raw so the
dashboard recombines them client-side under user weights; the JS `composite()`
in `dashboard.html` must stay in exact agreement with
`rating.composite_score()` including missing-axis renormalisation — there is a
JSC cross-check pattern in the repo history for verifying this.

Signals: hysteresis between `BUY_THRESHOLD` (70) and `EXIT_THRESHOLD` (45);
the band is one knob, not two. `HOLD` = user holds it (or it just cooled from
BUY); `NEUTRAL` = flat. Do not feed `HOLD` back into the carry-forward
condition in `signal_for` — that makes it self-sustaining and `NEUTRAL`
unreachable (past bug).

### Portfolio (`trading/manual.py`)

Holdings model with **average cost basis** (fees included in basis), not
discrete lots. Buys interpret `usd` as all-in (cost + fee). Guards raise
`TradeError` with user-facing, properly punctuated messages — the server
passes them straight through as HTTP 400 detail. A flat round-trip must lose
exactly the fees; `tests/test_manual.py` asserts this.

### Server (`server.py`)

`RANGES` maps `1h|24h|7d|30d|1y|all` to cutoffs; history endpoints downsample
server-side to ~360 points. `/api/asset/{s}/prices` picks resolution by range
(snapshots → 1h candles → 1d candles) and appends the live price so lines end
at "now". Trade prices are resolved **server-side** so a stale tab cannot
fill at an old quote. `/api/overview` re-stamps `followed`/`held` flags at
read time because the engine's copy is up to a minute stale.

### Frontend (`static/dashboard.html`)

One self-contained file, no CDN, no build step: animated landing (language
pills, 15 asset switches, capital input), Markets and Portfolio views, a
clickable stat sub-page per tile (Cash / Invested / Unrealized / Realized /
Fees), buy/sell drawer, SSE live updates, light/dark themes.

- **i18n**: `I18N` dict with `en`/`hy`/`uk`/`es`/`el`, 130 keys each — keep
  all five languages in exact key parity when adding strings (there is a JSC
  parity-check pattern in repo history). `t(key, vars)` does `{var}`
  templating. Russian was **removed at the user's request** (replaced by
  Ukrainian, with Greek added) — do not re-add it. The boot path falls back to
  `en` when a saved language code no longer exists. The brand name "AI Crypto
  Trading and Training by TT" is never translated.
- **Landing state**: `Landing.show()` reads saved settings once per visit;
  `Landing.render()` must never re-read them — it re-runs on every language
  switch and after Select All / Clear, and re-reading silently reverts the
  user's toggles and typed capital (this was a real bug).
- **Charts**: hand-rolled SVG (`lineChart`), crosshair+tooltip, range pills via
  `rangePills()`. Axis colors are the four validated categorical slots from
  the dataviz skill — every use is direct-labeled, never color-alone.
- The drawer defers re-renders while the user is typing an amount
  (`drawerBusy()`), or a cycle update would wipe their input mid-trade.

## Cadence constraints

`CYCLE_SECONDS = 60`. The stagger multiples are sized so the **wall-clock**
rates stay at the values that were safe at the old 120s cycle: klines every
10th cycle (~10 min), CoinGecko every 8th (~8 min), daily candles every 60th
(~hourly). Lowering the multiples risks free-tier 429s.

## Deployment

`render.yaml` deploys to Render's free tier (deploy-button URL in README).
**The region must be non-US** (`region: frankfurt` in the blueprint):
Binance's API geo-blocks US-hosted IPs with HTTP 451, which starves 14 of 15
assets — only HYPE survives, because Hyperliquid is not geo-blocked. This
presented as "No Data for every coin except one" on a default-region (Oregon)
deployment. Render regions are immutable after creation: applying the fix
means deleting the service and deploying the blueprint again.

Other free-tier realities: instance sleeps when idle, disk is ephemeral
(portfolio resets on restart), and the app keeps **one portfolio per
instance** — no user accounts. Per-visitor portfolios are the known next step
if a shared public instance is wanted. A claude.ai Artifact cannot host this:
the artifact CSP blocks external fetches (no network capability on this
account), so live prices are unreachable from an artifact page.

## Known issues / expectations

- Cold start: some axes read "—" until candles accumulate (`score_momentum`
  needs ≥60 closes, `risk_metrics` ≥30). Expected.
- `snapshots` grows ~21.6k rows/day at the 60s cycle; pruned at 90 days.
- Equity history accumulates only while the server runs — gaps in the
  portfolio charts mean the process was down, not a bug.
