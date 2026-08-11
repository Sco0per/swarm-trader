# Ultimate Swing Trader Refactor — Standing Rules

Save this file into the repo as `docs/REFACTOR_RULES.md` and commit it **before running prompt 01**.

Every prompt in this series begins with: _"Read `docs/REFACTOR_RULES.md` and obey it for the entire session."_ Do not paste these rules into each prompt — keep one copy in the repo so the rules can't drift between runs.

---

## 0. Repository state — read this before assuming anything

This repository has **already undergone a partial adaptive-swing refactor.** The following are reported as complete in the current README. Verify each claim against the code; do not assume it is false, and do not redo it:

- Day trading, mode switching, shorts, leverage, margin, options, and crypto are described as disabled
- `src/swing/` exists as the production package (`market.py`, `agents.py`, `risk.py`, `execution.py`, `config.py`, `database.py`, `universe.py`, `llm_backend.py`)
- Only `TREND_PULLBACK`, `BREAKOUT_RETEST`, `RELATIVE_STRENGTH_CONTINUATION` are valid setup types
- `SwingSettings.__post_init__` rejects startup if any risk parameter is looser than its immutable floor
- Durable state lives in hosted Turso (libSQL), falling back to local SQLite when `TURSO_DATABASE_URL` is unset
- The daily cycle is split into five scheduled agents (scan / decide-trade / reconcile / report / lessons)
- `portfolio_monitor.py` is a fail-closed shim that cannot read the account or place orders
- Live execution is human-supervised only via `live-ticket` / `record-live-fill` / `record-live-exit`; nothing calls a live order endpoint

**Your job across this series is to close remaining gaps, not to re-derive the architecture.** If you believe something above is wrong, prove it with a file reference before acting on it.

---

## 1. Resolved decisions — treat as settled, do not relitigate

| Decision                       | Value                                                                                                                           | Consequence                                                                                                                              |
| ------------------------------ | ------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------- |
| Risk per trade (default)       | **0.75%** of equity                                                                                                             | ~$15 on the $2,000 experiment account                                                                                                    |
| Max total concurrent open risk | **2.00%** of equity                                                                                                             | Caps real concurrency at 2–3 positions; the max-positions limit is a secondary ceiling, not the binding constraint. This is intentional. |
| Share sizing                   | **Whole shares only. Fractional shares are prohibited.**                                                                        | Alpaca fractional orders cannot carry bracket/stop legs; broker-native protection wins.                                                  |
| Position sizing authority      | **Risk-based size is authoritative.** Max-position-percent is a ceiling that can only ever reduce the quantity, never raise it. | A tight stop must not be allowed to justify an oversized position.                                                                       |
| Max individual position        | 30–35% of equity, configurable                                                                                                  | Ceiling only. It will rarely bind at 0.75% risk with realistic structural stops.                                                         |
| Max new positions per day      | 2                                                                                                                               |                                                                                                                                          |
| Minimum initial R:R            | 2.0                                                                                                                             |                                                                                                                                          |
| Weekly drawdown halt           | 4–5%                                                                                                                            |                                                                                                                                          |
| Portfolio drawdown halt        | 8–10%                                                                                                                           |                                                                                                                                          |
| Averaging down                 | Prohibited                                                                                                                      |                                                                                                                                          |
| AutoResearch rebuild           | **Out of scope for this series.** Isolate it from production and mark it not-swing-validated. Do not rebuild the backtester.    |                                                                                                                                          |

---

## 2. Absolute prohibitions

1. **Never execute any command that can place, amend, or cancel a broker order** — paper included. That means never running `swing-trader run`, `paper-review --submit`, `tighten-stop`, `close`, `execute_trades.py`, or anything equivalent. Read-only commands (`status`, `scan`, `analytics`, `init-db` against a local SQLite file) are permitted.
2. **No test may make a real network call** to an LLM provider, Alpaca, yfinance, SEC EDGAR, or Turso. Mock every external boundary. A test suite that requires credentials to pass is a failed test suite.
3. **Never write, read for value, or print secrets.** Do not create or modify `.env`. `.env.example` may be edited (placeholder values only). Never let a key reach a log, report, prompt, database row, or commit.
4. **Do not enable unattended live trading** or attach a live broker connector to any scheduled routine.
5. **Do not weaken any existing safety control**, and do not raise any configured ceiling, even if it blocks a feature you are building. If a safety limit genuinely blocks correct behavior, stop and report it instead of changing it.

---

## 3. Git discipline

- Work on a branch named `refactor/<prompt-number>-<short-name>` cut from the current HEAD.
- Commit per logical unit with a message stating what changed and why. Do not produce one giant commit.
- Never rewrite history, force-push, amend a pushed commit, or delete a branch.
- Do not merge to main. Leave the branch for human review.
- If the working tree is dirty when you start, stop and report it. Do not stash or discard the user's work.

---

## 4. Fail-closed engineering rules

- Missing, stale, malformed, or conflicting safety-critical data (quotes, bars, earnings dates, halt status, spread, broker asset state, broker reconciliation) → **reject the trade**. Never substitute an optimistic default.
- LLM schema failure, timeout, unavailable model, unconfigured role, unknown ticker, or invalid setup name → **`NO_TRADE`**. Never a fallback guess.
- If a data source you need for a _new_ feature does not exist in the repo (e.g. sector→cluster mapping, sector ETF relative strength), implement the gate as **fail-closed with an explicit `TODO` and a reason code**, and list it in your report. Do not stub it to return "pass".
- Execution must be idempotent. A repeated command must never duplicate a position.

---

## 5. Scope and dependency rules

- Do not add a new third-party dependency without stating why in your report. Prefer the standard library and what `pyproject.toml` already declares.
- Do not blindly delete a file until you have grepped for every import and CLI reference to it.
- Do not delete a meaningful test to make the suite green. Replace legacy tests with equivalent swing-only tests.
- Prefer simple, deterministic, testable, observable, fail-closed code over sophisticated architecture. No ML, RL, covariance optimizers, or new agent swarms.

---

## 6. Static-search discipline

When sweeping for legacy terms, use **word-boundary matching** and produce a review list — do not edit on a raw substring match. These are legitimate and must not be touched:

- `day` inside `days_held`, `trading days`, `max_hold_days`, `expected_hold_days`, `20-day`, `50-day`
- `short` inside `shorter`, `short-term`, `short trend measure`
- `margin` inside `margin of safety`, `profit margin`, `operating margin`
- Any mention inside migration notes, changelogs, or historical documentation

Active _production behavior_ enabling day trading, shorts, leverage, margin, options, crypto, or leveraged ETFs is what must be absent — not the strings themselves.

---

## 7. Definition of done for every prompt

A prompt is not complete until all of the following hold:

1. `poetry run pytest` passes, with no test skipped to achieve it
2. Any configured lint / format / type check passes
3. The branch is committed and the working tree is clean
4. A report file exists at `docs/refactor/<NN>_<name>_REPORT.md` (see §8)
5. You have performed a final repo-wide grep to confirm you introduced no new path that bypasses the risk engine

If you cannot reach all five, say so plainly and list what is incomplete. **Do not report success on partial work.**

---

## 8. Reporting

Write your full report to `docs/refactor/<NN>_<name>_REPORT.md` in the repo and commit it. In chat, give **at most 15 lines**: what you changed, what you deliberately did not change, what broke, and what the next prompt needs to know.

Every report must end with an explicit list of:

- **Assumptions I made** that a human should verify
- **Fail-closed TODOs** I left in place
- **Anything I could not verify** without real credentials or a live broker

Never write "production-ready" about anything that still requires credential or broker QA.
