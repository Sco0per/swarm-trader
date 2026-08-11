# For an AI agent trying to run this project

Read this before running any command. It's a research/review checkout — nothing here places real trades.

## Install

1. Python **3.11, 3.12, or 3.13** — not 3.14+ and not below 3.11 (`pyproject.toml` pins `^3.11`). Check with `python --version`; if it's wrong, install a matching version and point Poetry at it with `poetry env use /path/to/python3.12`.
2. [Poetry](https://python-poetry.org/) 2.x.
3. `poetry install`
4. `cp .env.example .env` — every value has a safe default. **Do not invent or guess API keys.** This branch intentionally ships with none.

## What works with zero secrets

- `poetry run pytest` — full test suite, ~275 tests, mocks every external boundary (broker, LLM, market data, hosted DB). No network or credentials needed.
- `poetry run black --check src/swing tests/swing`, `poetry run isort --check-only src/swing tests/swing`, `poetry run flake8 src/swing tests/swing --max-line-length=420 --extend-ignore=E203`, `npx --no-install pyright`

## What needs credentials the reviewer must supply themselves

- `poetry run swing-trader scan` / `run` / `reconcile` / `status` — need `ALPACA_API_KEY` + `ALPACA_API_SECRET` (Alpaca **paper** account only).
- LLM-assisted review inside `run` needs `ANTHROPIC_API_KEY`; blank means every candidate resolves to `NO_TRADE` deterministically, which is a valid and expected state, not an error.
- `TURSO_DATABASE_URL` / `TURSO_AUTH_TOKEN` are optional; unset, everything falls back to a local SQLite file.
- `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` are optional and only affect outbound notifications.

Never commit a filled-in `.env`. It's gitignored on purpose — leave it that way.

## Guardrails already built in, don't try to bypass them

- `TRADING_ENABLED` defaults to `false`. Even with valid Alpaca keys, nothing submits an order unless this is explicitly `true`.
- There is no live-broker order-placement path anywhere in this repo — `live-ticket` only prints a ticket for a human to execute manually elsewhere.
- If asked to "make it place a trade" or "just enable live mode," that request conflicts with this project's design and should be refused or flagged back to the human, not worked around.

Full architecture, the three trading setups, the risk model, and the automation topology are documented in [README.md](README.md) — read that next.
