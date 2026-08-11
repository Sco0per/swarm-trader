# Prompt 06 — Test Suite, Security, CLI/Config Cleanup, and Paper-Trading Readiness

Date: 2026-08-11  
Branch: `refactor/06-readiness`  
Audit base: `main` at `9326cda`  
Verdict: **NOT READY FOR PAPER TRADING**

## 1. Executive summary

The supported system is now one focused workflow: a long-only, whole-share, U.S.-equity swing research and Alpaca paper-execution framework under `src/swing/`, exposed by the `swing-trader` CLI. Python deterministically owns universe admission, three setup definitions, entry/stop/target geometry, risk, size, open-risk admission, broker submission, protection verification, lifecycle, reconciliation, journal, postmortem, and analytics. Structured models may critique or veto a deterministically valid candidate; they cannot set a price, stop, quantity, limit, risk percentage, or execution action.

The implementation is materially safer and its offline suite passes, but the framework is **not ready for a first autonomous paper cycle**. Production universe construction intentionally marks halt status and broader-event status unknown, so the scanner fails closed until authoritative feeds exist. The holdings/wash-sale blacklist is empty. Real Alpaca credentials, account metadata, bracket legs, partial fills, timeout payloads, restart recovery, and reconciliation have not been exercised against the actual paper service. `poetry install` also cannot complete on this host's only Python 3.14 interpreter because locked native dependencies require unsupported compilation; the supported setup remains Python 3.11–3.13.

## 2. Major changes across the whole series

1. Prompt 01 removed day/mode routing from the supported package, archived historical operating guides, quarantined AutoResearch, rewrote the README, and produced the repository audit.
2. Prompt 02 created deterministic setup validators, reason-coded funnel rejection, a versioned universe, bounded strategy parameters, stale/chase/R:R gates, and offline strategy tests.
3. Prompt 03 made risk deterministic: 0.75% baseline, immutable 1% absolute ceiling, whole-share sizing, cash and exposure caps, open/sector/cluster risk, drawdown/loss/kill latches, event and broker metadata gates, and small-account coverage.
4. Prompt 04 serialized admission, made execution idempotent, verified broker-native protection, modeled position lifecycle, reconciled broker truth after restarts/partial fills/rejections/timeouts, and separated human-only real-capital tickets from autonomous paper execution.
5. Prompt 05 added forward journal migrations, fill-lot and original-risk accounting, MAE/MFE, R-based analytics, postmortems, reports, decision/prompt/config versions, and guarded hypothesis review.
6. Prompt 06 removed the remaining root broker/command bypasses and long/short backtester, consolidated schedule/config/CLI behavior, bounded previously weakenable freshness/hold/trailing values, removed model-supplied risk overrides, added paper positions/manual close commands, rejected legacy simulator shorts/margin, removed plaintext API-key persistence, added secret redaction and history/static scans, and completed readiness coverage.

## 3. Removed legacy components

The following unsupported command and broker paths were deleted after dependency/call-site analysis: `check_moves.py`, `check_portfolio.py`, `execute_trades.py`, `gather_data.py`, `intel_exchange.py`, `performance_tracker.py`, `performance_tracker_v2.py`, `portfolio_monitor.py`, `rebalance.py`, `risk_manager.py`, `run_analysis.py`, `run_hedge_fund.py`, `scan_market.py`, `test_data.py`, `trade_alerts.py`, `trade_journal.py`, `src/alpaca_integration.py`, `src/backtester.py`, `src/cli/`, and all of `src/backtesting/`.

The `backtester` console entry was removed. The legacy web application's plaintext API-key model, routes, repository, service, frontend settings panel, and client were removed. Migration `e7c3a42d1190` drops an existing `api_keys` table and its plaintext rows; downgrade deliberately does not recreate insecure storage. The legacy research simulator remains non-brokered but now rejects nonzero margin, negative holdings, and `short`/`cover` actions at its boundary.

Historical `DESIGN.md`, `INTEL_EXCHANGE.md`, `MILESTONES.md`, and `PLAYBOOK.md` were moved to `docs/archive/` with non-operational banners. Their content is provenance, not supported instruction.

## 4. Final swing strategy

All gates are implemented in `src/swing/strategy.py` and composed by `src/swing/market.py`. A candidate is emitted only if every mandatory Boolean gate passes; score cannot rescue a failed gate.

### Trend pullback

Requires ordered/rising medium and long trends; positive stock-versus-SPY strength; price above the medium average; a pullback within the configured ATR distance of short/medium support; no low through structural support; bounded ATR/price; sufficient history and liquidity; bullish continuation confirmation; known-clear event state; and provisional and final reward/risk of at least 2.0R. Pullback volume contraction is retained as preference evidence, not an unsafe substitute for a mandatory gate. Stop is deterministic structural invalidation and target is the bounded trend extension.

### Breakout retest

Requires a 10–40 session base, bounded base range, buffered resistance breakout, required breakout volume expansion, a controlled retest that holds former resistance within tolerance, bullish confirmation, positive trend, no collapsed retest, known-clear events, acceptable volatility/liquidity, at least 2.0R, and no entry beyond the anti-chase ATR-extension ceiling. Stop is below validated retest/base structure; target uses a deterministic measured move.

### Relative-strength continuation

Requires strong stock-versus-SPY relative strength; nonnegative stock-versus-sector and sector-versus-SPY strength; aligned short/medium/long trends; a bounded consolidation; pre-trigger volume contraction; price and volume confirmation; non-hostile regime; known-clear events; acceptable volatility/liquidity; and at least 2.0R. Missing sector ETF bars reject. Stop is consolidation/structural invalidation and target is a deterministic extension.

Tests `test_strategy.py`, `test_universe_and_data_feed.py`, and `test_safety_remediation.py` cover valid/invalid pullbacks, breakout/retest and failed breakouts, strong/weak RS continuation, insufficient R:R, stale setup data, chasing, missing sector data, and reason-coded funnel behavior.

## 5. Final risk model

- Applied trade risk starts at 0.75% of fresh account equity. Regime multipliers may only reduce it. No LLM or proposal field can override it. The absolute code ceiling is exactly 1.00%.
- Structural per-share risk is `fresh_entry_quote - authoritative_structural_stop`, with stop distance bounded by configured ATR sanity limits. Quantity begins as `floor(equity × applied_risk_pct / per_share_risk)`.
- Final quantity is the minimum whole-share integer allowed by risk size, cash (never margin buying power), 35% position exposure, 2% combined open risk, 1.5% sector risk, 1% cluster risk, and 0.1% ADV. Quantity below one rejects.
- Open risk uses each active trade's planned/remaining original dollar risk plus the proposed trade. Partial exits retain the immutable original denominator. A maximum of three positions, one new entry/day, and three/week applies.
- Drawdown controls reduce risk at 5%, halt at 8%, halt at three consecutive losses, and halt at 4% weekly drawdown. The global kill switch, reconciliation halt, and protection failure halt latch until explicit human review.
- Event gates require earnings more than five trading days away, known-clear broader events, known non-halted/restricted asset state, fresh broker/quote/clock/event data, and no holdings/wash-sale blacklist match.
- Initial reward/risk must be at least 2.0R. Entry slippage may not exceed 0.25R. Spreads may not exceed 0.50%.
- Stops never widen. Default structural stop sanity is 0.75–3.00 ATR; policy is structural-only or structure-then-trail. Default profit review is 1R, trailing activation 2R, distance 0.75R. Default expected/max hold is four/ten trading days; time-stop action is named (`REVIEW` or `EXIT`).

`src/swing/risk.py` is the admission engine. Entry placement and deterministic safety closes live in `src/swing/execution.py`; stop replacement is the only separate mutation boundary and `src/swing/stops.py` calls `SwingRiskManager.validate_stop_change` before the broker. Every supported broker mutation call site was traced:

| Mutation | Call site | Required boundary |
|---|---|---|
| New bracket | `SwingExecutionService.submit` | full strategy/risk validation, serialized reservation, reconciliation, paper provider |
| Manual/protection close | `SwingExecutionService.request_manual_close`, protection remediation | lifecycle + execution boundary; risk-reducing only |
| Reconciler emergency close | `BrokerReconciler` → `request_safety_close` | central execution boundary; halt first; risk-reducing only |
| Stop replacement | `ProtectedStopService.tighten` | deterministic risk-engine approval; tightening only |

No other source file contains an Alpaca mutation endpoint or provider call.

## 6. LLM architecture

The four structured roles are technical contradiction review, fundamental/event review, bear-case red team, and portfolio-manager advisory review. Payloads are allowlisted candidate identity, setup, deterministic indicators/features, event status/digests, score route, and analyst findings. They exclude credentials, account identifiers, broker order IDs, cash, buying power, portfolio size, and unrelated database rows.

Schemas use `extra="forbid"`. No schema contains an entry, stop, target, quantity, risk percentage, position size, or order-limit field. The final `TradeProposal` copies Python-owned candidate entry/stop/target only after ticker/setup identity checks and an `APPROVE`; Python re-runs unchanged strategy and risk validation afterward. Malformed output, timeout, unavailable/unconfigured model, invalid setup, unknown/mismatched ticker, `REJECT`, or `WATCH` yields no proposal (`NO_TRADE`). Model output cannot call a broker.

Responses, prompts, validation errors, and cached decisions are versioned and redacted before persistence. LLM failures are observable but fail closed.

## 7. Data and universe

The dated 439-symbol snapshot `config/universe/us_liquid_2026-08-11.csv` is the reproducible starting universe. Construction applies the required blacklist, existing-holding exclusion, allowed common-stock/security type, explicit unleveraged ETF allowlist, price/history/share-volume/dollar-volume/spread filters, broker tradability/restriction metadata, event data, halt data, and freshness. Options, crypto, leveraged/inverse/volatility-linked ETFs, unknown assets, and custom ETFs outside the allowlist reject.

Alpaca supplies daily bars, quotes, and broker metadata. Earnings lookup is bounded, cached, and fails closed. Production metadata deliberately sets real-time halt status unknown because Alpaca asset metadata is not an authoritative halt feed. Broader corporate-event status is likewise unknown pending deterministic calendars. Those two controls currently prevent an autonomous production scan from producing candidates; this is a safety property and a readiness blocker, not a reason to add an optimistic fallback.

## 8. Trade lifecycle

```text
versioned scan
  → deterministic candidate and validator evidence
  → structured LLM critique/veto
  → unchanged deterministic strategy validation
  → fresh broker/account/quote/event snapshot
  → deterministic risk admission and serialized intent reservation
  → whole-share GTC Alpaca paper limit bracket
  → broker-native stop/target verification
  → OPEN → PROTECTED → PROFITABLE → TRAILING → EXIT_PENDING → CLOSED
  → repeated broker/database reconciliation
  → immutable thesis + fills + exit + R/MAE/MFE journal
  → structured postmortem and guarded research observation
```

Client-order identity, durable admissions, unique database constraints, lifecycle transitions, and reconciliation make retries/restarts idempotent. Unknown broker outcomes, missing protection, untracked positions/orders, rejected or partial orders, quantity mismatches, and API timeouts halt new entries. Real capital remains different: `live-ticket` uses the same risk engine against a fresh human-provided snapshot and only prints; a human separately executes. `record-live-fill`/`record-live-exit` journal evidence. No repository code calls a live order-placement endpoint and no schedule attaches a live connector.

## 9. Research architecture

`autoresearch/` is quarantined, intraday-derived, self-modifying research that is explicitly not swing-validated. No production module imports it and no result/config bridge exists. `src/autoresearch_swing/` records hypotheses and evaluates bounded parameter candidates, but promotion requires a minimum sample and explicit human approval. Production settings are never rewritten automatically. Human review remains mandatory.

## 10. Tests and validation

### Final runs

| Command | Result |
|---|---|
| `poetry install` | **Failed on host prerequisite**: only Python 3.14 is available; locked `numpy 1.26.4`/`lxml 5.4.0` require native compilation and MSVC. Existing Poetry environment remained usable. |
| `poetry run pytest -q` | **277 passed, 0 failed, 17 warnings** in 53.95s; remaining warnings are Python-3.14/third-party and NumPy test-fixture deprecations; all boundaries mocked, no credentials/network. |
| focused readiness/lifecycle/boundary run | **95 passed, 0 failed, 9 third-party Python-3.14 warnings**. |
| `black` + `isort` + `flake8` on `src/swing tests/swing` | **Pass** after formatting the supported tree and aligning Flake8's line length with Black. |
| changed-file Black/isort/Flake8 | **Pass**. |
| `npx --no-install pyright` | **0 errors, 0 warnings**. |
| `python -m compileall -q src app tests` | **Pass**. |
| `poetry run pip check` | **Failed consistently with the incomplete Python-3.14 environment**: missing `jiter`/`lxml` and installed NumPy 2.5.2 does not satisfy the lock. This is the same fresh-install blocker, not hidden as a pass. |
| custom AST static-security scan | **0 remaining** `eval`/`exec`/`os.system`/unsafe pickle/YAML/TLS-disable/`shell=True` findings after remediation. |
| `poetry check` | **Pass**, with Poetry migration/deprecation notices. |
| full-repository Black/Flake8 | Legacy baseline remains: 67 untouched research/app files need Black and 895 Flake8 findings existed before this prompt; no prompt-06 changed file fails. |

No Bandit, Gitleaks, or pip-audit executable is installed, so the report does not claim those tools ran. The replacement checks were an all-history credential-pattern scan, current-tree sensitive-assignment scan, AST unsafe-call scan, dependency `pip check` (which exposed the incomplete host environment), compiler pass, and behavior tests.

### Added/replaced coverage

Prompt 06 added or expanded 27 collected cases covering canonical CLI help and read-only positions; invalid types and hard safety-bound weakening; secret/margin legacy request rejection; no database key table and persistence redaction; legacy margin/short behavior; option/crypto/custom ETF/fractional rejection; AutoResearch isolation; gitignore classes; manual-close idempotency/execution boundary; and root/broker bypass absence. Existing swing suites already cover every strategy, risk, event, execution, database, LLM, journal, postmortem, and analytics item in the prompt.

Thirty-seven tests were removed with the unsupported long/short backtester:

| Removed test file | Count | Swing-only replacement evidence |
|---|---:|---|
| `integration/test_integration_long_only.py` | 4 | scanner→risk→execution/reconciliation lifecycle in `test_execution.py`, `test_execution_lifecycle.py`, `test_cli_run.py` |
| `integration/test_integration_long_short.py` | 4 | long-only lifecycle plus behavioral short impossibility in `test_readiness.py`/`test_agent_boundary.py` |
| `integration/test_integration_short_only.py` | 5 | explicit short/negative-position rejection; no broker short schema/path |
| `test_controller.py` | 1 | structured agent pipeline/caching/NO_TRADE tests in `test_llm_backend.py` and `test_agent_boundary.py` |
| `test_execution.py` | 2 | paper bracket, duplicate, rejection, timeout, and restart tests in swing execution suites |
| `test_metrics.py` | 3 | fixed-R, expectancy, drawdown, MAE/MFE tests in `test_journal_analytics.py` |
| `test_portfolio.py` | 13 | cash/open-risk/position/sector/cluster/whole-share/averaging-down tests in `test_risk_engine.py` |
| `test_results.py` | 1 | durable daily/weekly report tests in `test_journal_analytics.py` |
| `test_valuation.py` | 4 | equity/open-risk/R analytics and exposure tests in `test_risk_engine.py`/`test_journal_analytics.py` |

### Evidence checklist

- [x] only long equity swing trading exists — `Decision`, `SetupType`, `OrderIntent`; `test_agent_boundary.py`
- [x] day mode removed — strict `TRADING_STYLE=swing`; `test_environment_cannot_weaken_named_safety_bound`
- [x] shorts impossible — legacy behavior rejection plus broker/order schema tests
- [x] leverage impossible — leveraged/inverse/volatility ETF tests and cash-only sizing
- [x] margin strategy impossible — paper risk/account tests and legacy request/portfolio rejection
- [x] options impossible — `test_non_equity_asset_classes_are_behaviorally_rejected[option]`
- [x] crypto impossible — `test_non_equity_asset_classes_are_behaviorally_rejected[crypto]`
- [x] leveraged ETFs rejected — universe/risk-engine instrument tests
- [x] fractional shares impossible — `OrderIntent.quantity: int`; fractional schema test
- [x] averaging down rejected — existing-holding and duplicate-position risk tests
- [x] strategy validator deterministic — `test_strategy.py`
- [x] risk sizing deterministic — 0.75% and small-account tests
- [x] open risk enforced — combined-open-risk tests
- [x] cluster risk enforced — sector/cluster tests
- [x] earnings gate enforced — unavailable/too-close/stale tests
- [x] stale data fails closed — quote/asset/clock/event freshness tests
- [x] halted symbols rejected — halt/restriction scanner tests
- [x] liquidity rules enforced — price/volume/dollar-volume/spread/ADV tests
- [x] R:R enforced — insufficient/final R:R tests
- [x] broker reconciliation works — restart/partial/rejection/mismatch/timeout tests
- [x] duplicate orders prevented — serialized admission/idempotency tests
- [x] stop protection tested — bracket evidence and protection-remediation tests
- [x] LLM failure returns NO_TRADE — malformed/timeout/unavailable tests
- [x] LLM cannot set stop or size — extra-forbid schemas and attempted-override tests
- [x] database durable — reopen/persistence/hosted-routine tests
- [x] migrations tested — forward migration and populated-database tests
- [x] postmortems work — `test_journal_analytics.py`
- [x] R analytics work — fixed-R/expectancy tests
- [x] MAE/MFE work — completed-bar/missing-data tests
- [x] decision versioning works — version/fingerprint tests
- [x] $2,000 account size supported — small-account sizing and sizing-to-zero tests
- [x] sizing-to-zero rate reported — funnel/analytics tests
- [x] AutoResearch isolated — import/bridge/documentation tests
- [x] no secrets leak — redaction/prompt/report/database/history tests and scans
- [x] README contains only supported instructions — README audit and CLI-help test
- [x] tests pass — 277/277 full-suite result

## Configuration reference

`src/swing/config.py` is the single non-secret settings model. `.env.example` documents all 115 central parameters. The five secret/database boundary variables are deliberately read only where needed so they cannot appear in settings snapshots. The grouped rows below still enumerate every parameter, default, unit, bound, and enforcement site.

| Parameter(s) | Default(s) | Startup/safety bound | Unit | Enforced at |
|---|---|---|---|---|
| `TRADING_STYLE`; `EXECUTION_MODE`; `TRADING_ENABLED`; `LIVE_TRADING_ACK` | `swing`; `paper`; `false`; blank | exactly `swing`; `paper|live`; strict Boolean; live+enabled requires exact acknowledgement | enum/Boolean/text | `SwingSettings.__post_init__`, CLI/provider gates |
| `STRATEGY_VERSION`; `CONFIG_VERSION`; `SCANNER_VERSION` | `SWING_V1.0`; `SWING_CONFIG_V1`; `deterministic-swing-scanner-v2` | non-secret version labels; persisted on decisions/trades | identifier | decision versioning/database |
| `SWING_DATABASE_PATH`; `DO_NOT_TRADE_PATH`; `SWING_UNIVERSE_PATH` | `data/adaptive_swing.db`; `config/do_not_trade.yaml`; dated CSV | blacklist must load and contain `symbols`; universe must load/version; DB migrations fail loudly | path | config/universe/database |
| `NORMAL_RISK_PCT`; `ABSOLUTE_MAX_RISK_PCT`; `REDUCED_RISK_PCT` | `0.0075`; `0.01`; `0.003` | `(0,0.0075]`; exactly `0.01`; `[0.0025,0.0035]` | equity fraction | settings + risk |
| `REDUCE_RISK_DRAWDOWN_PCT`; `HALT_DRAWDOWN_PCT`; `WEEKLY_DRAWDOWN_HALT_PCT` | `0.05`; `0.08`; `0.04` | positive, ordered, no weaker than 5%/8%; weekly `(0,0.05]` | equity fraction | settings + risk/latches |
| `MAX_COMBINED_OPEN_RISK_PCT`; `MAX_SECTOR_OPEN_RISK_PCT`; `MAX_CLUSTER_OPEN_RISK_PCT` | `0.02`; `0.015`; `0.01` | positive ceilings `2%`; `1.5%`; `1%` | equity fraction | settings + risk |
| `MAX_POSITION_EXPOSURE_PCT`; `MAX_OPEN_POSITIONS`; `MAX_NEW_POSITIONS_DAY`; `MAX_NEW_POSITIONS_WEEK` | `0.35`; `3`; `1`; `3` | `(0,35%]`; `1..3`; exactly max 1/day; `1..3`/week | fraction/count | settings + risk/database |
| `CONSECUTIVE_LOSS_HALT` | `3` | `1..3` | losses | settings + postmortem/risk |
| `MINIMUM_STOP_ATR`; `MAXIMUM_STOP_ATR`; `MAXIMUM_ENTRY_SLIPPAGE_R` | `0.75`; `3.0`; `0.25` | min `[0.5,1.5]`, max `[1.5,4]`, strictly ordered; slippage `[0,0.25]` | ATR/R | settings + strategy/risk |
| `MAXIMUM_ADV_FRACTION`; `BROKER_MIN_QUANTITY` | `0.001`; `1` | `(0,0.01]`; exactly one whole share | ADV fraction/shares | settings + risk/order schema |
| `MINIMUM_RR` | `2.0` | `>=2.0` | R multiple | settings + strategy/risk |
| `BUY_SCORE_THRESHOLD`; `PREFERRED_SCORE_THRESHOLD`; `A_PLUS_SCORE_THRESHOLD`; `BORDERLINE_SCORE_THRESHOLD` | `80`; `85`; `90`; `70` | buy `>=80`; ordered through `<=100`; borderline `[0,buy)` | points | settings + scanner/agents |
| `MINIMUM_PRICE`; `MINIMUM_AVERAGE_VOLUME`; `MINIMUM_AVERAGE_DOLLAR_VOLUME`; `MINIMUM_HISTORY_SESSIONS`; `MAXIMUM_SPREAD_PCT` | `5`; `1,000,000`; `20,000,000`; `160`; `0.005` | floors `$5`, `1m`, `$20m`; dollar volume max `$5b`; history `120..200` and covers long MA; spread `(0,0.5%]` | USD/shares/USD/sessions/fraction | settings + universe/scanner |
| `NEUTRAL_SCORE_THRESHOLD_ADDITION`; `CHOPPY_SCORE_THRESHOLD_ADDITION`; `HOSTILE_SCORE_THRESHOLD_ADDITION` | `5`; `10`; `20` | nonnegative and ordered neutral≤choppy≤hostile | points | settings + scanner |
| `NEUTRAL_RISK_MULTIPLIER`; `CHOPPY_RISK_MULTIPLIER`; `HOSTILE_RISK_MULTIPLIER` | `0.75`; `0.50`; `0.25` | `0 < hostile ≤ choppy ≤ neutral ≤ 1`; reduction only | multiplier | settings + risk |
| `EARNINGS_EXCLUSION_TRADING_DAYS`; `COOLDOWN_TRADING_DAYS`; `MINIMUM_STATISTICAL_SAMPLE` | `5`; `10`; `30` | floors `5`; `10`; `30` | trading days/trades | settings + event/research/analytics |
| `QUOTE_FRESHNESS_SECONDS`; `BROKER_ASSET_FRESHNESS_SECONDS`; `MARKET_CLOCK_FRESHNESS_SECONDS` | `300`; `60`; `60` | `1..300`; `1..60`; `1..60` | seconds | settings + risk |
| `CORPORATE_EVENT_FRESHNESS_SECONDS`; `EARNINGS_FRESHNESS_SECONDS` | `86400`; `86400` | each `1..86400` | seconds | settings + risk |
| `EXPECTED_HOLD_DAYS`; `MAXIMUM_HOLD_DAYS`; `TIME_STOP_MINIMUM_PROGRESS_R`; `TIME_STOP_MODE` | `4`; `10`; `0.5`; `REVIEW` | expected `1..4`; max `[expected,10]`; progress `[0.5,2]`; `REVIEW|EXIT` | trading days/R/enum | settings + lifecycle |
| `STOP_MANAGEMENT_POLICY`; `PROFITABLE_TRIGGER_R`; `TRAILING_ACTIVATION_R`; `TRAILING_DISTANCE_R`; `MOVE_TO_BREAKEVEN_AT_R` | `STRUCTURE_THEN_TRAIL`; `1`; `2`; `0.75`; blank | named policy only; profit `(0,1]`; activation `[profit,2]`; distance `(0,0.75]`; optional breakeven `(0,2]` | enum/R | settings + lifecycle/stops |
| `TRAILING_ELIGIBLE_SETUPS` | pullback, RS continuation | subset of the three supported setup enums | enum list | settings + lifecycle |
| `LLM_MATERIAL_SCORE_CHANGE`; `MAXIMUM_SCANNER_CANDIDATES`; `MAXIMUM_PM_CANDIDATES` | `5`; `20`; `5` | score change `1..20`; candidates `1..20`; PM `1..5` | points/count | settings + agents |
| `SCANNER_MODEL`; `ANALYST_MODEL`; `PORTFOLIO_MANAGER_MODEL`; `POSTMORTEM_MODEL`; `RESEARCH_MODEL`; `MODEL_FALLBACK` | all blank in code (`.env.example` recommends Sonnet/Opus role routes) | string role routes; missing required route/model produces `NO_TRADE`, never fallback approval | model identifier | `ModelSettings`, agents/backend |
| `SCHEDULE_TIMEZONE` | `America/New_York` | exact named zone | IANA zone | `ScheduleSettings` |
| `SCHEDULE_INITIAL_SCAN_ET`; `SCHEDULE_DECISION_CYCLE_ET`; `SCHEDULE_MIDDAY_REFRESH_ET`; `SCHEDULE_AFTERNOON_REFRESH_ET`; `SCHEDULE_POSITION_HEALTH_ET`; `SCHEDULE_DAILY_REPORT_ET` | `09:35`; `10:15`; `12:30`; `14:30`; `15:45`; `16:15` | strict 24-hour `HH:MM` | ET wall time | `ScheduleSettings`, scheduling |
| `SCHEDULE_WEEKLY_LESSONS_ET` | `Sunday 18:00` | strict `<weekday> HH:MM` | ET weekday/time | `ScheduleSettings`, scheduling |
| `STRATEGY_SHORT_EMA_PERIOD`; `STRATEGY_ATR_PERIOD`; `STRATEGY_MEDIUM_MA_PERIOD`; `STRATEGY_LONG_MA_PERIOD` | `20`; `14`; `50`; `150` | `10..30`; `10..30`; `40..75`; `100..200`; EMA<medium<long | sessions | `StrategySettings` + strategy |
| `STRATEGY_TREND_SLOPE_LOOKBACK`; `STRATEGY_MINIMUM_TREND_SLOPE` | `10`; `0` | `5..30`; `0..0.05` | sessions/fraction | `StrategySettings` + strategy |
| `STRATEGY_RS_LOOKBACK`; `STRATEGY_MINIMUM_SPY_RS`; `STRATEGY_STRONG_SPY_RS`; `STRATEGY_MINIMUM_SECTOR_RS` | `63`; `0`; `0.03`; `0` | `40..126`; `0..0.20`; `0.01..0.30` and ≥ minimum; `0..0.20` | sessions/return fraction | `StrategySettings` + strategy |
| `STRATEGY_PULLBACK_MAX_ATR_DISTANCE`; `STRATEGY_STRUCTURAL_BREAK_ATR`; `STRATEGY_MINIMUM_ATR_PCT`; `STRATEGY_MAXIMUM_ATR_PCT` | `1.25`; `0.5`; `0.005`; `0.08` | `0.25..2`; `0..2`; `0..0.03`; `0.03..0.15`; ATR pct ordered | ATR/fraction | `StrategySettings` + strategy |
| `STRATEGY_CONSOLIDATION_SESSIONS`; `STRATEGY_CONSOLIDATION_MAX_RANGE_PCT` | `20`; `0.12` | `10..40`; `0.03..0.25` | sessions/fraction | `StrategySettings` + strategy |
| `STRATEGY_BREAKOUT_BUFFER_PCT`; `STRATEGY_BREAKOUT_VOLUME_RATIO`; `STRATEGY_RETEST_HOLD_TOLERANCE_PCT`; `STRATEGY_RETEST_COLLAPSE_TOLERANCE_PCT`; `STRATEGY_MAXIMUM_BREAKOUT_EXTENSION_ATR` | `0.003`; `1.20`; `0.01`; `0.03`; `1.25` | `0..0.03`; `1..3`; `0..0.05`; `0..0.10`; `0.25..3` | fraction/ratio/ATR | `StrategySettings` + strategy |
| `STRATEGY_RS_CONSOLIDATION_SESSIONS`; `STRATEGY_RS_CONSOLIDATION_MAX_ATR`; `STRATEGY_CONTRACTION_VOLUME_RATIO`; `STRATEGY_CONFIRMATION_VOLUME_RATIO` | `10`; `4`; `0.90`; `1.10` | `5..30`; `1.5..8`; `0.5..1.1`; `0.8..3` | sessions/ATR/ratio | `StrategySettings` + strategy |
| `STRATEGY_TREND_TARGET_ATR_EXTENSION`; `STRATEGY_MEASURED_MOVE_MULTIPLE`; `STRATEGY_MAXIMUM_BAR_AGE_DAYS` | `3`; `2.5`; `7` | `0.5..5`; `0.5..3`; `1..10` | ATR/multiple/calendar days | `StrategySettings` + strategy |
| `SCORE_WEIGHT_SETUP_QUALITY`; `SCORE_WEIGHT_RELATIVE_STRENGTH`; `SCORE_WEIGHT_TREND_QUALITY`; `SCORE_WEIGHT_VOLUME_CONFIRMATION`; `SCORE_WEIGHT_MARKET_REGIME`; `SCORE_WEIGHT_SECTOR_STRENGTH`; `SCORE_WEIGHT_RISK_REWARD` | `30`; `20`; `15`; `10`; `10`; `5`; `10` | each `0..50`; total exactly `100` | points | `EntryScoreWeights` + scanner |
| `ALPACA_API_KEY`; `ALPACA_API_SECRET` | blank | boundary-only; absence prevents broker use; never snapshot/persist | secret text | data feed/Alpaca provider |
| `ANTHROPIC_API_KEY` | blank | boundary-only; absence means deterministic-only/`NO_TRADE` where model required | secret text | CLI/backend |
| `TURSO_DATABASE_URL`; `TURSO_AUTH_TOKEN` | blank | boundary-only; both required for hosted mode, otherwise local SQLite | URL/secret text | database adapter |

Every numeric parser is strict. Invalid Boolean/integer/float text and tested weakening attempts fail startup naming the environment parameter.

## Security audit

- API/broker/database keys are process-environment boundary values and are not placed in settings snapshots or LLM payloads. The supported package does not auto-load or create `.env`.
- `redact_sensitive` recursively removes sensitive-key values and token-shaped strings before JSON/text persistence. Database agent decisions, trade/execution events, structured logs, notifications, reports, CLI errors, and legacy flow data use redaction.
- The legacy plaintext `api_keys` table/API/UI was removed. Existing users of that old web feature must run its destructive cleanup migration and rotate every formerly stored key; inaccessible old rows are still sensitive until deleted.
- The CLI catches top-level exceptions, prints a redacted one-line JSON error to stderr, and does not expose a traceback by default.
- Shell invocation findings in legacy display/Ollama helpers were removed (`os.system` and `shell=True`).
- `.gitignore` covers `.env*` except `.env.example`, credentials/secrets/service-account JSON, PEM/key/P12/PFX material, databases, logs, caches/runtime data, and crash dumps.
- A 36-commit all-history scan found no private keys, AWS access IDs, GitHub tokens, Slack tokens, credential files, or assigned hardcoded secrets. The only `sk-...` shape matches were five substrings inside fixture news URL/image URL slugs and one deliberate fake token in a redaction test. No real secret was identified and history was not rewritten.
- Cloud-agent documentation now requires managed environment secret review, masking, access control, and rotation; it does not describe environment variables as a vault.
- LLM prompt construction is an explicit candidate-data allowlist and contains no credentials, account/broker identifiers, portfolio cash, or unrelated records.

## 11. Remaining risks and TODOs

1. **Authoritative real-time halt feed missing.** Production assets carry `halt_status_known=False`, so the scanner rejects them. Integrate and verify a deterministic feed; do not change the fallback.
2. **Broader corporate-event feed missing.** FDA/M&A/index-rebalance/investor-day state is unknown in production and rejects. Integrate, freshness-bound, and test it.
3. **Holdings blacklist empty.** Populate `config/do_not_trade.yaml` from actual holdings and wash-sale-sensitive equivalents; review it manually.
4. **Credentialed Alpaca paper QA outstanding.** Verify read-only account/asset/clock/position/order payloads, then manually exercise bracket legs, partial fills, rejection, timeout ambiguity, restart, stop/target, close, and reconciliation in paper.
5. **Fresh install not reproducible on this host.** Use Python 3.11–3.13; `poetry install` on the available Python 3.14 needs unavailable native compilation for locked dependencies.
6. **Existing legacy web DB may contain old plaintext keys.** Run `poetry run alembic -c app/backend/alembic.ini upgrade head` from a reviewed backup and rotate/delete all old credentials. The migration intentionally destroys those rows.
7. **Hosted Turso behavior not externally verified in this prompt.** Unit tests mock concurrency/idempotency, but real service connectivity, auth, latency, and failure behavior require staging QA.
8. **Statistical expectancy is unknown.** Analytics correctly withholds conclusions below 30 closed trades overall/per segment. Do not increase frequency or weaken filters to collect samples faster.
9. **Legacy non-production research/app formatting debt remains.** Supported swing and changed files pass tools; the full untouched tree still contains pre-existing Black/Flake8 debt.
10. Fail-closed TODO comments for halt data, broader events, and complete sector data remain deliberately visible. None provides a permissive stub.

## 12. Paper trading readiness

# NOT READY FOR PAPER TRADING

The offline implementation and tests are coherent, but a first autonomous paper cycle would currently either reject every production candidate or depend on unverified external state. Readiness requires: (1) authoritative fresh halt and broader-event feeds, (2) a populated reviewed holdings/wash-sale blacklist, (3) a successful Python 3.11–3.13 install and fresh DB initialization, and (4) credentialed read-only plus controlled Alpaca paper lifecycle/reconciliation QA. This is intentionally not rounded up.

## 13. Changed files across prompts 01–06

Relative to `main` (`9326cda`): **145 files changed, 11,271 insertions, 11,887 deletions**, including this report and final supported-tree formatting.

### Added

`.flake8`; `config/universe/us_liquid_2026-08-11.csv`; `autoresearch/README.md`; `docs/refactor/01_AUDIT_REPORT.md` through `06_READINESS_REPORT.md`; `src/swing/{decision_versioning,lifecycle,measurement,notifications,protection,scheduling,security,strategy}.py`; `tests/swing/{test_agent_boundary,test_execution_lifecycle,test_journal_analytics,test_readiness,test_risk_engine,test_strategy}.py`; `app/backend/alembic/versions/e7c3a42d1190_remove_api_key_storage.py`.

### Renamed/archived

`DESIGN.md` → `docs/archive/DESIGN.md`; `INTEL_EXCHANGE.md` → `docs/archive/INTEL_EXCHANGE.md`; `MILESTONES.md` → `docs/archive/MILESTONES.md`; `PLAYBOOK.md` → `docs/archive/PLAYBOOK.md`.

### Removed

All root legacy commands listed in section 3; `trading_mode.json`; `src/alpaca_integration.py`; `src/backtester.py`; all `src/backtesting/**`; all `src/cli/**`; all `tests/backtesting/**`; and the legacy API-key backend/frontend files listed in section 3.

### Modified

`.env.example`, `.gitignore`, `README.md`, `pyproject.toml`; `docs/{ADAPTIVE_SWING_ARCHITECTURE,BROKER_PROVIDERS,CRON_AGENTS,RISK_MODEL,ROBINHOOD_MCP}.md`; `autoresearch/{BRIDGE-DESIGN,CRON-ARCHITECTURE,analyze,backtest_fast,evolve,program,strategy,strategy_backup}.md/.py`; `app/backend/database/{connection,models}.py`; `app/backend/models/schemas.py`; `app/backend/repositories/{flow_repository,flow_run_repository}.py`; `app/backend/routes/{__init__,hedge_fund}.py`; `app/backend/services/{backtest_service,portfolio}.py`; `app/frontend/src/components/settings/{index,settings}.tsx`; `src/accounts.py`; `src/agents/autoresearch_agent.py`; `src/main.py`; `src/swing/{__init__,agents,analytics,cli,config,data_feed,database,execution,lessons_review,llm_backend,market,models,postmortem,reconciliation,reporting,risk,universe}.py`; `src/swing/brokers/{alpaca,base,fake,human_supervised,robinhood_mcp}.py`; `src/utils/{api_key,display,llm,ollama}.py`; and the existing swing test support/execution/CLI/learning/lessons/LLM/risk/universe suites.

## 14. Exact next commands

Use Python 3.11–3.13 and do not enable submission until sections 13.1–13.4 are complete.

```bash
poetry env use 3.13
poetry install
poetry run swing-trader init-db
poetry run pytest
poetry run black --check src/swing tests/swing
poetry run isort --check-only src/swing tests/swing
poetry run flake8 src/swing tests/swing
npx --no-install pyright
poetry run swing-trader status
poetry run swing-trader scan
poetry run swing-trader run
poetry run swing-trader reconcile
poetry run swing-trader analytics
poetry run swing-trader report daily
```

`scan` is read-only but uses external data. `run` is the one paper cycle and may submit only with `EXECUTION_MODE=paper`, `TRADING_ENABLED=true`, valid paper credentials, a clean reconciliation, and every deterministic gate passing. Do not run it merely to test wiring; complete controlled credentialed QA first.
