# 05 — Journal, R-Multiple Analytics, Expectancy, and Postmortems

Branch: `refactor/05-journal-analytics`  
Date: 2026-08-11  
Scope: measurement, journaling, analytics, decision versioning, postmortems,
scheduling, notifications, and observability. No strategy admission rule, risk
ceiling, position-sizing rule, broker order shape, or exit trigger was changed.

## Outcome

The paper experiment now has a versioned measurement layer instead of a set of
loosely related P&L columns. Schema version 7 adds an exact closed-trade journal
view, entry-fill lots, candidate/rejection decision records, LLM review state and
cycle counters, structured logs, structured notifications, and durable report
payloads. The migration runs forward from a populated v5 database and preserves
existing rows.

Analytics are R-first and refuse to display bare small-sample statistics. The
configured minimum is **30 closed trades**, both overall and independently in
every segment. Below 30, the value is withheld and replaced by:

```json
{"status":"INSUFFICIENT_SAMPLE","sample_size":3,"minimum_required":30}
```

No dependency was added. Changes in execution and reconciliation are persistence
hooks only: they journal actual partial fills and update the actual initial-risk
basis. They do not change order admission, quantity, stop, target, order type,
broker calls, or lifecycle triggers.

## 1. Exact definition of R

For a long trade, the journal denominator is the actual risk put on by the entry
fill cohort against the original stop:

```text
initial_R_dollars = Σ(entry_fill_quantity × (entry_fill_price - original_stop))
realized_R        = realized_P&L / initial_R_dollars
unrealized_R      = remaining-position P&L / initial_R_dollars
```

The original stop is write-once. The denominator is accumulated while the broker
entry is partially filling, then remains fixed for the rest of the trade. It does
not change with account equity, a later stop movement, MFE/MAE, or the eventual
exit. Entry fill lots are stored in `trade_fills`; cumulative broker updates are
converted into incremental lots before the aggregate entry price and actual
initial risk are recomputed.

Partial exits realize P&L against the actual aggregate entry cost. The remaining
quantity produces `unrealized_R` against the same full original denominator, so
`realized_R + unrealized_R` describes the whole trade at that moment. Because
averaging down and adding to a position are prohibited, a new entry fill is
rejected after an exit fill exists.

A stop trailed above cost basis does **not** create a negative denominator or a
new R. It is represented as positive `stop_locked_R` for the remaining shares:

```text
stop_locked_R = remaining_quantity × (current_stop - average_entry) / initial_R_dollars
```

The immutable thesis retains the approved/intended risk contract; downstream
performance analytics use the journal's actual fill-derived
`initial_risk_dollars` denominator.

Evidence: `src/swing/measurement.py` (`calculate_r_multiples` and
`TradeMeasurementService`), and `src/swing/database.py` (`trade_fills`,
`record_entry_fill`, and `sync_cumulative_entry_fill`).

## 2. MFE and MAE

MFE/MAE use completed **daily high/low bars**, persisted during each deterministic
scan refresh for every open trade:

```text
MFE_R = quantity × max(0, highest_daily_high - entry) / initial_R_dollars
MAE_R = quantity × max(0, entry - lowest_daily_low) / initial_R_dollars
```

Missing or malformed bars do not become zero excursions. They create a structured
`DAILY_BAR_MISSING` or `DAILY_BAR_MALFORMED` observation and leave measurement
unknown until valid data arrives.

Limitations that can mislead:

- Daily bars reveal the extremes but not their intraday order. A bar can touch
  both stop and target without proving which happened first.
- A full entry-day bar may contain a high or low from before the fill; without
  timestamped intraday bars this can overstate entry-day MFE/MAE.
- Gaps and broker fill slippage are captured in actual exit price/slippage, but a
  daily low does not prove the price at which a stop could execute.
- A missed refresh leaves a hole; the code reports it and does not interpolate.

Daily granularity is proportionate for this swing system, but it is not suitable
for execution-quality claims at intraday resolution.

## 3. Journal and migration

`closed_trade_journal` exposes the requested names directly: ticker, setup,
entry/exit timestamps, days held, entry/exit prices, original stop/target, entry
score, regime, sector, risk cluster, initial dollars at risk, realized R, MAE_R,
MFE_R, entry/exit slippage, exit reason, entry earnings distance, technical
invalidation, LLM verdict, and risk-rejection history.

The underlying trade row also persists:

- `unrealized_r`, process-quality score, and independent outcome;
- config/scanner/model/prompt versions and market-data timestamp;
- deterministic feature values and a risk-settings snapshot;
- volatility bucket and risk cluster;
- actual entry fill lots with price, quantity, time, broker evidence, and
  idempotent broker-fill keys.

Every scanner rejection is persisted as the control group in `decision_records`
with reason code, stage, and all computed values available at the rejection
point. Accepted candidates store their validator features and score components.
Risk-engine rejections remain in `risk_rejections` and are mirrored to structured
reason-code logs. `why_not_trade(ticker, date)` unions scanner and risk evidence,
so the question can be answered without a new scan.

The forward migration is explicit and idempotent:

- v5 → v6 adds journal/version/postmortem columns and operational tables;
- v6 → v7 adds fill-lot persistence and the exact journal view;
- a populated-v5 fixture proves the old row survives and both migration versions
  are recorded.

## 4. Reproducible decisions and LLM cost control

Every persisted decision envelope contains strategy version, config version,
scanner version, model name, prompt version, market-data timestamp, feature
values, entry score, risk-settings snapshot, decision payload, and a SHA-256
fingerprint over canonical secret-redacted JSON. Two runs with identical inputs
and versions persist the same fingerprint and decision content.

The LLM cache key is ticker + setup + role + model + prompt version. A repeat call
is suppressed only when setup, regime, route, validator failures, earnings/event
state, optional material-news/event digest, and score (within the configurable
five-point materiality threshold) are unchanged. Model or prompt changes always
miss the cache. Each cycle persists actual calls, suppressed calls, and reason
counts; `run` prints the same counts.

One limitation remains important: the repository has no authoritative structured
material-news digest. The cache supports one when supplied, and changes to known
event/earnings state invalidate it, but it cannot detect news absent from all
inputs. Existing broader corporate-event status remains fail-closed at entry.

## 5. Analytics

The engine computes trade count, win rate, average winner/loser in R, expectancy,
median R, profit factor, cumulative-R max drawdown, average MAE/MFE, average hold,
maximum consecutive losses, trade-R Sharpe/Sortino where denominators exist,
daily-equity Sharpe/Sortino/max drawdown, and exact-period performance versus SPY
when at least 30 aligned return observations exist.

Every trade metric is segmented independently by:

- setup;
- market regime;
- sector;
- entry-score bucket;
- holding-period bucket;
- volatility bucket.

Segment-level SPY attribution is marked `NOT_MEANINGFUL_FOR_TRADE_SEGMENT` rather
than fabricated from a portfolio benchmark series. Overall SPY comparison uses
the aligned equity/benchmark snapshot series. Funnel output includes all scanner
reason codes plus the exact counts and candidate evidence for valid candidates
lost to whole-share rounding and insufficient cash.

Expectancy uses the requested loss-magnitude convention:

```text
expectancy_R = win_rate × average_winner_R
             - loss_rate × average_loser_R
```

## 6. Worked fixture output

This is the rendered shape for a synthetic 42-trade fixture. The values are
illustrative, not evidence that the strategy has edge.

```text
TREND_PULLBACK

Trades:          42
Win rate:        50.0%
Avg winner:      +2.40R
Avg loser:       -0.88R
Expectancy:      +0.76R
Profit factor:    2.73
Median:          +0.76R
Max drawdown:     -2.64R
Average hold:      6.2 days
Average MAE:       0.65R
Average MFE:       1.57R
MAE winners:       0.42R
MFE losers:        0.73R
Consecutive losses: 3

Daily equity observations: 42
Sharpe:            1.10
Sortino:           1.60
Strategy return:  +5.8%
SPY return:       +3.1%
Excess vs SPY:    +2.7 percentage points
```

An unfavorable setup is equally visible:

```text
BREAKOUT_RETEST

Trades:          34
Win rate:        38.2%
Avg winner:      +1.31R
Avg loser:       -1.04R
Expectancy:      -0.14R
Profit factor:    0.78
```

A three-trade segment is rendered only as:

```text
RELATIVE_STRENGTH_CONTINUATION

Trades: 3
Win rate:   INSUFFICIENT_SAMPLE (3/30)
Avg winner: INSUFFICIENT_SAMPLE (3/30)
Avg loser:  INSUFFICIENT_SAMPLE (3/30)
Expectancy: INSUFFICIENT_SAMPLE (3/30)
```

No suppressed value is included behind the marker, which prevents downstream
JSON consumers from accidentally presenting it as meaningful.

## 7. Process-quality postmortems

Outcome is stored separately as `WIN`, `LOSS`, or `BREAKEVEN`. Process answers are
stored individually, with a process-quality score over known answers and an
explicit coverage fraction. The cross-tab is queryable directly from
`postmortems(outcome, process_quality_score)` or the mirrored trade columns.

Deterministic checks own original validator validity, scanner identity, risk
approval, sizing versus planned risk, structural stop placement, entry slippage,
and earnings-gate compliance. They are supplied to an optional LLM as read-only
facts with an instruction not to override them. The model may judge entry timing,
thesis validity, exit quality, process versus luck, and whether the same
information would justify the trade again.

The score never includes outcome. Tests close two trades with identical process
answers and opposite outcomes and prove the process score is identical. Coverage
must be read with the score: 100% on two known answers is not equivalent to 100%
on all answers.

Weekly aggregation still reads only `OBSERVATION` rows and can create only a
candidate research hypothesis. It cannot validate a lesson, approve a strategy,
or write production configuration. The config-file hash remains unchanged in the
boundary test.

## 8. Scheduling, notifications, and observability

`src/swing/scheduling.py` defines seven configurable ET jobs: 09:35 scan, 10:15
decision, 12:30 scan, 14:30 scan, 15:45 reconcile/health, 16:15 daily report, and
Sunday lessons aggregation. Prompts allow exactly one subcommand and explicitly
forbid every other command and all live/broker mutations. The documentation now
requires an `America/New_York` scheduler so DST cannot shift market jobs.

Structured durable notification types cover high-quality candidate, paper open,
risk rejection, stop, target, close, broker/database mismatch, kill switch,
daily summary, weekly summary, and experiment milestone. Routine scanner rejects
do not notify. A no-trade day emits one deduplicated `INFO` daily summary with
reason `NO_TRADE`.

Structured logs and decision indexes support ticker/date/reason queries. A
recursive redactor protects recognized secret keys and token shapes before JSON,
logs, decision inputs, notifications, reports, or errors are persisted. The
secret fixture searches all relevant stored rows and finds only `[REDACTED]`.

Outbound notification delivery is intentionally not invented: these records are
durable and channel-neutral, but no email/SMS/chat connector exists in the repo.

## 9. Verification

- `poetry run pytest -q`: **287 passed, 0 failed, 0 skipped**; 15 existing
  dependency/deprecation warnings.
- New prompt-specific tests cover all requested R, bar excursion, expectancy,
  segmentation, sample guard, round-trip, migration, deterministic decision,
  postmortem independence, lessons isolation, LLM suppression, and secret cases.
- Black, isort, and flake8 pass on every changed Python file.
- `npx --no-install pyright`: 0 errors, 0 warnings.
- `git diff --check`: passed.
- No dependency was added.
- No credentialed service, LLM, Turso, or broker request was made.
- No broker-order command was run.

## Assumptions I made

- The original entry cohort ends when the broker entry order reaches its final
  filled/canceled state; after the first exit fill, further entry fills are
  invalid and rejected.
- Broker cumulative quantity and average-fill price are arithmetically sufficient
  to reconstruct incremental entry lots when individual fill events are absent.
- Completed daily bars are the right default excursion granularity for a swing
  experiment, provided their limitations above remain visible.
- Thirty closed trades per independent segment is the minimum useful exploratory
  floor, not proof of statistical significance or strategy persistence.
- Trade-R Sharpe/Sortino and daily-equity Sharpe/Sortino answer different questions
  and should never be compared as if they share a sampling frequency.
- A process score must always be interpreted beside its evidence-coverage fraction.

## Fail-closed TODOs

- Add an authoritative material-news/event digest. Until then, unavailable
  broader event state continues to block entry; the LLM cache cannot react to
  information that never enters the system.
- Add timestamp-filtered intraday bars if execution research later needs exact
  entry-day/exit-day MFE/MAE or stop-versus-target ordering.
- Connect durable notifications to a real outbound channel with retry and delivery
  acknowledgements; do not place credentials in notification payloads.
- Populate aligned SPY and daily equity snapshots consistently; analytics withhold
  Sharpe, Sortino, drawdown, and SPY excess return until the 30-observation floor.
- Validate migration DDL, triggers, fill uniqueness, and concurrent writes against
  hosted Turso before relying on it beyond the paper experiment.

## Anything I could not verify without real credentials or a live broker

- Actual Alpaca partial-fill event ordering, cumulative-average rounding, replaced
  order IDs, final fill timing, and gap-through exit payloads.
- Whether real daily bars arrive soon enough after every close to avoid a final
  excursion gap.
- Hosted Turso migration/trigger/index behavior and concurrent serialization.
- Real material-news change detection because no authoritative feed is configured.
- Delivery behavior for an outbound notification channel; only durable in-product
  records were implemented and tested.
