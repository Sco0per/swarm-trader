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

## The seven narrow routines

Each routine runs exactly one `swing-trader` subcommand. None of them touch
the Robinhood live-trading path (`live-ticket`, `record-live-fill`,
`record-live-exit`) — those remain human-only per `docs/ROBINHOOD_MCP.md`,
and Robinhood's MCP connector must never be attached to any of these
routines (routine creation auto-attaches every connected MCP connector by
default — always `clear_mcp_connections: true` immediately after creating
one, or it will silently gain live-order-placement tools).

Use an `America/New_York` timezone-aware scheduler, not fixed UTC expressions.
The environment keys below make every time configurable without changing code.

| # | Name | Configured ET default | Command | Needs |
|---|------|-----------------------|---------|-------|
| 1 | initial-scan-agent | `SCHEDULE_INITIAL_SCAN_ET=09:35` weekdays | `scan` | Turso + deterministic market data; no LLM |
| 2 | decision-cycle-agent | `SCHEDULE_DECISION_CYCLE_ET=10:15` weekdays | `run` | model, paper broker, market data, Turso |
| 3 | midday-refresh-agent | `SCHEDULE_MIDDAY_REFRESH_ET=12:30` weekdays | `scan` | Turso + deterministic market data; no LLM |
| 4 | afternoon-refresh-agent | `SCHEDULE_AFTERNOON_REFRESH_ET=14:30` weekdays | `scan` | Turso + deterministic market data; no LLM |
| 5 | position-health-agent | `SCHEDULE_POSITION_HEALTH_ET=15:45` weekdays | `reconcile` | paper broker + Turso; no candidate LLM |
| 6 | daily-report-agent | `SCHEDULE_DAILY_REPORT_ET=16:15` weekdays | `report daily` | Turso only |
| 7 | weekly-lessons-agent | `SCHEDULE_WEEKLY_LESSONS_ET=Sunday 18:00` | `review-observations` | Turso only |

Each agent prompt is generated from `src/swing/scheduling.py`. It permits its
single listed subcommand and explicitly forbids every other shell command and
all live/broker mutation subcommands. The scan refreshes never call an LLM.
The decision cycle reuses a stored schema-valid response unless the candidate
is new, its score changes by `LLM_MATERIAL_SCORE_CHANGE`, its setup/regime or
event/news digest changes, or a prompt/model version changes. Every decision
cycle persists actual and suppressed call counts.

`TRADING_ENABLED` is deliberately **not** set, matching `src/swing/config.py`'s
fail-closed default (`trading_enabled: bool = False`). Until it's explicitly
set to `true` in the cloud environment, `decide-trade-agent` will compute and
log full proposals every day but submit nothing — a rehearsal period before
real (paper) orders go out. Flip it on when you're satisfied watching it.

All seven routines run their orchestration on a cheap fixed-command model — each
one just runs a fixed CLI command and reports the output, no real reasoning
required. The actual trading intelligence is a separate set of Anthropic API
calls the Python code makes directly (`src/swing/llm_backend.py`, governed by
`MODEL_FALLBACK`/`ANALYST_MODEL`/etc.), billed and configured independently
of the routine's own model.

## Known limitations

- Reports are written both to the local artifact path and to the
  `persisted_reports` table, so an ephemeral checkout does not erase the
  audited payload.
- **DST**: the scheduler must use the named `America/New_York` zone. Fixed UTC
  expressions are prohibited because they shift market-relative jobs twice a year.
- **Market holidays**: cron doesn't know NYSE is closed on a given Tuesday;
  the commands are expected to degrade gracefully (no candidates / stale-data
  skip) on those days rather than error, but this isn't independently
  verified for every holiday case.
- **Cold start**: every run is a brand-new sandbox, so `poetry install`
  reinstalls the full dependency set from scratch each time. Expect each
  routine to take a few minutes before it even starts the actual command.
- **Telegram delivery doesn't work from inside these routines.** The
  sandbox's egress proxy blocks outbound HTTPS to `api.telegram.org`
  entirely (`connect_rejected`, "gateway answered 403 to CONNECT (policy
  denial)" — confirmed via the proxy's own status endpoint, not a Telegram-
  side error). Every completed `swing-trader` command still creates a
  durable `notifications` row in the hosted database either way (see
  `src/swing/notification_channels.py`), so nothing is lost -- it just
  can't be delivered from here. Delivery instead runs from
  `.github/workflows/deliver-telegram-notifications.yml`, a scheduled
  GitHub Actions workflow (every 15 minutes) that has normal outbound
  internet access: it connects to the same hosted Turso database and runs
  `swing-trader drain-notifications` to send anything still PENDING.
- **Earnings-date lookups still use yfinance**, not Alpaca — Alpaca's market
  data API has no earnings calendar at all. Yahoo Finance blocks this cloud
  environment's IP range for bulk price history (confirmed: 100%
  connection-reset failures), so `fetch_earnings_trading_days` in
  `src/swing/data_feed.py` is bounded to an 8-second-per-symbol timeout and
  degrades to "unknown, treat conservatively" on failure rather than
  crashing or hanging the whole scan. If Yahoo also blocks this specific
  endpoint, individual-stock candidates fail closed rather than losing
  earnings-exclusion precision. A dedicated earnings-calendar API is required.
- **The environment box is not a secrets vault.** `ANTHROPIC_API_KEY` / `ALPACA_API_KEY` /
  `ALPACA_API_SECRET` / `TURSO_AUTH_TOKEN` live in the cloud environment's
  plain "Environment variables" box, not a real secrets vault — the UI
  itself warns these are visible to anything running in the environment.
  Do not enable paper submission there until access controls, masking, rotation,
  and least-privilege paper-only credentials have been reviewed.
