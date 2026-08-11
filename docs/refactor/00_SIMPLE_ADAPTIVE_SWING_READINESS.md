# Simple Adaptive Swing — Readiness Report

Generated: 2026-08-10

## 0. Summary

This pass wired the **intelligence and orchestration layer** on top of the
already-audited `src/swing/` risk/execution/journal engine, so
`EXECUTION_MODE=paper` can now run scan → Claude analysis → red-team →
decision → deterministic risk check → paper broker → journal, unattended,
in one command. It did **not** touch the risk authority, position-sizing
formula, or any of the 12 hard-coded numeric limits — those were already
correct and are covered by pre-existing tests.

**Status: paper-trading-capable, not yet run against real data.** All
automated tests pass (72/72 in `tests/swing/`). The system has never
executed a real Alpaca paper order — that requires the user to supply
credentials and run it. Live/Robinhood trading remains disabled and
unimplemented, as required.

---

## 1. What changed

New files (`src/swing/`):
- `universe.py` — curated ~145-symbol liquid US stock/ETF universe (11 GICS sectors + broad ETFs), replacing the old 18-symbol static list for the swing path.
- `data_feed.py` — yfinance adapter: daily bars, earnings-date lookup, disk-cached, builds the exact inputs `DeterministicSwingScanner.scan()` expects.
- `llm_backend.py` — `AnthropicStructuredBackend`, the first real implementation of `StructuredModelBackend` (via `langchain_anthropic`, schema-constrained output), with per-call cost tracking.
- `lessons_review.py` — aggregates single-trade `OBSERVATION` lessons into `SwingResearchEngine` hypotheses once a group crosses the 30-sample floor. Never validates/promotes anything.

New CLI commands (`src/swing/cli.py`): `scan`, `run`, `review-observations`. `reconcile` now wires an LLM postmortem backend through when `ANTHROPIC_API_KEY` is set.

Extended existing files (additive, backward compatible):
- `database.py` — `record_model_cost()`, `validated_lessons_for()`.
- `agents.py` — `BearAnalysis` gained 9 named red-team checklist fields (extended/chase/weak-regime/weak-sector/bad-volume/weak-support/unrealistic-target/earnings-proximity/repeats-known-pattern); the bear call is now given any `VALIDATED` lessons matching the candidate's setup+regime.
- `postmortem.py` — `close_and_review()` gained an optional `backend` parameter; when supplied it asks Claude to classify the closed trade (adds a `VALID_WIN` category) instead of only auto-labeling losses; falls back to the untouched deterministic default on any failure.
- `models.py` — added `PostmortemClassification.VALID_WIN` (additive enum member).
- `reconciliation.py` — `BrokerReconciler` accepts an optional `postmortem_backend`.
- `.env.example` — filled in recommended model defaults (Sonnet for routine analysis, Opus for the final decision/postmortem/research roles), all independently overridable.

17 new tests added (`tests/swing/test_llm_backend.py`, `test_universe_and_data_feed.py`, `test_cli_run.py`, `test_lessons_review.py`, 3 added to `test_learning.py`).

## 2. What was kept unchanged

`src/swing/risk.py`, `execution.py`, `models.py` (setup types, Decision enum, risk numbers), `brokers/*`, `stops.py`, `reporting.py`, `analytics.py`, `database.py`'s existing schema and methods, and every existing test — all untouched. Legacy `src/agents/*`, `run_hedge_fund.py`, `risk_manager.py`, `src/config.py`, `autoresearch/` (day-trading, LLM-sized positions, auto-commits its own strategy file) remain exactly as before: out-of-scope reference code, not part of the swing production path.

## 3. Current architecture

```
config/.env (EXECUTION_MODE, TRADING_ENABLED, model roles)
        |
src/swing/universe.py + data_feed.py   -> curated liquid universe, yfinance bars, earnings dates
        |
src/swing/market.py                    -> DeterministicSwingScanner + MarketRegimeEngine (unchanged, deterministic)
        |
src/swing/agents.py (AgentPipeline)    -> technical -> fundamental -> bull -> bear (red-team + lessons) -> portfolio_manager
        |         ^
        |         AnthropicStructuredBackend (llm_backend.py) -- Claude, schema-constrained, cost-tracked
        v
src/swing/risk.py (SwingRiskManager)   -> UNCHANGED deterministic final authority (12 hard rules)
        |
src/swing/execution.py + brokers/alpaca.py (AlpacaPaperProvider) -> paper order placement
        |
src/swing/reconciliation.py            -> broker truth reconciliation + automatic postmortems (now LLM-classified when configured)
        |
src/swing/postmortem.py + lessons_review.py + src/autoresearch_swing/* -> journal -> observation -> (eventually) validated lesson
        |
src/swing/reporting.py ("swing-trader analytics"/"report") -> the dashboard
```

## 4. Paper-trading workflow (day-to-day)

Matches the README's existing 3-cron pattern; only the midday slot is new:

- **Morning**: `swing-trader reconcile` (unchanged) — confirms broker/DB agree, runs postmortems on anything closed overnight.
- **Midday**: `swing-trader run` (new) — scans the universe, runs the Claude pipeline, submits any approved BUY through the unchanged risk/execution path. Prints a JSON summary of candidates found, proposals generated, and per-ticker decisions.
- **Afternoon**: `swing-trader reconcile` again, then `swing-trader report daily`.
- **Weekly**: `swing-trader review-observations` (new) — surfaces any setup/regime groups that have crossed the 30-trade statistical floor as a hypothesis for human review; never changes production behavior by itself.

The system does not ask "should I buy this?" per trade — every gate is the same deterministic risk code that was already audited (max 1 new position/day, 3/week, 3 open, 2% combined risk, 0.5-1% per trade, 5%/8% drawdown throttle, 3-loss halt). If no LLM backend is configured (`ANTHROPIC_API_KEY` unset), `run` still executes safely and produces zero proposals — it never blocks or crashes.

## 5. How Claude learns

Every closed trade already gets a structured `PostmortemRecord` (R, MFE/MAE, holding period) — unchanged. What's new: when `POSTMORTEM_MODEL`/`ANTHROPIC_API_KEY` are configured, the postmortem is classified by Claude instead of only defaulting to VALID_LOSS/UNKNOWN — it now judges process (was the thesis reasonable, were rules followed, was it normal variance) and can flag `VALID_WIN`, `BAD_ENTRY`, `BAD_STOP`, etc. Each closed trade still creates exactly one low-confidence `OBSERVATION` lesson (unchanged). `review-observations` (new) groups those observations by setup type (and by setup+regime) and, once a group reaches 30 samples, proposes a `SwingResearchEngine` hypothesis carrying the aggregated win rate/expectancy — this is evidence-only prose, never a rule change. A lesson only becomes `VALIDATED` — and only `VALIDATED` lessons are ever fed back into the bear/red-team analysis of future candidates — through the pre-existing, unchanged, human-only `approve-strategy` path requiring ≥30 samples and an accepted backtest.

## 6. How AutoResearch works today (and its gap)

`src/autoresearch_swing/` (unchanged this pass) already has real train/validation/out-of-sample + walk-forward splitting (`validation.py`), a genuinely multi-metric fitness function that penalizes low sample size, overfitting, and drawdown breaches (`fitness.py`), and a human-approval-only promotion path enforced at the database layer (`research.py` + `database.approve_candidate_strategy`) — a hypothesis can never silently reach production.

**What's still missing, deliberately deferred this pass:** an actual backtest *engine* that replays the deterministic scanner/risk logic over historical bars to produce the `FitnessMetrics` these modules consume. Today `SwingResearchEngine.record_comparison()` only validates metrics a caller already computed — `review-observations` (new) can *propose* a hypothesis from real trade history, but nothing yet runs the historical backtest that would let a human formally validate it. This is a project-sized follow-up (needs 1-2+ years of cached historical bars and a swing-shaped simulator) — building it now, before any real paper trade has happened, was judged lower priority than getting the system trading and journaling at all. The legacy `autoresearch/` day-trading engine exists and works but is unrelated (different, disabled trading mode) and — separately — auto-commits its own `strategy.py`, which is fine only because that file is disconnected from the swing production path; it is not used or touched by anything in this pass.

## 7. Risk protections (unchanged, re-confirmed by tests)

All 12 hard rules from the original spec are enforced in `src/swing/risk.py`, none weakened, all still covered by `tests/swing/test_risk.py` and `test_safety_remediation.py`: 0.5%/0.75%/1% risk tiers, entry/stop-first position sizing (`floor(equity*risk_pct/(entry-stop))`, never sized-then-stopped), max 3 positions, max 1 new position/day, max 3/week, max 2% combined open risk, 2:1 minimum R:R (1.5:1 only with explicit exceptional-RR evidence), 5%-drawdown risk reduction, 8%-drawdown full halt, 3-consecutive-loss halt, no averaging down, no stop widening, no chasing (>0.25R above planned entry), earnings-proximity exclusion, leveraged/inverse-ETF ban, and exactly the 3 allowed setup types. Claude cannot bypass any of these — it can only produce a `TradeProposal` that the unchanged risk code then approves or rejects.

## 8. Broker architecture

`BrokerProvider` abstraction (unchanged) with three implementations: `FakeBrokerProvider` (test-only, in-memory), `AlpacaPaperProvider` (the live paper broker — real Alpaca paper API, so quotes/spread/fills/market-hours are as realistic as Alpaca's own paper engine, not an in-repo simulation), and `RobinhoodMCPProvider` (intentional stub, `available=False`). This pass did not add a separate simulated matching engine — Alpaca's paper API already provides realistic execution, and building a second simulator would have duplicated that without clear benefit, consistent with "don't add unnecessary complexity."

## 9. Robinhood MCP readiness

Unchanged and still a deliberate no-op: `src/swing/brokers/robinhood_mcp.py` raises `BrokerUnavailable` on every method because no verified Robinhood Trading MCP tool schemas were available to map. Nothing in this pass invented or guessed at MCP tool names. Before Robinhood can be wired in, someone with access to the actual Robinhood Trading MCP connector needs to inspect its real tool schemas and implement `RobinhoodMCPProvider` against them — the `BrokerProvider` interface means that implementation is a self-contained, isolated change; nothing in the scanner/pipeline/risk layer needs to change to support it.

## 10. Current limitations

- **Never run against real data or a real broker.** All verification was unit/integration tests with fake brokers and stubbed LLM responses (see §11-12). The very first `swing-trader scan`/`run` against live yfinance + Alpaca paper credentials is still ahead.
- **No halt-status data feed.** `UniverseAsset.is_halted` always defaults to `False` — there is no free, reliable halt feed. Documented, not silently papered over.
- **Model pricing table is a reasonable default, not verified.** `llm_backend.py`'s `PRICE_PER_MILLION_TOKENS` must be checked against current Anthropic pricing before trusting GROSS-vs-NET numbers in `swing-trader analytics`/`report`.
- **No historical backtest engine for AutoResearch yet** (§6) — `review-observations` can propose hypotheses from live trade history, but formal out-of-sample validation of a proposed rule change isn't runnable yet.
- **No dashboard UI.** `app/` is an unrelated generic flow-builder tool. The dashboard is `swing-trader analytics` (JSON) / `swing-trader report <type>` (Markdown) — this already covers every field the spec's "simple dashboard" section asks for except volatility-bucket and relative-strength-bucket breakdowns (existing, pre-this-pass gap, left as the existing `not_collected_or_insufficient` placeholders).
- **No breadth signal or market-cap weighting** in regime classification/scanning — out of scope for this pass, flagged as a possible future refinement, not blocking.
- **Universe is a hand-curated static list**, not a dynamic screener — deliberate, matches the repo's "zero paid data APIs" stance, but means new listings/delistings require a manual edit to `universe.py`.

## 11. Tests run

**Important environment note:** this sandbox has no project Python/poetry installation at all (`poetry`, `python`, and `py` are all unavailable). I could not run `poetry run pytest` as originally planned. Instead I:
1. Syntax-checked every new/edited file with `python -m py_compile` (a separate, non-project Python 3.12 interpreter located at `C:\Program Files\LibreOffice\program\python.exe`).
2. Installed the actual runtime dependencies needed by `src/swing/*` and `src/autoresearch_swing/*` (`pydantic`, `pandas`, `numpy`, `requests`, `langchain-anthropic`, `pytest`) into an isolated target directory and ran the real `tests/swing/` suite against real code (not a simulation of running it).

This is real execution, not a claim — but it is **not** the project's own `poetry` environment, and it did **not** cover `tests/backtesting/`, `tests/test_api_rate_limiting.py`, or the legacy day-trading stack (those need the full `langgraph`/`fastapi`/`sqlalchemy` dependency tree, which was out of scope to install here, and none of those files were touched by this pass).

**You must run `poetry install && poetry run pytest` yourself** before trusting this in production — that is the authoritative check.

## 12. Test results

```
tests/swing/  ->  72 passed, 0 failed
  test_risk.py                      22 passed  (unchanged)
  test_safety_remediation.py        24 passed  (unchanged)
  test_execution.py                  4 passed  (unchanged)
  test_learning.py                   8 passed  (5 unchanged + 3 new)
  test_llm_backend.py                4 passed  (new)
  test_universe_and_data_feed.py     6 passed  (new)
  test_cli_run.py                    3 passed  (new)
  test_lessons_review.py             1 passed  (new)
```

One real bug was found and fixed during this verification: `data_feed.fetch_earnings_trading_days()` used `DataFrame.empty`, which is `True` for any zero-column frame regardless of row count — it could silently treat a valid earnings date as "unknown" depending on the exact shape yfinance returns. Fixed to check `len(index) == 0` directly.

No tests were skipped, weakened, or deleted to make this pass.

## 13. Exact command to start autonomous paper trading

```bash
poetry install
cp .env.example .env
# edit .env: set ANTHROPIC_API_KEY, ALPACA_API_KEY, ALPACA_API_SECRET
# EXECUTION_MODE=paper and TRADING_ENABLED=false are the safe defaults already in .env.example

poetry run swing-trader init-db
poetry run swing-trader status

# Dry run first (no orders, confirms the pipeline works end to end):
poetry run swing-trader run

# Once satisfied, allow real paper orders:
# set TRADING_ENABLED=true in .env, then:
poetry run swing-trader run
poetry run swing-trader reconcile
```

For unattended cron operation, schedule `swing-trader reconcile` (morning), `swing-trader run` (midday), `swing-trader reconcile` (afternoon), and `swing-trader review-observations` (weekly) — see README.md's existing "Automation with OpenClaw" section for the cron wiring pattern (only the command list changes; the scheduling mechanism is unchanged).

## 14. Exact command to stop trading immediately

```bash
poetry run swing-trader kill-switch on --approved-by "<your name>"
```

This latches the global kill switch in the database; `SwingRiskManager.validate_entry()` checks it first and rejects every new entry until explicitly cleared with `swing-trader kill-switch off --approved-by "<your name>"`. It does not touch open positions — those still carry their broker-native GTC bracket stops and are managed via `reconcile`/`tighten-stop`. Setting `TRADING_ENABLED=false` in `.env` (or simply not running `swing-trader run`) is the even simpler stop for a cron-driven setup.

## 15. What remains before Robinhood real-money testing

1. Run `swing-trader scan`/`run` against real yfinance + Alpaca paper credentials and fix whatever the first real-data run surfaces (data quirks, rate limits, timezone edge cases) that no offline test can catch.
2. Accumulate the spec's own milestones: 20 completed trades with zero major safety violations, then 50 for an expectancy check, then 100 to check whether the system is actually improving.
3. Verify the AI-cost pricing table against current Anthropic pricing so GROSS-vs-NET is trustworthy.
4. Populate `config/do_not_trade.yaml` with your actual long-term holdings so the swing system can never touch them.
5. Build the deferred AutoResearch backtest engine if you want data-driven strategy changes validated before promotion (not required to keep paper trading safely — only required before changing production rules).
6. Get real, verified Robinhood Trading MCP tool schemas and implement `RobinhoodMCPProvider` against them (not guessed).
7. Work through `docs/PAPER_TO_LIVE_CHECKLIST.md` in full (already existed, unchanged by this pass) — profit factor > 1.5, drawdown < 8%, dedicated-account identity verification, emergency procedures rehearsed, explicit human sign-off.
8. Only then: `LIVE_TRADING_ACK=I_ACKNOWLEDGE_LIVE_RISK` + `EXECUTION_MODE=live` + `TRADING_ENABLED=true`, and even then only with the small ~$2,000 first-live-phase size the spec calls for.

This report does not claim the system is ready for real money. It claims the system can now run autonomous **paper** trading end-to-end, with the same hard-coded risk rules as before, and that the code delivered in this pass passes every test written for it.
