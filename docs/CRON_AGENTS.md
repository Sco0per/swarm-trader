# Daily Cloud Routines (Cron Agents)

This document describes how `src/swing` is operated by a fleet of scheduled
cloud agents ("routines," created via Claude's remote-trigger API) instead of
a human running the CLI by hand every day.

## Why this needed a real design decision, not just a cron entry

Cloud routines each spin up a **fresh, ephemeral sandbox** on every fire: a
clean `git clone` of this repo, no persistent disk, no access to the local
machine that authored this doc. `src/swing`'s safety model — the kill switch,
the drawdown halt, the consecutive-loss halt, the trade journal a postmortem
engine reads lessons from — all lives in `SwingDatabase` (see
`src/swing/database.py`). If that state doesn't survive between runs, the
halts silently reset to "off" every single day, which defeats the entire
point of `src/swing/risk.py`.

**First attempt (abandoned): commit the database file back to git.** The
original design tracked `data/adaptive_swing.db` in git and had every
routine `git add && commit && push` it after running. This turned out not to
work at all: cloud routine sessions have **no functioning git-push
credentials**, confirmed with two separate real failures — first a 520KB
`poetry.lock` regeneration, then (to rule out file size as the cause) a
routine's own tiny database diff, both rejected with the identical `403
Forbidden`. Verbose curl output showed no Authorization header ever reaching
GitHub; the injected `GITHUB_TOKEN` in these sessions is a non-functional
placeholder. The only real GitHub write path from a routine is a
content-inlining MCP tool, which is fine for small text edits but unsafe for
a binary SQLite file of any size — the exact bytes have to pass through the
model's context intact, and a single corrupted byte silently breaks the
database.

**Current design: a hosted Turso (libSQL-over-HTTP) database.**
`SwingDatabase` connects to a remote Turso database instead of a local file
whenever `TURSO_DATABASE_URL` is set (see `.env.example`); with it unset, it
falls back to the original local SQLite file, so local dev and the test
suite are completely unaffected. Every `swing-trader` command now reads and
writes the hosted database directly and in real time — there is no
end-of-run save step, and therefore nothing for a routine to git-push at
all. `turso_serverless` (the Python client) is a DB-API 2.0 driver
deliberately built sqlite3-compatible: `?`-style parameters, `Row` supports
column-name access the same way `sqlite3.Row` does, `IntegrityError` is
raised on constraint violations, `executescript()` commits any pending
transaction first — verified by installing the package and reading its
source directly, not just its (incomplete) hosted docs.

Turso's free tier (5GB storage, 500M row reads/month, 10M row writes/month)
is far beyond what this system needs — a handful of routine runs a day, each
doing lightweight reads/writes, is nowhere close to those limits.

## The five routines

Each routine runs exactly one `swing-trader` subcommand. None of them touch
the Robinhood live-trading path (`live-ticket`, `record-live-fill`,
`record-live-exit`) — those remain human-only per `docs/ROBINHOOD_MCP.md`,
and Robinhood's MCP connector must never be attached to any of these
routines (routine creation auto-attaches every connected MCP connector by
default — always `clear_mcp_connections: true` immediately after creating
one, or it will silently gain live-order-placement tools).

| # | Name | Cron (UTC) | ET (approx, EDT) | Command | Needs |
|---|------|-----------|-------------------|---------|-------|
| 1 | scan-agent | `35 13 * * 1-5` | 9:35am | `scan` | Turso only (no LLM, no broker) |
| 2 | decide-trade-agent | `3 14 * * 1-5` | 10:03am | `run` | `ANTHROPIC_API_KEY`, `ALPACA_API_KEY`, `ALPACA_API_SECRET`, Turso |
| 3 | reconcile-agent | `55 19 * * 1-5` | 3:55pm | `reconcile` | `ALPACA_API_KEY`, `ALPACA_API_SECRET`, Turso |
| 4 | report-agent | `15 20 * * 1-5` | 4:15pm | `report daily` | Turso only |
| 5 | lessons-agent | `7 22 * * 0` | Sun 6:07pm | `review-observations` | Turso only |

`TRADING_ENABLED` is deliberately **not** set, matching `src/swing/config.py`'s
fail-closed default (`trading_enabled: bool = False`). Until it's explicitly
set to `true` in the cloud environment, `decide-trade-agent` will compute and
log full proposals every day but submit nothing — a rehearsal period before
real (paper) orders go out. Flip it on when you're satisfied watching it.

All 5 routines run their orchestration on **Haiku 4.5**, not Sonnet — each
one just runs a fixed CLI command and reports the output, no real reasoning
required. The actual trading intelligence is a separate set of Anthropic API
calls the Python code makes directly (`src/swing/llm_backend.py`, governed by
`MODEL_FALLBACK`/`ANALYST_MODEL`/etc.), billed and configured independently
of the routine's own model.

## Known limitations

- **`report-agent`'s output file isn't persisted anywhere.** `report daily`
  writes a markdown file under `reports/` in the ephemeral checkout, which
  vanishes when the sandbox is torn down — there's no git-push and the file
  content itself isn't written to the database. The routine's printed output
  (captured in its run log / push notification) is currently the only
  durable copy. Worth fixing later, e.g. by having the report content itself
  land in a database table.
- **DST**: the UTC offsets above assume EDT (UTC-4). They drift by an hour
  during EST (roughly early Nov–mid Mar); the cron expressions need a manual
  ±1h nudge at each DST transition, or update to ET-aware scheduling.
- **Market holidays**: cron doesn't know NYSE is closed on a given Tuesday;
  the commands are expected to degrade gracefully (no candidates / stale-data
  skip) on those days rather than error, but this isn't independently
  verified for every holiday case.
- **Cold start**: every run is a brand-new sandbox, so `poetry install`
  reinstalls the full dependency set (langchain family included) from
  scratch each time. Expect each routine to take a few minutes before it
  even starts the actual command.
- **Earnings-date lookups still use yfinance**, not Alpaca — Alpaca's market
  data API has no earnings calendar at all. Yahoo Finance blocks this cloud
  environment's IP range for bulk price history (confirmed: 100%
  connection-reset failures), so `fetch_earnings_trading_days` in
  `src/swing/data_feed.py` is bounded to an 8-second-per-symbol timeout and
  degrades to "unknown, treat conservatively" on failure rather than
  crashing or hanging the whole scan. If Yahoo also blocks this specific
  endpoint, individual-stock scoring loses earnings-exclusion precision
  (ETF candidates are unaffected) — a dedicated earnings-calendar API would
  be the real fix if this turns out to matter in practice.
- **Secrets aren't really secret.** `ANTHROPIC_API_KEY` / `ALPACA_API_KEY` /
  `ALPACA_API_SECRET` / `TURSO_AUTH_TOKEN` live in the cloud environment's
  plain "Environment variables" box, not a real secrets vault — the UI
  itself warns these are visible to anything running in the environment.
  Accepted as a reasonable tradeoff here (paper trading, revocable keys),
  but don't treat that box as secure storage.
