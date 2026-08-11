# 04 — Execution, Position Lifecycle, LLM Boundary, and Reconciliation

Branch: `refactor/04-execution`  
Date: 2026-08-11

## Outcome

The supported swing path now implements the complete boundary between an approved deterministic risk decision and a reconciled close. Every entry is rechecked against fresh broker/candidate state, reserved with a deterministic client order ID, submitted as a whole-share broker bracket, persisted with an immutable original thesis, and moved through a durable lifecycle. Missing protection or ambiguous broker truth latches trading off and records an operational alert.

The model pipeline is advisory and negative-selection oriented. It now has exactly four active roles: Technical Context Analyst, Catalyst/Fundamental Analyst, Bear Case/Failure Critic, and Portfolio Manager/Final Proposal Reviewer. The former active bull-persona stage was removed. Model `APPROVE` is converted only into an internal proposal; it never authorizes execution and is followed by deterministic strategy, risk, freshness, broker-state, and reconciliation validation.

No broker command, paper submission command, amendment command, cancellation command, credentialed request, LLM request, or other real network action was executed during this prompt. All broker and LLM behavior was exercised with mocks.

## End-to-end trade lifecycle

| Stage | Authority and behavior | Failure behavior |
|---|---|---|
| Scan | `DeterministicSwingScanner` evaluates the configured universe and the three allowed setup geometries. | Hard validator failure or missing required data does not become an LLM candidate. |
| Candidate | Python owns setup, score, structure, target, structural invalidation, liquidity, regime, earnings status, event status, and timestamps. | Only `strong`/`very_strong` candidates with no validator failures enter model review. |
| LLM review | Technical → Catalyst/Fundamental → Bear Case → Portfolio Reviewer. Structured schemas forbid extra fields. | Schema error, timeout, unavailable/unconfigured model, data gap, unknown ticker, invalid/mismatched setup, identity mismatch, `REJECT`, or `WATCH` produces no proposal (`NO_TRADE` behavior). |
| Strategy validation | Python rechecks that proposal identity/setup/score/regime match the deterministic candidate. | Stable rejection reason; terms are never loosened. |
| Risk validation | Python re-derives authoritative structural stop, current R:R, whole-share size, cash/exposure/open-risk/sector/cluster/liquidity limits, drawdowns, loss streak, earnings/events, and entry slippage. | Rejection with a machine reason code; no retry with wider risk or target. |
| Pre-execution broker gate | Reconcile, refresh account/buying power/positions/orders, asset metadata, quote, spread, market clock, tradability, halt state, and all freshness timestamps immediately before admission. | Any missing/stale/conflicting/unavailable item rejects. A reconciliation mismatch latches new entries off. |
| Admission/idempotency | UUIDv5 intent from strategy version + decision ID + ticker + side; SQLite/Turso `entry_admissions.intent_id` primary key; unique reservation/submission indexes; broker `client_order_id`. | The same decision cannot reserve or submit twice. Unknown broker outcome retains the reservation and latches reconciliation halt. |
| Order | Whole-share GTC Alpaca bracket with entry limit, stop-loss, and take-profit sent atomically. | Terminal empty entry releases the admission. Unknown/unexpected status halts for reconciliation. |
| Broker protection | A filled/partial-filled response must prove active stop and target legs. Reconciliation repeats this proof after restart and recognizes an active replacement leg. | Missing/rejected/cancelled protection triggers `EXIT_PENDING`, a latched halt, a documented emergency-close request, and a durable remediation event. |
| Position management | Immutable original thesis remains separate from current stop/high-watermark/lifecycle context. Named stop policy and time-stop policy evaluate current state. | Illegal lifecycle transitions raise. Stop widening remains prohibited. |
| Reconciliation | Broker is external execution truth; database is the internal journal. Reconcile before non-dry submission, after restart, and after ambiguity. | Mismatch is stored in `reconciliation_mismatches`, a critical `operational_alert` is created, and trading remains latched off even after a later clean snapshot. |
| Exit/close | Broker stop/target fills, gap-through stops, thesis invalidation, trend failure, event risk, configured time stop, or manual safety exit enter `EXIT_PENDING`; verified fill enters `CLOSED` and runs postmortem accounting. | Partial/unverified exit quantity or unexplained position changes halt reconciliation. |

## Pre-execution gate and freshness defaults

The gate uses fresh state from the broker immediately before each order, not the earlier scan. Every threshold is configurable and startup-validated:

| Setting | Default | Enforcement |
|---|---:|---|
| `QUOTE_FRESHNESS_SECONDS` | 300 seconds | Both retrieval and market timestamps required; missing/future/stale rejects. |
| `BROKER_ASSET_FRESHNESS_SECONDS` | 60 seconds | Asset identity, class, active/tradable state and timestamp required. |
| `MARKET_CLOCK_FRESHNESS_SECONDS` | 60 seconds | Broker review must explicitly return `market_open=true` and a fresh timestamp. |
| `CORPORATE_EVENT_FRESHNESS_SECONDS` | 86,400 seconds | Explicit clear status and timestamp required. |
| `EARNINGS_FRESHNESS_SECONDS` | 86,400 seconds | Non-ETF entry requires clear structured earnings data and timestamp. |
| `STRATEGY_MAXIMUM_BAR_AGE_DAYS` | 7 calendar days | Completed daily-bar timestamp must be present and fresh. |
| `MAXIMUM_ENTRY_SLIPPAGE_R` | 0.25R | Current quote beyond intended entry by more than this rejects; startup refuses values above 0.25R. |
| `MAXIMUM_SPREAD_PCT` | 0.50% | Spread must be present and no wider than the configured ceiling. |
| `MINIMUM_RR` | 2.0 | R:R is recomputed from current quote, deterministic target, and structural stop. Target is never widened to compensate. |

Independent tests cover stale quote retrieval, stale quote market time, missing market timestamp, stale daily candidate, stale account, incomplete daily bar, missing/stale corporate-event timestamp, missing/stale earnings timestamp, missing spread, stale broker asset, and closed market clock.

## Broker-native protection and idempotency

Alpaca remains paper-only and receives one GTC bracket payload. Fractional shares remain structurally impossible. The broker response is checked for `order_class=bracket`; once any quantity is filled, both an active stop leg and active target leg must be visible. Reconciliation flattens nested bracket orders, accepts a valid replacement leg, rejects a terminal protective leg without replacement, and uses actual fills for gap-through-stop accounting.

Idempotency is layered deliberately:

1. Deterministic UUIDv5 intent key.
2. Durable `entry_admissions.intent_id` primary key and unique `decision_id`.
3. Unique `ORDER_RESERVED` and `ORDER_SUBMITTED` event indexes.
4. The same intent becomes the broker `client_order_id` (Alpaca-compatible 48-character form).
5. An ambiguous network outcome remains `UNKNOWN`; it is not retried.

I implemented the remediation-path option for protective-leg failure. Atomic bracket creation prevents intentionally naked entry, but a partial fill can coexist with a later rejected/cancelled/missing child leg. In that case the system cannot pretend the entry never happened: it persists the trade, creates `OPEN`, immediately moves to `EXIT_PENDING`, latches reconciliation halt, requests an emergency close through the broker boundary, and records whether that request succeeded or failed. No new entry is allowed while truth remains ambiguous.

## Persisted position lifecycle

Canonical sequence:

```text
OPEN → PROTECTED → PROFITABLE → TRAILING → EXIT_PENDING → CLOSED
```

Legal transitions and triggers:

| From | To | Trigger |
|---|---|---|
| — | `OPEN` | Verified broker fill/partial fill or restart recovery of a broker position. |
| `OPEN` | `PROTECTED` | Active broker stop and target legs verified. |
| `OPEN` | `EXIT_PENDING` | Emergency remediation because protection could not be verified. |
| `PROTECTED` | `PROFITABLE` | Named policy's configurable profit threshold is reached (default 1.0R). |
| `PROTECTED` | `EXIT_PENDING` | Structural/target fill detected, thesis invalidation, technical failure, event exit, automatic time stop, or manual safety exit. |
| `PROFITABLE` | `TRAILING` | Named trailing policy is enabled, setup is eligible, and activation threshold is reached (default 2.0R). |
| `PROFITABLE` | `EXIT_PENDING` | Any deterministic exit trigger. |
| `TRAILING` | `EXIT_PENDING` | Any deterministic exit trigger or broker exit ambiguity. |
| `EXIT_PENDING` | `CLOSED` | Broker exit quantity and actual fill are reconciled. |

All other transitions raise `IllegalLifecycleTransition`; tests enumerate every illegal state pair. State and transition evidence live in `position_lifecycle` and `lifecycle_events`, survive restart, and use an expected-state compare-and-swap update.

### Stop management

`STOP_MANAGEMENT_POLICY` is named and swappable:

- `STRUCTURAL_ONLY`
- `STRUCTURE_THEN_TRAIL` (default)

Parameters include `PROFITABLE_TRIGGER_R=1.0`, `TRAILING_ACTIVATION_R=2.0`, `TRAILING_DISTANCE_R=0.75`, and `TRAILING_ELIGIBLE_SETUPS`. `MOVE_TO_BREAKEVEN_AT_R` is blank/disabled by default. Reaching +1R therefore marks profitability but does not automatically move every stop to breakeven. Every proposed stop still passes the no-widening control before broker replacement.

### Time stop

Defaults: expected hold 4 trading days, maximum hold 10, minimum progress 0.50R, `TIME_STOP_MODE=REVIEW`.

- Maximum hold triggers the configured action regardless of trend.
- At/after the expected window, the action triggers only when progress is below the configured R threshold **and** trend has degraded.
- `REVIEW` is the default conservative operational choice because this codebase keeps exit mutations human-supervised unless broker-native protection fires.
- `EXIT` is also implemented and deterministically moves the lifecycle to `EXIT_PENDING`.

## Immutable thesis contract

Every opened paper or recorded human-supervised live position stores a `TradeThesis` containing ticker, setup, original entry/stop/target, original total and per-share risk, original R:R, entry score, regime, expected/max hold, technical structure, and invalidation statements.

The mutable `trades` journal and `position_lifecycle` runtime state are separate. `trade_theses` has database triggers rejecting both `UPDATE` and `DELETE`; the trade row and thesis insert are atomic. `update_trade` also denies original thesis-like columns. Tests prove both Pydantic immutability and a direct storage overwrite failure. Explicit corporate-action evidence may adjust current quantity/entry/stop state without changing the original thesis.

## Exact LLM boundary

### Models may

- Analyze only candidates that already passed deterministic screening.
- Identify technical contradictions and invalidations.
- Identify earnings, guidance, offerings/dilution, corporate actions, lawsuits, regulatory risk, acquisitions, FDA events, executive departures, sector/macro catalysts, abnormal headline risk, and data gaps.
- Act as an independent bear-case critic and kill a weak/rationalized setup.
- Return structured `APPROVE`, `REJECT`, or `WATCH` advisory verdicts with reasoning.

### Models may not

- Set or change entry, stop, target, quantity, position size, risk percentage, portfolio/sector/cluster limit, drawdown threshold, liquidity rule, earnings gate, or event gate.
- Submit, amend, cancel, replace, or close an order.
- Override a Python rejection, loosen validation, select an unknown ticker/setup, or authorize execution.
- Receive credentials, secrets, account identifiers, buying power, broker client-order IDs, or unrelated raw records.

`PMStructuredDecision` has no entry/stop/target/quantity/risk/limit field and forbids extras. Adversarial tests inject quantity, stop, risk limit, position size, and earnings-gate override fields; each becomes no trade. Prompt payloads are explicit allowlists, not candidate/account object dumps. Tests also cover malformed output, timeout at every role, missing model configuration, unknown ticker, invalid/mismatched setup, `REJECT`, and `WATCH`.

Legacy Buffett/Munger/Burry/Wood/Lynch/Ackman-style modules remain in the legacy research application but have no import or runtime dependency from `src/swing`. An AST guard test enforces that separation. The production swing pipeline no longer calls a bull persona or creates fake persona consensus.

## Reconciliation and deliberate recovery

Broker state is treated as external truth; database state is the durable internal journal. The reconciler covers root orders, nested/replaced legs, partial fills/exits, actual exit prices, gap-through stops, restart recovery, unknown admissions, untracked positions/orders, short/manual positions, and explicit split/reverse-split evidence.

On mismatch:

1. `reconciliation_halt=true` is latched.
2. A durable mismatch row and critical operational alert are inserted.
3. No new order may pass risk validation.
4. A later clean snapshot does **not** automatically clear the halt.
5. `swing-trader ack-reconciliation --approved-by ... --reason ...` requires a clean last reconciliation plus a named human and reason before clearing.

The human-supervised live path also closes a former journal bypass. `live-ticket` now stores a short-lived deterministic approval. `record-live-fill` must consume it exactly once, match the whole-share quantity and entry/R:R geometry, and prove active stop/target order IDs before it may create an immutable thesis and `PROTECTED` lifecycle.

## Legacy mutation isolation

The pre-refactor mutation functions in `execute_trades.py`, `rebalance.py`, and `src/alpaca_integration.py` now raise before any broker network I/O. Their read-only/research compatibility surfaces were retained. AST tests verify each legacy placement/cancellation/flattening primitive begins with a hard failure. Production enums expose no day-trade, short, margin, option, crypto, or leveraged-ETF decision/setup mode; existing risk tests continue to reject margin accounts, unsupported asset classes, negative/short positions, and leveraged/inverse/volatility-linked instruments.

The supported broker mutation path is therefore `SwingExecutionService`/`BrokerReconciler`/`ProtectedStopService` behind the unchanged deterministic risk engine. The read-only human-supplied live provider still raises on every state-changing method.

## Tests and verification

- `poetry run pytest -q`: **268 passed, 0 failed, 0 skipped**; 15 existing dependency/deprecation warnings.
- Black check on every Python file changed from Prompt 03: passed.
- isort check on every Python file changed from Prompt 03: passed.
- flake8 on every changed Python file with repository line length and Black-compatible `E203` exclusion: passed.
- `npx --no-install pyright`: **0 errors, 0 warnings**.
- `git diff --check`: passed.
- Final mutation/risk-bypass search: only the supported swing broker boundary plus hard-disabled legacy code remains; no model or sibling module can reach the swing admission path without deterministic risk validation.
- No dependency was added.
- No credentialed or state-changing broker/LLM command was run.

## Assumptions I made

- Alpaca continues to return nested bracket legs and replacement linkage consistently enough for the normalized evidence matcher; real payload QA is still required.
- `REVIEW` is the more conservative default time-stop action for a human-supervised execution system; a human should decide whether research supports automatic `EXIT` later.
- The default trailing-eligible setups (`TREND_PULLBACK`, `RELATIVE_STRENGTH_CONTINUATION`) are research starting points, not proven sources of edge.
- Explicit corporate-action records with old/new quantity and ratio are adequate evidence for current-state adjustment; unexplained quantity changes must remain mismatches.
- A fresh cached earnings lookup is acceptable within the configured 86,400-second cache/freshness window.

## Fail-closed TODOs

- Integrate authoritative structured FDA, M&A, index-rebalance, investor-day, and other corporate-event feeds. Production event status remains unknown and blocks entries until then.
- Integrate a reliable real-time halt feed. Broker `active/tradable` alone remains insufficient proof that an instrument is not halted.
- Connect `operational_alerts` to a real outbound notification channel. Until then, mismatch notification is durable/in-product rather than email/SMS/chat delivery.
- Validate Turso trigger, compare-and-swap, and serialized admission behavior under real concurrent libSQL traffic.
- Research time-stop and trailing-policy parameters with sufficient out-of-sample trade data before changing defaults.

## Anything I could not verify without real credentials or a live broker

- Actual Alpaca bracket/partial-fill/replaced-leg/corporate-action payload shapes, timing, child-leg activation, and emergency-close acknowledgement.
- Real gap-through stop slippage and whether all relevant fills arrive in the queried history window.
- Real Robinhood human-ticket execution/protection evidence beyond mocked snapshot inputs; the provider intentionally cannot mutate or query a live account automatically.
- Live quote/market-clock/asset freshness latency, real spreads, halt flags, or order idempotency behavior at the broker.
- Real LLM provider timeout/schema behavior and prompt delivery; the boundary was tested only with mocked structured backends.
- Outbound delivery of reconciliation alerts because no notification connector exists in the repository.
