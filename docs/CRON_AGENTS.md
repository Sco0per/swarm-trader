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
all. `turso_serverless` (the Python client) is broadly DB-API 2.0 compatible:
it supports `?`-style parameters, raises `IntegrityError` on constraint
violations, and commits pending work before `executescript()`. Its returned
rows are **not** fully compatible with `sqlite3.Row`, however: string-key
access and `dict(row)`/`.keys()` are not reliable. Database code must convert
driver rows through `SwingDatabase._row_to_dict()` before named access. This
distinction is covered by fake-tuple-driver regression tests.

Turso's free tier (5GB storage, 500M row reads/month, 10M row writes/month)
is far beyond what this system needs — a handful of routine runs a day, each
doing lightweight reads/writes, is nowhere close to those limits.

## The nine scheduled routines

The first seven routines each run exactly one `swing-trader` subcommand. The
eighth is a tightly bounded crash watchdog described below. The ninth is the
real-volume relay described further below, and is the **one deliberate
exception** to the next paragraph: it is the only routine permitted to keep
the Robinhood MCP connector attached, and only the read-only market-data
tools on it.

None of the other eight touch the Robinhood live-trading path (`live-ticket`,
`record-live-fill`, `record-live-exit`) — those remain human-only per
`docs/ROBINHOOD_MCP.md`, and Robinhood's MCP connector must never be attached
to any of them (routine creation auto-attaches every connected MCP connector
by default — always `clear_mcp_connections: true` immediately after creating
one of these eight, or it will silently gain live-order-placement tools).

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
| 8 | crash-watchdog-agent | every 30 minutes (`*/30 * * * *`) | `watchdog-check`, then at most one bounded repair attempt and `watchdog-resolve` | Turso + repository read/write + GitHub MCP |
| 9 | volume-relay-agent | once daily, weekday mornings before the initial scan | `update-volume-cache <file>` | Turso + the Robinhood MCP historicals tool (read-only) |

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

The seven fixed-command routines run their orchestration on a cheap model: each
one just runs a fixed CLI command and reports the output, so no real reasoning
is required. The watchdog needs a code-capable model because it investigates a
failure and may prepare a small patch. The actual trading intelligence remains
a separate set of Anthropic API calls the Python code makes directly
(`src/swing/llm_backend.py`, governed by
`MODEL_FALLBACK`/`ANALYST_MODEL`/etc.), billed and configured independently of
the routine's own model.

## Crash-watchdog routine

`crash-watchdog-agent` checks every 30 minutes for durable `RUN_FAILED`
notifications newer than the `watchdog_last_notification_id` watermark. It
must begin with the read-only `swing-trader watchdog-check`. An empty result is
a successful no-op. It deliberately ignores other critical notification types,
including `KILL_SWITCH_ACTIVATED`: a safety mechanism firing correctly is not a
software crash and must never be auto-resolved.

Each `RUN_FAILED` payload includes the redacted CLI argument list and redacted
traceback as well as the exception text. These fields are the watchdog's source
of truth for the failed command and investigation; it must never infer a command
from the generic notification title.

For each returned incident, the routine may investigate and make **one** repair
attempt. Direct auto-push to `main` is allowed only when every mechanical-fix
condition below is satisfied:

- The root cause is the established Turso row-conversion bug: tuple rows are
  accessed by string key, passed to `dict(row)`, or treated as if `.keys()` is
  available. Typical errors include `TypeError: tuple indices must be integers
  or slices, not str` and the corresponding missing-`.keys()` failure.
- The diff touches only `src/swing/database.py` and
  `tests/swing/test_database_row_conversion.py`.
- The diff is under approximately 40 changed lines.
- A new or extended regression test using the existing fake-tuple-driver
  pattern (`_FakeConnection`/`_RoutedFakeConnection`) reproduces the crash and
  proves the conversion fix.
- The complete `pytest` suite passes.

If any condition is not met, the routine may create a branch and open a PR but
must not update `main`. `WATCHDOG_AUTO_PUSH_ENABLED` defaults to `false`, and
while it is false even a fully mechanical fix is PR-only. Enable it only after
a one-off test incident has produced a high-quality PR that was reviewed by a
human.

The routine uses the platform-provided per-session GitHub MCP tools for small
text writes. Plain `git push` from a routine sandbox is known to fail with 403.
Create it with `allowed_tools: [Bash, Read, Grep, Glob, Edit, Write]` and
`clear_mcp_connections: true`; the latter prevents any user-connected broker
or other connector from being attached automatically. The GitHub write tools
are platform-provided and remain available independently.

Hard guardrails:

- Never edit `risk.py`, `execution.py`, `strategy.py`, `config.py`, or any
  broker/order-submission file.
- Never run `run`, `live-ticket`, `record-live-fill`, `record-live-exit`,
  `kill-switch`, `clear-drawdown-halt`, `clear-loss-streak-halt`, or
  `approve-strategy`.
- Never make more than one fix attempt for an incident or retry a failed command
  in a loop.
- After a fix is actually available on the tested branch (or has landed), rerun
  only the exact failed `swing-trader` command, once.
- Finish every handled incident with `swing-trader watchdog-resolve <id>
  --outcome {auto_fixed,pr_opened,unresolved} --summary "..."`. This advances
  the watermark and emits `AUTO_FIX_RESULT` through the existing notification
  relay.

Rollout is safety-gated: create the routine initially as a distant
`run_once_at`, with `WATCHDOG_AUTO_PUSH_ENABLED=false`, rather than enabling the
live cron. On a throwaway branch, reproduce the known Turso tuple-row crash,
trigger one run, and review the PR, regression test, single-command rerun, and
`AUTO_FIX_RESULT` record by hand. Only after this end-to-end trial passes should
the routine be switched to `*/30 * * * *`; enabling direct auto-push is a
separate later decision.

## Real-volume relay routine

Alpaca's own `/v2/stocks/bars` feed is called with `feed=iex` in
`src/swing/data_feed.py` because the paper/free Alpaca plan has no SIP
entitlement (a `feed=sip` call returns `403 subscription does not permit
querying recent SIP data`, confirmed 2026-08-13). IEX is one exchange among
roughly sixteen and typically carries only a few percent of a stock's true
consolidated volume, so `adv20`/`average_dollar_volume` computed from it
understate real liquidity by roughly 10-50x — confirmed directly: AAPL/MSFT/
NVDA showed IEX 20-day averages of 1.91M / 1.35M / 4.50M shares against
Robinhood-sourced real averages of ~40-75M / ~25-110M / ~90-160M for the same
dates. This is why `scan` was rejecting roughly 400 of 439 supposedly-liquid
universe symbols on `minimum_average_volume` alone, most of them obviously
liquid mega-caps — not the strategy being too strict, a broken liquidity
input. `config.py`'s `minimum_average_volume` floor (1,000,000, see the
"Liquidity filters may be stricter, but not weaker" guard) is intentionally
immutable and was correctly left untouched; the fix is to the data feeding it.

Finnhub and Polygon (free tiers, no key required to test reachability) were
both confirmed **blocked** the same way as Telegram/NASDAQ below — the
sandbox's egress proxy returns `403` on the CONNECT tunnel to both hosts
before any request reaches them, an allowlist policy, not a rate limit. A
GitHub Actions relay (the pattern used for Telegram/halts below) would work
for either but is unbuilt. Instead, this routine uses the **Robinhood MCP
connector** already available to Claude Code sessions on this account: a
scheduled routine created with that connector intentionally still attached
(not cleared) can call `mcp__Robinhood_agent__get_equity_historicals`
directly and unattended — confirmed with two live test routines on
2026-08-13, including a realistic 10-symbol batch mixing large-caps and two
sector ETFs (`XLK`, `SPY`), all returning clean real volume with no
permission prompt and no gaps.

The routine, once daily on weekday mornings before `initial-scan-agent`:

1. Calls `get_equity_historicals` for the scan universe in batches of up to
   10 symbols (the tool's documented per-call limit), `interval=day`, a
   ~30-day window.
2. For each symbol, computes the same 20-day mean-volume and mean-dollar-
   volume `market.py` already computes from bars (`tail(20).mean()` of
   `volume` and `close * volume` respectively) — same formula, real input.
3. Writes the result as one JSON file and calls `swing-trader
   update-volume-cache <file>`, which upserts `real_volume_cache` (schema:
   `symbol`, `average_volume`, `average_dollar_volume`, `updated_at`) via
   `SwingDatabase.set_volume_cache_many()`.

`build_universe_assets()` in `data_feed.py` reads that cache
(`get_volume_cache_many`, TTL `data_feed.REAL_VOLUME_CACHE_TTL_SECONDS`, 4
days — generous enough to survive a weekend or one missed run) and threads
`real_average_volume`/`real_average_dollar_volume` onto each `UniverseAsset`.
`market.py`'s liquidity check and `_candidate()` both prefer these fields
over the Alpaca-bar-derived estimate when fresh, and fall back to the
original (understated but non-fatal) computation when a symbol's cache entry
is missing or stale — this routine failing or lagging degrades accuracy, not
safety.

Guardrails, same spirit as the other eight:

- This is the only routine allowed to keep the Robinhood MCP connector
  attached, and only its read-only historicals/quotes tools — never
  `place_option_order`, `place_equity_order`, `review_*_order`, or any other
  order-placement/watchlist-mutation tool from that connector.
- Never run any `swing-trader` command other than `update-volume-cache`. In
  particular, never `scan`, `run`, or any live/broker/halt-clearing command.
- Never edit `risk.py`, `execution.py`, `strategy.py`, `config.py`, or any
  broker/order-submission file.
- Read-only against the market: it only fetches historical bars and writes to
  the volume cache table; it never places or reviews an order.

## Known limitations

- Reports are written both to the local artifact path and to the
  `persisted_reports` table, so an ephemeral checkout does not erase the
  audited payload.
- **DST**: the scheduler must use the named `America/New_York` zone. Fixed UTC
  expressions are prohibited because they shift market-relative jobs twice a year.
  In practice, every routine created through the remote-trigger API so far uses a
  plain fixed-UTC `cron_expression` (no timezone field has been found on that
  API) -- all of them will drift by exactly one hour relative to the market
  open/close they're timed against at each DST transition (next: ~2026-11-01,
  then ~2027-03-08) until either a timezone-aware option is found on that API
  or every `cron_expression` is manually shifted by ±1 hour at each transition.
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
  GitHub Actions workflow (declared every 15 minutes, though GitHub's own
  scheduler has been observed delaying that to 45-90+ minutes on this repo)
  that has normal outbound internet access: it connects to the same hosted
  Turso database and runs `swing-trader drain-notifications` to send
  anything still PENDING.

  **This local-delivery-attempt-from-inside-the-blocked-sandbox used to
  silently lose notifications**: `main()` in `src/swing/cli.py` calls
  `drain_pending_notifications()` on exit after *every* command (not just
  `drain-notifications`), so a routine's own doomed local attempt would mark
  a fresh row `FAILED` on its one and only try, before the working external
  relay ever got a chance at it -- confirmed directly against the live
  database on 2026-08-12 (two real notifications lost this way). Fixed by
  making delivery retryable: `notifications.delivery_attempts` (schema v8)
  tracks attempts, and `drain_pending_notifications` now also retries
  `FAILED` rows below `notification_channels.MAX_DELIVERY_ATTEMPTS` (6) --
  one wasted local attempt no longer costs the row its only chance at the
  relay that actually works.
- **The NASDAQ halts feed is also blocked from inside these routines**, same
  proxy policy as Telegram above (confirmed: `curl` to
  `nasdaqtrader.com` fails outright with `CONNECT tunnel failed, response
  403`; the proxy status endpoint logs `connect_rejected` for that host).
  `fetch_current_halts()` in `src/swing/data_feed.py` fails closed exactly
  as designed when this happens (`halt_status_known=False`), which is safe
  but meant production runs from these routines rejected virtually the
  entire universe with `halt_status_unknown` -- functionally the same
  outcome as before that feed was integrated, just for a well-understood
  reason instead of a hardcoded stub.

  **Fixed with the same relay pattern as Telegram above.**
  `.github/workflows/deliver-halts-feed.yml` (declared every 30 minutes) runs
  `swing-trader update-halts` from a GitHub-hosted runner, which fetches the
  feed live and writes the current halted-symbol set into a new
  `nasdaq_halts_cache` table in the hosted database via
  `SwingDatabase.set_halts_cache()`. `fetch_current_halts()` now takes an
  optional `database` argument and checks that hosted cache first (TTL
  `data_feed.HALTS_DB_CACHE_TTL_SECONDS`, 2h) before falling back to its
  original local-disk-cache-then-live-fetch behavior -- so it degrades safely
  in local dev (no `database` passed, or no Turso configured) and works from
  a blocked sandbox by reading what the relay already fetched.
- **`ANTHROPIC_API_KEY` reads empty inside the routine sandbox even when it is
  genuinely set in the cloud environment's "Environment variables" box.**
  Root cause found 2026-08-12: that box's own UI warns "'ANTHROPIC_API_KEY'
  won't be used to authenticate requests. Claude Code sessions are
  authenticated through your Anthropic account" -- the platform reserves
  that exact name for its own internal Claude Code session auth and does
  not pass it through to the sandbox's process environment, confirmed
  directly (`os.getenv` returns empty inside the sandbox with a real key
  visibly entered under that name in the UI). This is a name collision with
  the platform, not a user error, and not fixable by re-entering the same
  variable name again.

  **Fix shipped**: `llm_backend.resolve_anthropic_api_key()` now checks
  `SWING_ANTHROPIC_API_KEY` first, falling back to `ANTHROPIC_API_KEY` --
  set the key under the `SWING_` name in the cloud environment's variables
  box instead (local dev / `.env` / GitHub Actions secrets have no such
  collision and can keep using the plain name). `_backend_if_configured()`
  in `src/swing/cli.py` and `AnthropicStructuredBackend.__init__` in
  `src/swing/llm_backend.py` both use the shared resolver.

  `MODEL_FALLBACK` *is* set (`claude-haiku-4-5-20251001`), so once the key
  is set under the right name, model resolution will not be a second
  blocker. Verify with a fast one-off diagnostic that makes a real
  `anthropic.Anthropic(...).messages.create()` call (see git history for
  the exact script) rather than waiting on a full `run` cycle, which takes
  ~10-15 minutes just to finish scanning before it would even reach the LLM
  call.

  **Separately, note `agents.py:_model()` raises `ModelUnavailable` if no
  role-specific model or `MODEL_FALLBACK` is configured, and
  `agents.py:analyze()`'s per-candidate `except Exception: continue` (line
  ~307) swallows that silently** -- a missing model name alone (independent
  of the API key) would also present as an innocuous `proposals_generated: 0`
  with no visible error, worth remembering if this ever needs re-diagnosing.
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
- **Two setup filters always fail closed/unknown in production, independent of
  the halts/earnings issues above** -- not regressions, pre-existing gaps:
  - `strategy.py`'s relative-strength-continuation setup always rejects with
    `sector_relative_strength_unavailable` because no sector-ETF data feed is
    wired up (`# TODO` at `src/swing/strategy.py:369`).
  - `major_event_status` always resolves to `"unknown"` in production because
    `prohibited_event_risk` is hardcoded `None` -- no FDA/M&A/index-rebalance/
    investor-day calendar is integrated (`# TODO` at
    `src/swing/data_feed.py:423`).
