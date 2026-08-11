# Daily Cloud Routines (Cron Agents)

This document describes how `src/swing` is operated by a fleet of scheduled
cloud agents ("routines," created via Claude's remote-trigger API) instead of
a human running the CLI by hand every day.

## Why this needed a design decision, not just a cron entry

Cloud routines each spin up a **fresh, ephemeral sandbox** on every fire: a
clean `git clone` of this repo, no persistent disk, no access to the local
machine that authored this doc. `src/swing`'s safety model — the kill switch,
the drawdown halt, the consecutive-loss halt, the trade journal a postmortem
engine reads lessons from — all lives in `data/adaptive_swing.db`
(`SwingDatabase`, see `src/swing/database.py`). If that state doesn't survive
between runs, the halts silently reset to "off" every single day, which
defeats the entire point of `src/swing/risk.py`.

**Decision: `data/adaptive_swing.db` is deliberately tracked in git**
(see the `!data/adaptive_swing.db` exception in `.gitignore`), and every
routine round-trips it: pull the latest commit, run its one command, commit
the updated DB (and any `reports/` output) back, push. `SwingExecutionService`
already re-reads `kill_switch` / `drawdown_halt` / `loss_streak_halt` /
`reconciliation_halt` straight from the database on every `submit()` call
(`src/swing/execution.py`), so as long as the DB state round-trips correctly,
the existing risk engine enforces the halts automatically — no special-casing
needed in the routine prompts themselves.

This is a pragmatic choice, not the "correct" one: SQLite-in-git means
**routines must never run concurrently** (two overlapping writers racing to
push `adaptive_swing.db` is a real corruption risk). That's why the five
routines below are spaced at least 20 minutes apart in the daily sequence.
If this system ever needs true concurrent agents, migrate `SwingDatabase` to
a hosted database (e.g. Turso/libSQL) first.

## The five routines

Each routine runs exactly one `swing-trader` subcommand, wrapped in the same
pull → run → commit → push protocol. None of them touch the Robinhood
live-trading path (`live-ticket`, `record-live-fill`, `record-live-exit`) —
those remain human-only per `docs/ROBINHOOD_MCP.md`.

| # | Name | Cron (UTC) | ET (approx, EDT) | Command | Needs |
|---|------|-----------|-------------------|---------|-------|
| 1 | scan-agent | `35 13 * * 1-5` | 9:35am | `scan` | nothing (no LLM, no broker) |
| 2 | decide-trade-agent | `3 14 * * 1-5` | 10:03am | `run` | `ANTHROPIC_API_KEY`, `ALPACA_API_KEY`, `ALPACA_API_SECRET` |
| 3 | reconcile-agent | `55 19 * * 1-5` | 3:55pm | `reconcile` | `ALPACA_API_KEY`, `ALPACA_API_SECRET` |
| 4 | report-agent | `15 20 * * 1-5` | 4:15pm | `report daily` | nothing |
| 5 | lessons-agent | `7 22 * * 0` | Sun 6:07pm | `review-observations` | nothing (never auto-validates) |

`TRADING_ENABLED` is deliberately **not** set, matching `src/swing/config.py`'s
fail-closed default (`trading_enabled: bool = False`). Until it's explicitly
set to `true` in the cloud environment, `decide-trade-agent` will compute and
log full proposals every day but submit nothing — a rehearsal period before
real (paper) orders go out. Flip it on when you're satisfied watching it.

## Known limitations

- **DST**: the UTC offsets above assume EDT (UTC-4). They drift by an hour
  during EST (roughly early Nov–mid Mar); the cron expressions need a manual
  ±1h nudge at each DST transition, or update to ET-aware scheduling.
- **Market holidays**: cron doesn't know NYSE is closed on a given Tuesday;
  the commands are expected to degrade gracefully (no candidates / stale-data
  skip) on those days rather than error, but this isn't independently verified
  for every holiday case.
- **Cold start**: every run is a brand-new sandbox, so `poetry install`
  reinstalls the full dependency set (langchain family included) from
  scratch each time. Expect each routine to take a few minutes before it
  even starts the actual command.
- **Secrets**: `ANTHROPIC_API_KEY` / `ALPACA_API_KEY` / `ALPACA_API_SECRET`
  must be configured on the cloud **environment** the routines run in. There
  is no API-level mechanism for setting these from outside claude.ai's UI —
  check the environment's settings at https://claude.ai/code/routines.
