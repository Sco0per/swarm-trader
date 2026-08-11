# Adaptive Swing Implementation QA Report

Date: 2026-08-09  
Branch: `adaptive-swing-agent`  
Production strategy baseline: `SWING_V1.0`  
Default execution posture: paper, `TRADING_ENABLED=false`

## Outcome

The highest-risk audit defects have been remediated in code. The legacy monitor is fail-closed, swing brackets are GTC, every non-dry entry requires broker reconciliation, final entry admission is serialized in SQLite, unknown outcomes latch a global halt, broker closures invoke postmortems, and hard configuration limits cannot be weakened.

The code is suitable for dry-run review. It is **not yet authorized for Alpaca paper order submission** because real paper credentials and broker lifecycle QA were unavailable. It is not ready for live or Robinhood execution.

## Safety remediation

- Replaced `portfolio_monitor.py` with a compatibility shim that performs no network or order operations. Zero/negative stop thresholds cannot trigger.
- Changed Alpaca bracket `time_in_force` from DAY to GTC.
- Added `ProtectedStopService`; it validates the durable stop, while the provider independently reads the current broker stop and rejects widening.
- Added schema-v2 `entry_admissions`. `BEGIN IMMEDIATE` serializes final daily, weekly, position-count, and 2%-open-risk admission across distinct concurrent intents.
- Added mandatory broker reconciliation before every non-dry submission.
- Unknown order outcomes and unexpected broker states latch `reconciliation_halt`. Automatic retries are forbidden. A clean reconciliation is the only automatic clear path.
- Added restart recovery from persisted admission payload plus broker client-order ID/history.
- Added reconciliation of orders, nested bracket legs, positions, entry/exit fills, partial exits, weighted exit price, and trade snapshots.
- Broker-detected full exits automatically close the durable trade, calculate R/MFE/MAE, persist a postmortem, and create an observation.
- Three completed losses now latch `loss_streak_halt`, generate a review, and require named human approval to clear.
- Environment/dataclass settings may be stricter but cannot weaken 0.50% normal risk, 0.75% A+ risk, 1% absolute risk, 2% combined risk, score 80, three positions, one/day, three/week, 5%/8% drawdown controls, liquidity floors, or freshness bounds.
- Future quote, market, candidate, and account timestamps are rejected.
- Broker asset identity/class/tradability/status can veto upstream candidate metadata. The Alpaca account margin flag now comes from its reported multiplier.
- Known terminal broker rejections release their admission without creating a trade; unknown statuses halt for reconciliation.
- Repaired the obsolete API rate-limit test so it explicitly tests the retained `api_original.py` implementation and no longer breaks full-suite collection.

## Architecture and data model

The supported path is:

```text
deterministic daily scanner
  -> structured analyst/red-team/PM schemas
  -> mandatory broker/database reconciliation
  -> deterministic risk authority
  -> serialized durable admission
  -> Alpaca paper GTC bracket
  -> reconciliation/snapshots
  -> automatic close/postmortem/observation
  -> statistics/hypothesis validation
  -> explicit human strategy approval
```

SQLite schema version 2 contains `trades`, `trade_snapshots`, `market_regimes`, `candidate_scores`, `agent_decisions`, `postmortems`, `lessons`, `hypotheses`, `strategy_versions`, `backtests`, `experiments`, `rule_violations`, `execution_events`, `entry_admissions`, `equity_snapshots`, `benchmark_snapshots`, `model_costs`, `system_state`, and `schema_migrations`.

The operational database was initialized at `data/adaptive_swing.db`. It is gitignored, contains the `SWING_V1.0` production baseline, and currently contains no trades or personal/account data.

## Risk authority

- Quantity is `floor(allowed_dollar_risk / (live_entry - technical_stop))`, rounded down and capped by cash/exposure.
- Normal risk is at most 0.50%; eligible A+ risk is at most 0.75%; no configured/requested path can exceed 1%.
- At 5% drawdown, normal risk is 0.30%; at 8%, a durable human-review halt is latched.
- Maximum three simultaneous positions, one new entry/day, three/week, and 2% combined planned open risk.
- No shorts, new-entry margin borrowing, leverage, averaging down, adds, stop widening, stale/future data, duplicate intents, blacklisted holdings, or unsuitable regime.
- Technical invalidation precedes sizing. Normal minimum reward/risk is 2R; the 1.5-2R exception requires explicit evidence.
- Missing broker truth, unknown outcomes, and untracked orders/positions fail closed.

## Broker status

- `FakeBrokerProvider`: network-free; used for all submission/reconciliation safety tests.
- `AlpacaPaperProvider`: paper endpoint only, current quote/clock/account/asset checks, GTC bracket, stable client intent ID, status/history, close/cancel, and protected stop replacement.
- `RobinhoodMCPProvider`: deliberately unavailable. No MCP tools were exposed, so no schemas or execution were fabricated.

## Verification

| Check | Result |
|---|---:|
| `pytest tests/swing -q` | 55 passed |
| `pytest -q` | 99 passed |
| Pyright | 0 errors, 0 warnings |
| Python compileall | passed |
| CLI `init-db` and `status` | passed, schema v2 |
| Real Alpaca calls | not run; credentials unavailable |

Regression coverage includes technical sizing, normal/A+/hard caps, immutable configuration bounds, daily/weekly/open-risk limits, simultaneous candidates, drawdown/loss/reconciliation halts, GTC payload, terminal and unknown broker outcomes, restart recovery, partial and full fills, weighted exits, automatic postmortems, provider/service stop-widening refusal, stale/future timestamps, blacklist/instrument/tradability checks, malformed schemas, scanner scoring/filtering, SQLite restart persistence, lesson transitions, and human-only AutoResearch promotion.

No automated test can place a real order.

## Backtest results

No swing edge or performance result is claimed. The repository does not contain a sufficiently broad, versioned, multi-regime dataset for a defensible 200-day scanner backtest with chronological train/validation/out-of-sample and benchmark comparisons. Existing backtesting regression tests pass, but synthetic control metrics are not strategy evidence.

## Remaining blockers before paper submission

1. Configure a dedicated Alpaca paper account and verify credentials/account identity.
2. Populate `config/do_not_trade.yaml` from the operator's actual long-term holdings and wash-sale-sensitive equivalents; this cannot be inferred safely.
3. Connect and validate the production broad-universe, OHLCV, earnings, halt, sector, spread, and corporate-action feeds.
4. Configure and contract-test a concrete structured-model backend. The current role boundary safely returns no proposals when models are unavailable.
5. Observe actual GTC child-leg creation, overnight persistence, partial take-profit resizing, cancel/replace, rejection, and corporate-action behavior. Alpaca DNR/DNC handling needs an explicit operating procedure.
6. Schedule `swing-trader reconcile` as an independent recurring health check in addition to mandatory pre-submission reconciliation.
7. Rehearse kill-switch, unknown-outcome, connectivity-loss, and emergency-exit procedures.

Additional research limitations remain: AutoResearch accepts caller-supplied experiment metrics rather than running a full independent backtest; statistical confidence/bootstrap analysis and a connected read-only React dashboard are not complete.

## Safe operating instructions

Keep `TRADING_ENABLED=false`.

```bash
poetry run pytest
poetry run swing-trader status
poetry run swing-trader paper-review intent.json
```

After credentials are configured, `poetry run swing-trader reconcile` may be used read-only against the paper account. Do not pass `--submit` until every paper blocker above is checked. Never run the legacy monitor as an execution tool; it is disabled by design.

## Live/Robinhood gate

Live execution requires all paper-to-live checklist items, adequate out-of-sample and paper evidence, zero major control failures, dedicated account isolation, reviewed strategy/capital approval, and a functioning verified live provider. Robinhood additionally requires a real installed MCP connector and contract tests. None of those conditions are currently met, so live and Robinhood must remain disabled.
