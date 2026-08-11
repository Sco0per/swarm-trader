# 03 — Deterministic Stops, Position Sizing, Open Risk, and Cluster Limits

Branch: `refactor/03-risk-engine`  
Date: 2026-08-11

## Outcome

The swing path now has one deterministic Python authority for structural stops, whole-share quantity, open-risk accounting, sector/cluster exposure, event gates, and durable halts. Analyst and portfolio-manager schemas no longer expose entry, stop, target, quantity, or limit fields. A `TradeProposal` still carries compatibility fields for internal/live-ticket transport, but risk validation ignores their stop/target values: execution uses `RiskDecision.authoritative_stop` and the deterministic candidate target.

No broker order, amendment, cancellation, paper submission command, live submission command, or credentialed network request was executed. No dependency was added.

## Final risk model

| Control | Configured value | Immutable startup bound | Enforcement |
|---|---:|---:|---|
| Baseline risk per trade | 0.75% equity | `NORMAL_RISK_PCT <= 0.75%`; absolute applied risk `<= 1.00%` | `SwingSettings.__post_init__`; `SwingRiskManager.validate_entry` |
| A+ compatibility setting | 0.75% | `<= 0.75%` | Retained for config compatibility; no longer grants an increase |
| Reduced risk | 0.30% | 0.25%–0.35% | Applied once portfolio drawdown reaches 5%, unless a halt already binds |
| Regime risk | Bull 1.00×, neutral 0.75×, choppy 0.50×, hostile 0.25× | Multipliers must be ordered and `<= 1` | May only reduce baseline risk; bear/risk-off remain no-trade |
| Total concurrent open risk | 2.00% equity | `<= 2.00%` | Recomputed from durable trades/admissions on every review and rechecked under serialized admission |
| Sector open risk | 1.50% equity | `<= 1.50%` | Downward quantity cap and serialized admission recheck |
| Cluster open risk | 1.00% equity | `<= 1.00%` | Downward quantity cap and serialized admission recheck |
| Individual position value | 35% equity | `<= 35%` | Ceiling only; never increases risk-based quantity |
| Open positions | 3 | 1–3 | Secondary ceiling; 2% open risk is intentionally expected to bind first |
| New positions/day | 1 | `<= 1` | Existing stricter control retained; resolved maximum of 2 was not used to weaken it |
| New positions/week | 3 | `<= 3` | Durable count plus serialized reservation |
| Initial reward/risk | 2.0 | `>= 2.0` | Deterministic target and authoritative stop; the old 1.5 exceptional bypass is inert |
| Weekly drawdown halt | 4.0% | `<= 5.0%` | Recomputed from durable weekly equity high; activation recorded with timestamp/reason |
| Portfolio drawdown halt | 8.0% | `<= 8.0%` | Recomputed from durable all-time equity high; latched pending human review |
| Loss-streak halt | 3 losses | `<= 3` | Durable completed-trade streak and latched halt |
| Earnings exclusion | 5 trading days | May not be below 5 | Existing stricter-than-requested window retained |
| ATR stop distance | 0.75–3.00 ATR | min 0.50–1.50; max 1.50–4.00; ordered | Tight or wide structural invalidations reject; no percentage fallback |
| Liquidity allocation | 0.10% of ADV | `<= 1.00%` of ADV | Whole-share downward cap |
| Broker minimum | 1 whole share | Immutable at exactly 1 | Fractional shares and rounding up are impossible |

All settings are exposed in `.env.example`. Attempts to weaken hard ceilings fail at settings construction and name the offending parameter.

## Structural stop authority

Each setup validator exposes `ValidationResult.invalidation_level`:

- `TREND_PULLBACK`: below recent/medium-trend support with the configured structural ATR break allowance.
- `BREAKOUT_RETEST`: below the held breakout/retest structure with the ATR break allowance.
- `RELATIVE_STRENGTH_CONTINUATION`: below the continuation consolidation low with the ATR break allowance.

The scanner copies that level into `SwingCandidate.structural_invalidation`. At risk time, Python verifies that it is below the fresh entry quote and between 0.75 and 3.00 ATR away. Missing, nonpositive, too-tight, or too-wide levels reject with `REJECT_SETUP_INVALID`; there is no 7% or other default stop. Execution and trade persistence consume only the risk decision’s authoritative stop.

`TechnicalSwingAnalysis` and `PMStructuredDecision` have no entry, stop, target, quantity, risk, or limit fields and both forbid extra fields. Schema-invalid/unavailable LLM results persist as `REJECT_LLM_SCHEMA`. Tests also pass malicious proposal stop/target/risk values and confirm they cannot increase quantity or change the authoritative stop.

## Position sizing and small-account behavior

The initial quantity is exactly:

```text
risk_amount    = equity × applied_risk_percent
risk_per_share = fresh_entry_quote − authoritative_stop
risk_quantity  = floor(risk_amount / risk_per_share)
```

The final quantity is `min(risk_quantity, cash, position allocation, portfolio-risk headroom, sector-risk headroom, cluster-risk headroom, ADV liquidity)`. Every term is a whole-share integer and every constraint can only reduce size. Cash uses `min(cash, buying_power)`, so buying power cannot introduce margin.

Risk quantity zero returns `REJECT_QTY_ZERO` before cash is considered. A positive risk quantity with less than one share of cash returns the distinct `REJECT_INSUFFICIENT_CASH`. The cycle funnel includes persisted counts, tickers, prices, and risk-per-share for both categories.

### Offline cached dry sizing scan

To avoid credentials and broker calls, I reused the repository’s cached daily bars, substituted explicitly synthetic-clear event/halt metadata solely to let candidates reach sizing, and used a temporary $2,000 paper account:

- 195 cached universe symbols were available from the 439-symbol versioned universe.
- 4 deterministic candidates were returned; 2 met the risk engine’s score and mapping gates and reached sizing.
- ABBV at $247.97 and JNJ at $261.81 each sized to exactly 1 share.
- Quantity-to-zero: 0/2 (0%). Insufficient cash: 0/2 (0%).

This sample is too small to estimate the true unreachable-universe rate. It does show the validity threat: both reachable candidates were already at the one-share floor. With only $15 baseline risk, any structural stop wider than $15 per share makes a candidate impossible regardless of setup quality. The persisted funnel counters must be evaluated over many real paper cycles before drawing conclusions about strategy expectancy or universe reachability.

## Open-risk accounting

Every validation call reloads open trades and active admissions from SQLite/Turso-facing durable state. Broker positions must match durable trades; otherwise the new trade rejects with `REJECT_BROKER_MISMATCH`.

For a filled long position:

```text
open_risk = abs(quantity) × max(cost_basis − current_protective_stop, 0)
```

A stop above cost basis contributes zero open risk. It never contributes negative risk, because allowing one winner to offset another position’s loss exposure would overstate available risk capacity. Unfilled submitted trades and unlinked active admissions conservatively contribute their planned dollar risk.

Each review persists `total_open_risk`, `open_risk_percent`, `risk_by_sector`, `risk_by_cluster`, and `risk_by_position` in `risk_snapshots`. `risk_rejections` stores the machine code, candidate, price, stop distance, risk quantity, all quantity caps, and relevant portfolio values. Final admission repeats portfolio, sector, and cluster checks under the database write lock.

## Correlation clusters

The required clusters are present: Semiconductors, Mega-cap technology, High-beta growth, Financials, Healthcare, Defensives, Industrials, Consumer, and Energy.

The versioned 439-symbol universe now produces a complete ticker→cluster map. Maintained sector labels supply the base mapping, while explicit ticker overrides separate correlated subgroups that sector-only accounting misses. NVDA, AMD, AVGO, TSM, and SMCI are all explicitly assigned to `Semiconductors`; tests exhaust the cluster risk with NVDA/AMD and prove that SMCI is rejected. A ticker outside the maintained map fails closed with `REJECT_NO_CLUSTER_MAPPING`.

## Earnings and event protection

New stock entries require `earnings_data_status=clear`, a nonnegative structured trading-day distance, and earnings farther away than five trading days. Unavailable, stale, conflicting, malformed, or near earnings data rejects deterministically. ETFs skip issuer earnings but do not skip the broader corporate-event status gate.

The default existing-position policy returns `EXIT_BEFORE_EARNINGS` inside the same window and `EXIT_REVIEW_REQUIRED_EVENT_DATA_UNSAFE` for unsafe event data. Prompt 04 must connect that policy to its exit state machine; this prompt deliberately did not place or close an order.

Reliable structured coverage for FDA decisions, M&A, index rebalances, and investor days does not exist in this repository. Production scan metadata therefore sets broader event status to unknown and fails closed. The offline sizing scan above explicitly bypassed only this unavailable-data condition for measurement and did not persist to the repository database.

## Halts and prohibited behavior

Kill switch, reconciliation halt, weekly drawdown halt, portfolio drawdown halt, loss-streak halt, daily/weekly entry counts, durable equity highs, durable loss streak, and open risk are re-read during each admission review. Halt activations are stored in `halt_events` with UTC timestamp, key, reason, and source, in addition to the latched `system_state` value.

Adding to any existing or durable pending symbol returns `REJECT_AVERAGING_DOWN`. Whole-share-only sizing, cash-only exposure, long-only decisions, leveraged/inverse instrument rejection, and stop-widening rejection remain enforced.

## Reason-code coverage

The stable canonical codes include all requested values: `REJECT_EARNINGS_WINDOW`, `REJECT_LOW_RR`, `REJECT_OPEN_RISK`, `REJECT_CLUSTER_RISK`, `REJECT_SECTOR_RISK`, `REJECT_STALE_DATA`, `REJECT_WIDE_SPREAD`, `REJECT_SETUP_INVALID`, `REJECT_ENTRY_CHASE`, `REJECT_DRAWDOWN_HALT`, `REJECT_LOSS_STREAK`, `REJECT_KILL_SWITCH`, `REJECT_LLM_SCHEMA`, `REJECT_BROKER_MISMATCH`, `REJECT_QTY_ZERO`, `REJECT_INSUFFICIENT_CASH`, `REJECT_MAX_POSITIONS`, `REJECT_DAILY_ENTRY_LIMIT`, `REJECT_AVERAGING_DOWN`, `REJECT_NO_CLUSTER_MAPPING`, and `REJECT_LIQUIDITY`. More-specific existing rules remain available through the legacy `rule` field while every rejection also returns `reason_code`.

## Verification

- `poetry run pytest -q`: 194 passed, 0 failed, 0 skipped; 15 existing dependency/deprecation warnings.
- Black and isort checks on every changed Python file: passed.
- flake8 on every changed Python file with the repository’s 420-column/Black-compatible policy: passed.
- `npx --yes pyright`: 0 errors, 0 warnings.
- `git diff --check`: passed.
- Exposure mapping audit: 439/439 configured universe symbols mapped.
- Final repository grep found only the pre-existing legacy `execute_trades.py` mutation surface plus the established swing execution/broker/stop services; this diff introduced no new order path or risk-engine bypass.
- No credentialed request and no broker state-changing command was run.

## Assumptions I made

- The versioned universe’s maintained sector labels are acceptable base data for cluster assignment when an explicit correlation override is not required.
- The 1.00% per-cluster and 1.50% per-sector risk ceilings are conservative starting points, not validated edge estimates.
- A stop above cost basis should count as zero rather than negative open risk.
- The existing one-new-position-per-day and five-trading-day earnings controls must remain because relaxing them to the prompt’s looser maxima would violate the standing no-weakening rule.

## Fail-closed TODOs

- Prompt 04 must consume `existing_position_event_action` in the deterministic exit state machine; no automatic earnings exit was added here.
- Integrate reliable structured FDA, M&A, index-rebalance, and investor-day calendars. Until then, production broader-event metadata remains unknown and blocks new trades.
- Integrate a reliable real-time halt feed. Existing production metadata still fails closed when halt status is unknown.
- Review cluster overrides as the universe evolves; a ticker absent from the versioned map will reject rather than inherit an optimistic default.
- Accumulate real paper-cycle `REJECT_QTY_ZERO` and `REJECT_INSUFFICIENT_CASH` rates before treating the $2,000 experiment as representative.

## Anything I could not verify without real credentials or a live broker

- Live broker asset, quote, cash, position, protective-stop, and quantity payload compatibility.
- Whether broker-reported protective stops and partial fills reconcile exactly to the durable open-risk calculation.
- Turso’s transaction/isolation behavior for simultaneous portfolio/sector/cluster admissions.
- Live earnings-calendar freshness and conflict behavior, or any broader corporate-event feed.
- The true sizing-to-zero rate on a fresh complete 439-symbol scan; the credential-free cached sample covered 195 symbols and only two candidates reached sizing.
