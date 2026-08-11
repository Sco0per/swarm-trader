# Adaptive Swing Architecture

`src/swing/` is the sole supported operational package. `swing-trader` is the sole installed console command. The framework is long-only, daily-bar swing research with Alpaca paper execution and human-only real-capital tickets.

## Authority boundaries

```text
data_feed + versioned universe
  -> DeterministicSwingScanner
  -> strategy.validate_setup / score_entry
  -> AgentPipeline advisory veto
  -> SwingRiskManager
  -> SwingExecutionService
  -> AlpacaPaperProvider
  -> BrokerReconciler / lifecycle / measurement / postmortem / reporting
```

The scanner and strategy validator own setup identity, features, score, route, structural invalidation, and target. The model pipeline receives only strong deterministic candidates and can return advisory analysis or a veto. Its schemas contain no price, quantity, risk, or limit fields.

`SwingRiskManager` re-derives the structural stop, R:R, whole-share quantity, cash cap, position value, portfolio/sector/cluster open risk, liquidity cap, drawdown/loss/kill latches, event state, instrument eligibility, and freshness immediately before admission. `SwingExecutionService` is the only entry and manual-close boundary. `ProtectedStopService` is the only stop-amendment boundary and calls the same risk manager. Reconciliation may request only safety-reducing emergency exits when protection is missing or broker truth proves a lifecycle close is required.

The only external mutation endpoint is hard-coded to Alpaca paper in `src/swing/brokers/alpaca.py`. Human-supplied live and Robinhood providers raise on every mutation method. Scheduled jobs instantiate only the Alpaca paper provider for `run` or `reconcile`; live commands are never scheduled.

## Deterministic strategy

Exactly three setup enums exist: `TREND_PULLBACK`, `BREAKOUT_RETEST`, and `RELATIVE_STRENGTH_CONTINUATION`. Each validator returns every condition, stable failure codes, computed features, invalidation, and target. A hard failure cannot be rescued by score or model output. Missing sector, event, halt, earnings, quote, bar, broker-asset, or market-clock data fails closed.

## Risk and lifecycle

Baseline risk is 0.75% of equity and can only be reduced. Quantity is whole-share risk sizing and is capped by cash, 35% position value, 2% portfolio risk, 1.5% sector risk, 1% cluster risk, and 0.1% ADV. Minimum initial R:R is 2.0. Weekly/portfolio drawdown and three-loss halts are durable and human-cleared.

Entries use one serialized, idempotent GTC paper bracket. Broker-native stop and target protection is verified after fills and on restart. Position lifecycle is `OPEN -> PROTECTED -> PROFITABLE -> TRAILING -> EXIT_PENDING -> CLOSED`; illegal transitions raise. Stops never widen. The original thesis and original R denominator are immutable.

## Data and persistence

The versioned CSV universe accepts common stocks plus a maintained allowlist of unleveraged ETFs. Blacklist, membership, sector, and cluster data fail closed. Local SQLite is the default system of record; Turso is optional. Explicit migrations, constraints, triggers, unique intent/fill keys, and serialized admissions preserve durability and idempotency.

Journal measurement uses actual fills against the original stop. Daily highs/lows support MAE/MFE with explicit missing-data records. Analytics withhold small-sample statistics below 30 closed trades overall or within each segment.

## Research isolation

`autoresearch/` is quarantined intraday-derived code and has no dependency into `src/swing`. It cannot write production settings or promote results automatically. `src/autoresearch_swing/` records hypotheses and requires minimum samples plus human approval. The legacy personality research application has no broker integration and its simulator rejects margin, negative holdings, and short/cover actions.

## Safety posture

Unknown data rejects; zero trades is valid. Models can veto but cannot authorize. Paper broker truth is reconciled before trust. Ambiguous order outcomes are never retried. Real-money tickets print only and require a human to execute separately. No component is production-ready without credentialed read-only QA, real paper broker-behavior QA, complete event/halt sources, and a reviewed holdings blacklist.
