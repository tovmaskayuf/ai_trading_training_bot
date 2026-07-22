# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

**AI Crypto Trading and Training by TT** — a paper-trading trainer for 15
cryptocurrencies. A 60-second polling engine collects live prices and computes
a four-axis AI rating per asset; each visitor trades their own virtual
portfolio by hand through a Polymarket-styled web UI with an animated start
screen, five languages, time-range charts everywhere, optional accounts, a
global leaderboard, and a master console for administering players.

**There is no automated trading.** An earlier iteration had a bot that traded
its own portfolio against the user's ("versus bot"); that was removed
deliberately at the user's request. The engine only collects data and rates.
`trading/strategy.py`, the bot's `positions`/`trades`/`equity` tables, and
`tests/test_strategy.py` are gone — do not resurrect them. `db.MIGRATIONS`
drops the old bot tables from any pre-existing database. (Note `portfolio.py`
at the repo root is the *current* per-user portfolio module, unrelated to the
deleted `trading/portfolio.py`.)

**Every visitor has their own portfolio.** This was not always true: the app
once served one shared portfolio, so any visitor's buy spent everyone's cash,
anyone could liquidate anyone's position, and an unauthenticated reset wiped it
for all of them. Do not reintroduce process-global portfolio state.

## Git workflow — commit and push as you go

**Commit each logical unit of work as soon as it is complete, and push to
`origin` in the same pass.** Do not batch a session's work into one commit at
the end — the point is that there is always a restore point and the session's
state survives even if the conversation is lost.

Repository: `tovmaskayuf/ai-trading-training-bot` on GitHub (**public**), branch
`main`, HTTPS remote (`gh` is authenticated). Renamed from `ai-trading-bot` to
`ai_trading_training_bot` on 2026-07-20, then to the current hyphenated name on
2026-07-21 so the repo, the Render service and the site all read alike. GitHub
redirects both old URLs, but update links rather than relying on the redirect.

- Commit when a module, fix, or coherent feature slice works — not per file.
- Run the suites covering what you touched, and run **all** of `tests/` before
  anything that crosses module boundaries. `analytics/` →
  `test_indicators.py`; `portfolio.py`/`accounts.py`/`userstore.py` →
  `test_portfolio.py`; `admin.py` → `test_admin.py`;
  `static/dashboard.html` → `test_frontend.py`;
  `settings.py`/`trading/manual.py` → `test_manual.py`;
  `providers/base.py`/`engine.py` cadence → `test_ratelimit.py`;
  retention/`GUEST_TTL_DAYS`/`EQUITY_RETENTION_DAYS` → `test_maintenance.py`.
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

Python 3.14 in a local venv. `requirements.txt`: fastapi, uvicorn, httpx,
`psycopg[binary]`. No pytest/ruff/black — the test files are **plain scripts**
that print PASS/FAIL per assertion and exit non-zero on failure.

**Node is required for `test_frontend.py`** (`brew install node`). Without it
that suite fails loudly rather than skipping, which is deliberate.

```bash
.venv/bin/python tests/test_indicators.py    # indicator correctness
.venv/bin/python tests/test_manual.py        # legacy portfolio accounting
.venv/bin/python tests/test_portfolio.py     # per-user portfolios + leaderboard
.venv/bin/python tests/test_admin.py         # block / delete / reset + no password leak
.venv/bin/python tests/test_ratelimit.py     # no retry on 418/429, host cooldowns
.venv/bin/python tests/test_maintenance.py   # retention: guest purge, equity prune
.venv/bin/python tests/test_frontend.py      # JS parses, i18n parity, DOM sanity
.venv/bin/python -m uvicorn server:app --port 8000   # run everything
```

`test_portfolio.py`, `test_admin.py` and `test_maintenance.py` run against
whichever backend is configured, so they double as the Postgres check:
`DATABASE_URL=postgresql://… .venv/bin/python tests/test_portfolio.py`.

Always run from the project root. Tests point `config.DB_PATH` (and
`config.BASE_DIR` for the user store) at a scratch temp file **before**
importing `db`/`userstore`, so they never touch real state — preserve that
pattern in new tests.

**Never ship frontend changes without running `test_frontend.py`.** A release
once shipped a dashboard whose markup did not parse: an unclosed `<noscript>`
swallowed the entire body, so the server stayed healthy and every API check
passed while the page was blank. A brace-balance count was treated as proof the
file was valid. It is not — only `node --check` and an HTML parse are.

**The directory name contains `}{`** (`ai_trading}{bot`). Braces break
unquoted shell paths — always quote it. This is why the GitHub repo is named
`ai-trading-training-bot`; GitHub rejects brace characters.

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

### Storage — two stores, on purpose

**`db.py` — market data, ephemeral.** SQLite in WAL mode; epoch-millisecond
timestamps everywhere. Tables: `candles` (1h and 1d rows distinguished by
`interval`), `snapshots`, `ratings`, `meta`. `prune()` trims only
`snapshots`/`ratings` past 90 days. This lives on the instance's disk and is
**expected to be lost on restart** — it regenerates from the providers in
about 3.5 seconds, so durability buys nothing here.

**`userstore.py` — accounts and portfolios, durable.** Postgres when
`DATABASE_URL` is set, otherwise its own SQLite file (`data/users.db`) so local
development needs no database server. Tables: `users`, `sessions`,
`portfolios`, `holdings`, `user_trades`, `user_equity`. Both backends speak
`ON CONFLICT … DO UPDATE`, which is what lets one set of statements serve both;
every difference is confined to `_Dialect`. Statements are written with `?` and
converted to `%s` for psycopg — **escape `%` before substituting `?`**, or a
literal percent becomes a placeholder.

`_split_statements()` strips `--` comments before splitting on `;`, because a
semicolon *inside a comment* otherwise cuts it in half and the remainder parses
as SQL. That was a real bug.

**Retention on the durable side is a size bound, not tidiness.** Free Postgres
has a hard 1 GB ceiling and holds the *only* copy of every account, while the
market-data store is regenerable and prunes itself. `userstore.maintenance()`
runs one pass — `purge_stale_guests()`, `purge_expired_sessions()`,
`prune_equity()` — from the engine on a cycle multiple, hourly at the 60s
cadence; these are bulk deletes over indexed columns and running them per cycle
would cost more than the rows do.

**Those "indexed columns" are indexed on purpose, and the indexes are not the
obvious ones.** Both primary keys lead on the wrong column for the maintenance
pass: `user_equity` is keyed `(user_id, ts)` and `sessions` on `token`, so
`prune_equity()`'s `WHERE ts < ?` and `purge_expired_sessions()`'s expiry sweep
each degraded into a full scan of the table. `idx_user_equity_ts` and
`idx_sessions_expires` exist for exactly those two deletes — measured at 19.55ms
scanning vs 0.86ms seeking over 1.08M equity rows, and the scan grows with the
table while the seek does not. This costs more than it looks: every statement
in this module runs under one process-wide lock, so a slow maintenance delete
blocks every concurrent request, not just itself. Note the index is a genuine
*loss* on a delete that removes a large fraction of the table (77ms scanning vs
168ms indexed when trimming a third) — the hourly steady-state prune, which
removes one cycle's worth past the boundary, is the case being optimised for.

The thing that made this urgent: **every cookie-less request mints a guest
row**, so crawlers, uptime probes and one-off page loads each left one behind
permanently, and each was charged an equity row every cycle forever —
1,440 rows per abandoned visitor per day, in the one table that cannot be
regenerated. Two independent guards, and both are load-bearing:
`record_equity_all()` skips portfolios that hold nothing and never traded
(their curve is a constant the seed row already records), and
`purge_stale_guests()` deletes untraded guests past `GUEST_TTL_DAYS` (7).
A guest who actually traded is kept until they claim an account, and a
registered account is never purged at any age. `prune_equity()` trims history
past `EQUITY_RETENTION_DAYS` (90).

`_USER_TABLES` is ordered so child rows never outlive their user: **no backend
here declares foreign keys, so nothing cascades on its own** — deleting a user
without walking that list leaves orphans behind.

### Engine (`engine.py`)

One asyncio loop: fetch → snapshot → rate → `portfolio.record_equity_all` →
publish to SSE subscribers. Failure in any provider degrades that cycle rather
than killing the service.

There is **no separate bootstrap pass**: cycle 0 already refreshes both candle
intervals (`0 % KLINE_EVERY == 0` and `0 % DAILY_EVERY == 0`), so backfilling
first ran the same ~30 fetches twice and left the dashboard empty for minutes
on every cold start. `refresh_candles()` fetches concurrently behind a
semaphore (`CANDLE_CONCURRENCY`) and records failures in
`STATE["_candle_errors"]`, surfaced by `/api/health` — a silent candle failure
empties momentum, risk *and* relative while prices keep flowing, which looks
like a half-broken UI with nothing to explain it.

`STATE["running"]` is set when the loop starts, not after the first successful
cycle, so health does not report the engine as down throughout warm-up.

The rating's `holding` flag is fixed at `False`: ratings are shared by every
viewer, so they cannot depend on whose holdings they are. Each response stamps
that caller's own `held` flags at read time in `/api/overview`.

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

### Portfolio (`portfolio.py`)

Per-user. Holdings model with **average cost basis** (fees included in basis),
not discrete lots. Buys interpret `usd` as all-in (cost + fee). Guards raise
`TradeError` with user-facing, properly punctuated messages — the server passes
them straight through as HTTP 400 detail. A flat round-trip must lose exactly
the fees; `tests/test_portfolio.py` asserts this.

Every trade writes the holding, the trade row **and the cash balance inside one
transaction**. Writing cash afterwards leaves a window where a crash books the
asset without the payment — free money on a buy, vanished proceeds on a sell.

`snapshot()` aggregates trade totals **in SQL**, not by pulling rows into
Python: it runs once per cycle per connected client, so a long history would
otherwise scale with history × viewers. `record_equity_all()` writes one equity
point for every portfolio in a single bulk pass for the same reason.

`leaderboard()` marks every player to the **same live prices at read time**
rather than reading stored equity rows — otherwise whoever traded most recently
would rank on the freshest valuation. Guests are excluded.

It publishes `starting_capital` and `pnl` alongside the total, and the board
shows profit rather than the raw balance, because **players choose their own
opening balance on the landing screen**. A bare total is not a score under that
rule: someone who opened with $10M and never traded outranks a player who
doubled $1,000. Profit is zero for an untouched account at any size, and the
ranking itself is by percentage return. Keep the money column honest if you
add sort modes here.

`trading/manual.py` is the retired single-portfolio version, kept only because
`tests/test_manual.py` still covers its accounting.

### Accounts (`accounts.py`)

PBKDF2-HMAC-SHA256 from the stdlib; the iteration count is stored in the hash
so it can be raised later without invalidating existing ones. Comparison is
constant-time.

A login for an **unknown username still verifies against a cached real-cost
dummy hash**. A cheap placeholder returns in ~0ms against ~33ms for a wrong
password, and that difference enumerates valid accounts. Measured at 1.00x
after the fix — do not "optimise" it away.

First-time visitors get a **guest** row (`is_guest = 1`, unusable password)
so they can trade before signing up; `claim_account()` converts that same row
in place so their portfolio carries over. Guests are kept off the leaderboard.

Signup takes a `confirm` field and rejects a mismatch **server-side as well as
in the client** — a typo on a brand-new account locks the owner out of it.

### Administration (`admin.py`)

One admin account, seeded by `ensure_master()` from `MASTER_USERNAME` /
`MASTER_PASSWORD` at startup. **Exactly one** — `_demote_other_admins()` strips
the flag from every other account and kills their sessions on each boot.
Without that, changing `MASTER_USERNAME` granted admin to the new name while
leaving it on the old one, so a former admin kept full access to every player's
record. That happened in production. Renaming must move the privilege, never
copy it. **The password is never committed** — the repo is
public. With `MASTER_PASSWORD` unset no admin account is created at all, rather
than one with a guessable password. The hash is re-applied on every boot, so
rotating the env var rotates the credential.

**Passwords cannot be revealed, and this is not a gap to be closed.** They are
PBKDF2 hashes; there is nothing to read back, and storing them reversibly would
expose every player (people reuse passwords across sites) for no operational
gain. `reset_password()` is the supported recovery path. `list_players()` and
`player_detail()` return `password: None` with a note saying why, so the UI
never implies otherwise. `tests/test_admin.py` asserts no hash-shaped string
appears in either payload.

Admin routes return **404, not 403**, to non-admins — a distinct 403 would
confirm the surface exists to anyone probing.

Blocking **leaves the user's sessions in place**. Deleting them would make the
account anonymous, and `require_user()` would hand it a fresh guest — so a
blocked player would silently keep playing rather than being told. With the
session intact every request resolves to them and returns an explicit 403.
`/api/me` is the one exception: it answers so the client can *explain* the
block instead of failing blank.

`delete_player()` removes the rows outright rather than tombstoning, because
freeing the username for reuse is the point. Admins cannot be blocked or
deleted, which also stops the operator locking themselves out.

### Server (`server.py`)

`RANGES` maps `1h|24h|7d|30d|1y|all` to cutoffs; history endpoints downsample
server-side to ~360 points. `/api/asset/{s}/prices` picks resolution by range
(snapshots → 1h candles → 1d candles) and appends the live price so lines end
at "now". Trade prices are resolved **server-side** so a stale tab cannot
fill at an old quote. `/api/overview` re-stamps `followed`/`held` flags at
read time because the engine's copy is up to a minute stale.

`require_user()` resolves an HttpOnly session cookie and creates a guest on
first visit. **Every portfolio-mutating endpoint is scoped to that caller** —
`/api/manual/reset` once wiped the single shared portfolio for everyone, with
no authentication at all.

The session cookie is `secure` only when `_is_local()` is false, which tests
`RENDER` and `DATABASE_URL`. Render terminates TLS so production is always
HTTPS, but a `secure` cookie over plain `http://127.0.0.1` is **not set at
all** — so hardcoding it on would break login locally in a way that looks like
broken auth rather than a cookie policy.

`/` sets `Cache-Control: no-cache, must-revalidate`. The whole app is one file,
so without it browsers apply heuristic freshness and pin users to an old build
across deploys — including a broken one, which is how a bad release once
survived being reverted.

### Frontend (`static/dashboard.html`)

One self-contained file, no CDN, no build step: animated landing (language
pills, 15 asset switches, capital input), Markets / Portfolio / Leaderboard
views plus an admin-only Master view, a clickable stat sub-page per tile (Cash /
Invested / Unrealized / Realized / Fees), buy/sell drawer, account modal, SSE
live updates, light/dark themes.

- **The Master tab is hidden, not protected, in the client.** `#adminTab`
  ships with `.hide` and is only revealed when `/api/me` comes back
  `is_admin`. That is cosmetic — the real enforcement is server-side, where
  every `/api/admin/*` route 404s for non-admins. Never let the client's flag
  become the thing that gates access.
- **The admin player view is a drawer, not an `alert()`.** It reuses the trade
  drawer's slot (`.dr-*` styles) to show a stats grid, equity curve, holdings
  and recent trades. `alert()` was the original and is not an option to go back
  to: after a couple of dialogs browsers offer "prevent additional dialogs",
  and once the user ticks it the action silently does nothing — plus a dialog
  cannot render the chart. The equity curve is drawn inside
  `requestAnimationFrame` after the `innerHTML` write, or the SVG has no
  measurable width yet and the line comes out empty.
- **Header layout**: the live/cycle status pill sits between the brand and the
  view tabs, held there by a `.header-gap` spacer on each side. It is
  deliberately *not* centred on the viewport — the right-hand cluster is wider
  than the brand, so a true centre reads as closer to the tabs than the logo.
- **i18n**: `I18N` dict with `en`/`hy`/`uk`/`es`/`el`, 210 keys each — keep
  all five languages in exact key parity when adding strings.
  `data-i18n` sets `textContent`, `data-i18n-ph` sets `placeholder`; both are
  swept by `applyStatic()` and both are checked by the frontend test.
  `tests/test_frontend.py` checks this by **evaluating the object in node**,
  not by regex: keys inside translated prose ("no hay jugadores: …") produced
  false mismatches when it was done textually. `t(key, vars)` does `{var}`
  templating. Russian was **removed at the user's request** (replaced by
  Ukrainian, with Greek added) — do not re-add it. The boot path falls back to
  `en` when a saved language code no longer exists. The brand name "AI Crypto
  Trading and Training by TT" is never translated.
- **Crawler summary**: `#seo-summary` is plain markup hidden by a `.js` class
  that an inline `<head>` script stamps before first paint. It must **not** be
  wrapped in `<noscript>` — HTML-to-text converters, including AI browsing
  tools, discard noscript content along with scripts, so the page read as an
  empty shell. It must also not be `display:none` by default, which would hide
  it from the very readers it exists for.
- **No native `alert` / `confirm` / `prompt`.** Browsers offer "prevent this
  page from creating additional dialogs" after a couple of them, and once a
  user accepts, all three stop working for the session. `confirm()` then
  returns false, which merely fails safe, but `prompt()` returns null -- so the
  admin password reset silently did nothing. Use `Ask.open()`, which returns a
  promise and supports an optional input. `tests/test_frontend.py` fails if any
  native dialog reappears.
- **User-supplied text is escaped** with `esc()` before reaching `innerHTML`.
  The username charset bars angle brackets, but display names should not rest
  on a single validator.
- **Landing state**: `Landing.show()` reads saved settings once per visit;
  `Landing.render()` must never re-read them — it re-runs on every language
  switch and after Select All / Clear, and re-reading silently reverts the
  user's toggles and typed capital (this was a real bug).
- **Charts**: hand-rolled SVG (`lineChart`), crosshair+tooltip, range pills via
  `rangePills()`. Axis colors are the four validated categorical slots from
  the dataviz skill — every use is direct-labeled, never color-alone.
- The drawer defers re-renders while the user is typing an amount
  (`drawerBusy()`), or a cycle update would wipe their input mid-trade.

## Cadence and rate limits

`CYCLE_SECONDS = 60`. The stagger multiples are sized so the **wall-clock**
rates stay at the values that were safe at the old 120s cycle: klines every
10th cycle (~10 min), CoinGecko every 8th (~8 min), daily candles every 60th
(~hourly). Lowering the multiples risks free-tier 429s.

**Binance bans, it does not merely throttle.** Repeated 429s escalate to HTTP
418 with an IP ban lasting tens of minutes — this happened, for 30 minutes, and
presented as prices flowing while momentum/risk/relative stayed empty (those
three need candles; only HYPE, which is not on Binance, kept a full rating).

Three rules came out of it, all with tests in `tests/test_ratelimit.py`:

1. **Never retry a rate-limit response.** `providers/base.py` retries transport
   errors and 5xx, but 418/429 fail immediately and put the *host* on a
   cooldown parsed from the ban timestamp or `Retry-After`. Retrying is what
   escalates a soft limit into a ban, since each attempt spends more of the
   same budget. A three-attempt retry on 429 is what caused this.
2. **`CANDLE_CONCURRENCY = 2`, not 5.** klines costs weight 4 per call and the
   budget is per *IP* — which on Render's shared egress is not ours alone. A
   five-wide burst across 14 symbols on every cold start was the trigger.
   Concurrency buys a few seconds of startup; a ban costs half an hour.
3. **Daily candles are deferred off cycle 0** (fetched on cycle 1 instead when
   the database is empty). Both intervals refreshing on the same cold-start
   tick doubled the weight at the exact moment every symbol needed a full pull.
   Nothing needs daily bars in the first minute — they feed the 1y/all charts,
   not the ratings.

`/api/health` reports `rate_limited` (hosts on cooldown, seconds remaining) and
`candle_errors`. Empty is healthy; an entry there explains missing rating axes
immediately instead of via the logs.

## Deployment

`render.yaml` deploys to Render's free tier (deploy-button URL in README). The
web service is `ai-trading-training-bot`, matching the GitHub repo — the name
becomes the onrender.com subdomain, so repo, service and site all read alike.

**Renaming the service is dashboard-first.** `name:` in the blueprint must
match the live service; if it does not, the next sync provisions a *second*
service rather than renaming the existing one, and the new one comes up on an
empty database while the old one keeps every account. Rename in the Render
dashboard (Settings → Name), then update `render.yaml` to match. The public URL
changes with it and the old one stops resolving. The database name is a
separate matter: changing `tt-trading-db` provisions a **new, empty** Postgres,
so leave it alone unless the intent really is to start over.

**The region must be non-US** (`region: frankfurt` in the blueprint):
Binance's API geo-blocks US-hosted IPs with HTTP 451, which starves 14 of 15
assets — only HYPE survives, because Hyperliquid is not geo-blocked. This
presented as "No Data for every coin except one" on a default-region (Oregon)
deployment. Render regions are immutable after creation: applying the fix
means deleting the service and deploying the blueprint again.

**A Postgres database is required for accounts to persist.** `render.yaml`
provisions `tt-trading-db` (free plan, Frankfurt) and injects `DATABASE_URL`.
Without it the app still runs, but the user store falls back to SQLite on the
ephemeral disk and every account, portfolio and leaderboard standing is wiped
on restart. **Free Render Postgres expires 30 days after creation** and is
deleted after a 14-day grace period — upgrade or export before then.
`tools/backup_userstore.py` is the export: it dumps `users`, `portfolios`,
`holdings`, `user_trades` and `user_equity` to JSON through psycopg, so it
needs no `pg_dump` on the machine running it. Point it at the **External**
connection string; the internal one only resolves inside Render's network.
It skips `sessions` on purpose — live tokens, self-expiring, no upside to
writing them to disk.

That fallback is silent from the outside, which is why `/api/health` reports
`store_backend` and `accounts_durable`. Check them after any deploy that
touches the database: `"store_backend": "sqlite"` on the live instance means
accounts are already living on borrowed time, and the difference is only
otherwise visible once the data is gone.

**`MASTER_PASSWORD` gates the admin account into existence.** `render.yaml`
declares it `sync: false` so Render prompts for it rather than storing it in
the repo — this is a public repository and a committed admin credential is
readable by anyone. Deploy without it and there is **no admin account at all**
(by design, over a guessable default), so the Master tab never appears for
anyone. `MASTER_USERNAME` defaults to `master`. Changing the env var and
redeploying rotates the password, because `ensure_master()` re-applies it on
every boot.

Other free-tier realities: instance sleeps when idle, and the market-data disk
is ephemeral (candles re-backfill on every cold start, ~3.5s). A claude.ai
Artifact cannot host this:
the artifact CSP blocks external fetches (no network capability on this
account), so live prices are unreachable from an artifact page.

## Known issues / expectations

- Cold start: some axes read "—" until candles accumulate (`score_momentum`
  needs ≥60 closes, `risk_metrics` ≥30). Expected.
- `snapshots` grows ~21.6k rows/day at the 60s cycle; pruned at 90 days.
- Equity history accumulates only while the server runs — gaps in the
  portfolio charts mean the process was down, not a bug.
- **CoinGecko 429s on the Render deployment** even though the same call
  succeeds from a home IP. Render's free tier egresses through shared
  addresses that CoinGecko rate-limits on reputation, so lowering our own
  cadence does not fix it. Prices are unaffected (Binance/Hyperliquid carry
  them) but `mcap`/`rank` stay `None`, so the Structure axis scores on volume
  trend and spread alone. The fix is a free CoinGecko demo API key passed as
  an env var (`COINGECKO_API_KEY`), not a cadence change. A *wrong* key is
  loud — 401 `error_code 10002`; a *missing* one is silent, which is why
  `/api/health` reports `coingecko_auth`.
- **Binance klines failing while `/ticker/24hr` keeps working** means a
  rate-limit ban, not the geo-block. Confirmed cause: HTTP 418, `-1003 Way too
  much request weight used; IP banned`. Prices survive because the ticker call
  is one batched request; candles do not, so momentum, risk and relative empty
  out and only HYPE stays fully rated. See *Cadence and rate limits* for the
  three rules that came out of it. Distinguish from the geo-block by the
  `stale` flag: under a geo-block prices fall back to CoinGecko and are flagged
  stale, under a ban they are not.
