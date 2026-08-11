# Swarm Trader

A focused, **long-only, deterministic-risk, AI-assisted swing trading** research and
paper-trading framework. It exists to test one question: do three clearly defined
swing setups — `TREND_PULLBACK`, `BREAKOUT_RETEST`, `RELATIVE_STRENGTH_CONTINUATION`
— have real positive expectancy on an approximately $2,000 paper account?

LLMs analyze, critique, and propose. Deterministic Python validates the setup,
controls risk, sizes the position, and decides whether an order is permitted.
**The LLM is never the risk authority.**

> Day trading, mode switching, shorts, leverage, margin, options, crypto, and
> leveraged/inverse ETFs are not supported. `src/swing/risk.py` rejects them and
> there is no configuration that turns them on.

---

## Status

This is a **research and paper-trading** system under active refactor. It is not
production-ready and has never been validated against a real broker account.

**Working today**

- Deterministic daily scanner with real setup-geometry classification (`src/swing/market.py`)
- Deterministic market regime engine (SPY/QQQ trend, slope, realized volatility)
- Transparent weighted candidate score with a configurable buy threshold
- Deterministic final risk authority — sizing, halts, freshness, spread, earnings,
  duplicate-intent, chase, and instrument gates (`src/swing/risk.py`)
- Whole-share, risk-based position sizing from capital at risk
- Portfolio combined-open-risk accounting, serialized inside a database transaction
- Broker-native GTC bracket orders with an idempotent intent key (`src/swing/execution.py`)
- Mandatory broker reconciliation that fails closed on any mismatch (`src/swing/reconciliation.py`)
- R-multiple journaling with MFE/MAE and process-quality postmortems
- Expectancy analytics segmented by setup, regime, sector, and score bucket
- Durable state in local SQLite or hosted Turso (`src/swing/database.py`)
- Human-supervised live workflow that computes a ticket but never places an order

**Known gaps — not yet implemented** (tracked in `docs/refactor/01_AUDIT_REPORT.md`)

- No volatility-aware structural stop enforcement: the stop is proposed by the
  LLM and only geometrically sanity-checked, not required to sit below structure
  or at an ATR-derived distance
- No correlation / sector / cluster exposure limits
- No time stop and no explicit position lifecycle state machine
- No weekly drawdown halt (only 5% risk-reduction and 8% hard halt)
- Universe is a hardcoded ~200-symbol list, not a configurable screen
- No incremental database migrations — the schema version is recorded but never applied
- No structured logging (reason codes are persisted to the database, not logged)
- No notification architecture
- Legacy scripts at the repository root can still reach Alpaca without passing
  through the swing risk engine — see the audit report's bypass-path section

---

## Quick start

```bash
poetry install
cp .env.example .env      # then fill in your keys

# Initialize the durable system of record and inspect safety state
poetry run swing-trader init-db
poetry run swing-trader status

# Run the test suite
poetry run pytest

# Scan only — no LLM calls, no broker calls, no orders
poetry run swing-trader scan

# Review analytics and persist reports
poetry run swing-trader analytics
poetry run swing-trader report daily
```

Safe defaults are `EXECUTION_MODE=paper` and `TRADING_ENABLED=false`. With
`TRADING_ENABLED=false` the full pipeline runs and logs complete proposals but
submits nothing.

---

## How it works

### Code is the risk authority; models only advise

Models (`src/swing/agents.py`, `src/swing/llm_backend.py`) produce *structured
proposals* — a ticker, a setup type, an entry/stop/target, a bull case, a bear
case — never a final "do it." Every proposal passes through `src/swing/risk.py`
and `src/swing/execution.py` before anything reaches a broker.
`SwingSettings.__post_init__` rejects startup outright if any risk parameter is
configured looser than its immutable floor. If a model role has no configured
backend, or returns output that fails schema validation, the result is
`NO_TRADE` — never a fallback guess.

```
Alpaca (daily bars) + yfinance (earnings, bounded timeout)
    │
    ▼
DeterministicSwingScanner                     src/swing/market.py
  scores a liquid universe purely on math — trend, relative strength,
  volume, setup geometry, event risk. No LLM. Emits ranked candidates.
    │
    ▼
AgentPipeline                                 src/swing/agents.py
  only candidates already above the score threshold get LLM analysis:
  technical read, fundamental/event read, bull case, independent red-team
  bear case with a named checklist, then a portfolio-manager verdict
    │
    ▼
SwingRiskManager → SwingExecutionService      risk.py / execution.py
  re-reads halts, positions, and open risk from the database on every call;
  sizes the position from capital at risk; rejects anything over a ceiling
    │
    ▼
Broker
  Alpaca paper  → autonomous, GTC bracket (entry + stop + target)
  Robinhood live → human-supervised ticket only; this code never places it
```

### Risk model in one paragraph

Sizing is driven by current broker equity, not a fixed constant. `risk_per_share
= live quote − technical stop`; `quantity = floor(equity × applied_risk_pct /
risk_per_share)`, whole shares only. That quantity is then only ever *reduced* by
cash, buying power, and the maximum-position-exposure ceiling — never increased to
make a share fit. Normal risk is 0.50%, an explicitly qualified A+ candidate may
request 0.75%, and 1.00% is a hard maximum. At 5% drawdown risk drops to 0.30%;
at 8% a durable halt latches and requires a recorded human approval to clear.
Combined planned open risk across all positions is capped at 2% of equity, which
is the real constraint on concurrency. Full detail: [docs/RISK_MODEL.md](docs/RISK_MODEL.md).

### The daily agent fleet

The daily cycle is split into five independently scheduled cloud routines, each
running exactly one `swing-trader` subcommand
(see [docs/CRON_AGENTS.md](docs/CRON_AGENTS.md)):

| Agent | Runs | Job |
|---|---|---|
| **scan-agent** | 9:35am ET, weekdays | Market scan — candidates only, no LLM, no orders |
| **decide-trade-agent** | 10:03am ET, weekdays | Re-scans, runs LLM analysis, submits paper orders if `TRADING_ENABLED=true` |
| **reconcile-agent** | 3:55pm ET, weekdays | Confirms broker state matches the journal; halts on any mismatch |
| **report-agent** | 4:15pm ET, weekdays | Writes the daily report |
| **lessons-agent** | Sundays | Aggregates postmortems into candidate hypotheses — never auto-validates |

Each routine's blast radius is exactly the one subcommand it is allowed to run.

### State: a hosted database, not git

`SwingDatabase` connects to a hosted Turso (libSQL) database when
`TURSO_DATABASE_URL` is set, so the kill switch, drawdown and loss-streak halts,
the trade journal, candidate scores, and lessons all persist across ephemeral
cloud sandboxes with no save step. With that variable unset it falls back to a
local SQLite file, so local development and the test suite never touch Turso.

### Live trading is human-supervised by design

`decide-trade-agent` can run unattended, but only against Alpaca's **paper**
account. Real money goes through a deliberately non-automatable path:
`swing-trader live-ticket` runs the same unchanged risk engine against a
human-typed account snapshot and prints an approved ticket; a human places that
order themselves; `record-live-fill` / `record-live-exit` journal what actually
happened. Nothing in this codebase calls a live order-placement endpoint. See
[docs/ROBINHOOD_MCP.md](docs/ROBINHOOD_MCP.md).

---

## Commands

```bash
swing-trader init-db                      # Initialize / inspect the system of record
swing-trader status                       # Kill switches, halts, mode, record counts
swing-trader scan                         # Deterministic scan; no LLM, no broker writes
swing-trader run                          # Full daily cycle (paper)
swing-trader reconcile                    # Reconcile broker truth against the journal
swing-trader paper-review <intent.json>   # Risk-check a proposal; add --submit to send
swing-trader tighten-stop <...>           # The only supported stop amendment path
swing-trader analytics                    # Expectancy-first analytics
swing-trader report <type>                # daily | weekly | 20-trade | 50-trade | 100-trade | graduation
swing-trader kill-switch on|off --approved-by <name>
swing-trader clear-drawdown-halt --approved-by <name>
swing-trader clear-loss-streak-halt --approved-by <name>
swing-trader review-observations          # Aggregate postmortems into hypotheses
swing-trader live-ticket <snapshot.json>  # Compute a live ticket; never places an order
swing-trader record-live-fill <file>      # Journal a fill a human executed
swing-trader record-live-exit <id> <price> --reason <text>
```

---

## Configuration

All settings are environment variables read by `src/swing/config.py`. See
`.env.example`. Every risk parameter may be made **stricter**; startup fails if
any is made weaker than its immutable floor.

Model roles (`ANALYST_MODEL`, `PORTFOLIO_MANAGER_MODEL`, `POSTMORTEM_MODEL`,
`RESEARCH_MODEL`, `MODEL_FALLBACK`) are independently overridable. An empty or
unavailable role fails to `NO_TRADE`.

`config/do_not_trade.yaml` holds long-term holdings and wash-sale-sensitive
equivalents that must never be traded. It ships empty and must be populated
before any real use.

---

## Documentation

| Document | Covers |
|---|---|
| [docs/ADAPTIVE_SWING_ARCHITECTURE.md](docs/ADAPTIVE_SWING_ARCHITECTURE.md) | Component boundaries and target architecture |
| [docs/RISK_MODEL.md](docs/RISK_MODEL.md) | Sizing, halts, and every entry gate |
| [docs/LEARNING_ENGINE.md](docs/LEARNING_ENGINE.md) | Postmortem → observation → hypothesis → human approval |
| [docs/BROKER_PROVIDERS.md](docs/BROKER_PROVIDERS.md) | Broker abstraction and reconciliation |
| [docs/CRON_AGENTS.md](docs/CRON_AGENTS.md) | The five scheduled routines and their limitations |
| [docs/EXPERIMENT_PROTOCOL.md](docs/EXPERIMENT_PROTOCOL.md) | The $2,000 experiment and graduation criteria |
| [docs/PAPER_TO_LIVE_CHECKLIST.md](docs/PAPER_TO_LIVE_CHECKLIST.md) | What must be true before real money |
| [docs/EMERGENCY_PROCEDURES.md](docs/EMERGENCY_PROCEDURES.md) | Halting, reconciling, and clearing latches |
| [docs/REMEDIATION_STATUS.md](docs/REMEDIATION_STATUS.md) | Prior audit remediation and what remains blocked |
| [docs/AUTORESEARCH_SWING.md](docs/AUTORESEARCH_SWING.md) | Swing hypothesis validation and the human promotion gate |
| [docs/ROBINHOOD_MCP.md](docs/ROBINHOOD_MCP.md) | Why live order placement is never automated |
| [docs/REFACTOR_RULES.md](docs/REFACTOR_RULES.md) | Standing rules for the ongoing refactor |
| [docs/refactor/](docs/refactor/) | Per-prompt audit and refactor reports |

---

## Repository layout

```
src/swing/                 The production package — the only supported path
  market.py                Deterministic scanner, regime engine, setup classification
  risk.py                  Final deterministic risk authority
  execution.py             Idempotent review → admission → submission
  reconciliation.py        Broker/database truth reconciliation, fails closed
  stops.py                 The only supported stop-amendment boundary
  brokers/                 BrokerProvider + Alpaca paper, fake, human-supervised
  config.py  models.py  database.py  universe.py  data_feed.py
  agents.py  llm_backend.py  postmortem.py  analytics.py  reporting.py
src/autoresearch_swing/    Swing hypothesis records, fitness, human promotion gate
tests/swing/               Swing test suite — no test makes a network call
docs/                      Current documentation
docs/archive/              Pre-refactor documents, historical reference only
autoresearch/              QUARANTINED intraday experiment — see autoresearch/README.md
app/                       Legacy FastAPI/React research UI for the personality swarm
src/agents/ src/backtesting/ src/tools/   Legacy imported hedge-fund research code
*.py (repository root)     Legacy operational scripts — not the swing path
```

---

## Historical Note

This repository began as a fork of
[virattt/ai-hedge-fund](https://github.com/virattt/ai-hedge-fund) and was
extended into a dual-mode (swing + intraday day trading) system with ~20
investor-personality LLM agents, autonomous mode switching via a
`trading_mode.json` steering file, separate day/swing Alpaca accounts, short
selling, leveraged-ETF exposure, and an overnight strategy-evolution loop
(`autoresearch/`). That architecture was abandoned: it was unfalsifiable, its
risk limits were advisory rather than enforced, and it let LLM discretion set
position size.

The current system replaces all of it with three explicit setups, deterministic
risk code that no model output can weaken, and a single long-only paper
execution path. Documents describing the predecessor now live in
[docs/archive/](docs/archive/) and `autoresearch/`; they are provenance, **not
operating instructions**, and their commands, risk numbers, and cron examples are
not supported. The legacy code still present under `src/agents/`,
`src/backtesting/`, `app/`, and the repository root is retained as reference and
is not invoked by the swing path — removing it is tracked in
`docs/refactor/01_AUDIT_REPORT.md`.

---

## Disclaimer

This project is for education and research. It is not financial advice, has not
been validated against a live broker, and comes with no warranty. Do not commit
real capital to it. You are solely responsible for anything you run.

## License

MIT — see the upstream project for the original terms.
