# Adaptive Swing Architecture

Status: implementation baseline for `SWING_V1.0`  
Safety posture: paper trading, new entries disabled unless explicitly enabled

## Existing architecture audit

The repository currently has two overlapping systems:

1. The LangGraph research application in `src/` gathers price/fundamental data, fans it out to many investor-personality agents, sends their signals to an LLM-assisted risk agent, and asks an LLM portfolio manager for actions. `src/alpaca_integration.py` may then send those actions to Alpaca.
2. The V2 operational scripts (`scan_market.py`, `run_hedge_fund.py`, `risk_manager.py`, `execute_trades.py`, `portfolio_monitor.py`, `trade_journal.py`, and `performance_tracker_v2.py`) add a dynamic intraday scanner, mode routing, exposure limits, bracket orders, stop monitoring, a JSONL journal, and benchmark snapshots.

The end-to-end paths are currently:

```text
market APIs -> gather_data / tools API -> personality and technical agents
            -> LLM risk signal -> LLM portfolio manager -> Alpaca integration
            -> portfolio monitor -> JSONL journal -> performance snapshots

Alpaca movers/actives -> scan_market -> day-mode analysis -> execute_trades
                      -> exposure validator -> direct Alpaca REST
```

### Components worth preserving

- The data API/cache layer and existing backtesting framework.
- Pydantic structured-output support in the agent runtime.
- Existing benchmark collection and performance snapshot concepts.
- The AutoResearch experiment log, immutable-baseline idea, syntax checks, and rollback behavior.
- Alpaca paper credentials and order primitives, but only behind a broker provider.
- The frontend/backend as a base for a later read-only swing dashboard.

### Components to modify

- `src/config.py`: one explicit swing style, safe execution defaults, three setup families, model roles, and deterministic thresholds.
- Risk management: technical-stop sizing, aggregate planned risk, daily/weekly entry limits, drawdown and loss-streak halts, instrument/event/freshness/duplicate checks, and immutable stop direction.
- Scanner: daily OHLCV, liquidity, trend, setup, regime, and relative-strength ranking before any LLM call.
- Agent flow: scanner -> technical/fundamental/regime context -> bull and independent bear review -> structured PM proposal -> deterministic risk authority.
- Execution: broker-neutral intents, preflight refresh, persisted idempotency key, dry review, and reconciliation.
- Journal/performance: SQLite lifecycle records rather than a five-day or append-only decision log.
- AutoResearch: swing-only hypotheses, time-series splits, regime stability, complexity/sample penalties, and human-only promotion.

### Components disabled from production swing paths

- Day mode and automatic mode switching.
- `apex`/scalping workflows, EOD day flattening, and intraday regime strategy switching.
- Shorts, covers as entries, margin, options, crypto, futures, leveraged/inverse ETFs, averaging down, and adding to losers.
- Legacy fixed 7% stop generation and LLM-derived position quantity.
- Direct production invocation of the personality swarm. It remains available as research/reference code.

## Target architecture

```text
broad US liquid universe
  -> deterministic daily scanner (10-20 candidates)
  -> deterministic regime + sector/stock relative strength
  -> cost-controlled structured analysts
  -> bull case + independent red team
  -> structured PM decision (BUY/WATCH/HOLD/EXIT/NO_TRADE)
  -> deterministic risk authority
  -> mandatory broker reconciliation + serialized durable admission
  -> GTC paper bracket execution + reconciliation
  -> snapshots and execution events
  -> trade close -> deterministic metrics + postmortem
  -> observations -> hypotheses -> validation -> human approval
```

LLMs may rank evidence, describe technical invalidation, red-team a thesis, classify a postmortem, and propose a hypothesis. They cannot change thresholds, compute the final permitted quantity, bypass a halt, promote a strategy, or submit an order directly.

## New component boundaries

- `src/swing/config.py`: typed environment-backed safety and strategy settings.
- `src/swing/models.py`: enums and schema-validated candidate, proposal, risk, order, postmortem, and broker records.
- `src/swing/database.py`: SQLite schema and repositories with foreign keys, WAL mode, migrations, and permanent audit records.
- `src/swing/market.py`: daily indicator, regime, sector-relative-strength, setup classification, and normalized scoring.
- `src/swing/risk.py`: the final entry authority and stop-amendment invariant.
- `src/swing/brokers/`: `BrokerProvider`, a non-network fake, the Alpaca paper implementation, a human-supervised provider for live Robinhood tickets (`HumanSuppliedBrokerProvider`), and an unused, intentionally nonfunctional automated-Robinhood skeleton (`RobinhoodMCPProvider`) — see docs/ROBINHOOD_MCP.md for why order placement is never automated.
- `src/swing/execution.py`: mandatory pre-submission reconciliation, idempotent review/place, and serialized entry admission.
- `src/swing/reconciliation.py`: broker/database order, position, fill, restart, and automatic-close reconciliation.
- `src/swing/stops.py`: protected stop amendment boundary; long stops can only tighten.
- `src/swing/postmortem.py`: close metrics and one-trade observations; never direct strategy mutation.
- `src/swing/analytics.py`: expectancy-first statistics with sample sizes and benchmark fields.
- `src/autoresearch_swing/`: hypothesis records, time-series validation plans, balanced fitness, and human promotion gate.
- `src/swing/cli.py`: database initialization, risk review, reports, research review, kill switch, and readiness checks.

Legacy imports remain intact where practical. Production documentation and new entry points use only the target swing path.

## Persistent data model

SQLite is the system of record. Core normalized tables are:

- `trades`: immutable initial plan plus fill/close state, R metrics, context, strategy version, and broker identifiers.
- `trade_snapshots`: price, stop, MFE, MAE, and timestamped position state.
- `market_regimes`: deterministic inputs and classification.
- `candidate_scores`: score components, rejection reason, data provenance, and candidate disposition.
- `agent_decisions`: role/model/schema version, structured payload, cost, and validation status.
- `postmortems`: rule adherence, classification, answers, and evidence.
- `lessons`: observation/provisional/validated/rejected/retired state, sample size, supporting trades, and version.
- `hypotheses`: testable proposed rule, source observations, status, and human approval fields.
- `strategy_versions`: production/candidate status, change set, evidence, and approval.
- `backtests`: immutable configuration/data hashes, split metrics, benchmark results, and artifact path.
- `experiments`: experiment protocol and fitness result.
- `rule_violations`: attempted or realized violations with severity and linked intent/trade.
- `execution_events`: intents, reviews, submissions, fills, failures, stop changes, and reconciliation events.
- `entry_admissions`: serialized pending/submitted/unknown outcomes included in activity and open-risk limits.
- `equity_snapshots`, `benchmark_snapshots`, and `model_costs`: drawdown, comparison, and net-of-cost reporting.
- `system_state`: kill switch, drawdown approval latch, and operational metadata.

Foreign keys connect decisions and events to candidates/trades. Unique order-intent IDs make retries idempotent. Database timestamps are UTC ISO-8601.

## Learning pipeline

```text
one closed trade -> postmortem -> OBSERVATION
related observations with adequate sample -> HYPOTHESIS
hypothesis -> train/validation/out-of-sample + walk-forward tests
passing evidence -> candidate strategy report
explicit human approval -> new production strategy version
```

One trade can never modify production rules. A winning trade may still be classified as a mistake and a losing trade may be a `VALID_LOSS`. All segmented metrics carry sample size and remain labeled insufficient below the configured threshold.

## Broker abstraction

`BrokerProvider` owns account, buying power, positions, orders, fresh quotes, order review, placement, cancellation, stop replacement, closing, status, and history. Provider objects never choose trades. Every non-dry submission first runs full reconciliation, then refreshes account/quote/orders/positions immediately before deterministic risk validation. Unknown outcomes latch a global entry halt until reconciliation proves broker truth.

`AlpacaPaperProvider` is paper-only and uses GTC bracket orders for multi-day protection. Stop replacement reads the current broker stop and independently rejects widening. Live mode is disabled by default and requires explicit acknowledgment in addition to `TRADING_ENABLED=true`.

Robinhood's Agentic Trading MCP is a real, verified product (`https://agent.robinhood.com/mcp/trading`, no fabricated names) but has no paper/sandbox mode and is scoped for a human-supervised session rather than a headless credential — see docs/ROBINHOOD_MCP.md. This codebase therefore never automates Robinhood order placement. `HumanSuppliedBrokerProvider` runs the same unchanged risk engine against a human-typed real account snapshot to produce an approved order ticket (`swing-trader live-ticket`); a human executes it, and `swing-trader record-live-fill`/`record-live-exit` journal the result. `RobinhoodMCPProvider` remains an unused, intentionally nonfunctional skeleton — order placement stays permanently disabled on any Robinhood provider regardless of what tool schemas are eventually verified.

## Safety architecture

All uncertainty fails closed. Entry requires:

- long-only permitted instrument; price above $5; adequate volume/liquidity; no leveraged/inverse ETF or configured long-term holding;
- one of the three setup types and score at least 80;
- no ordinary-stock earnings inside five trading days;
- fresh timestamped quote and sufficient settled buying power;
- a technical stop below entry and a realistic target normally at least 2R;
- quantity rounded down from current-equity dollar risk (0.50%, reduced in drawdown; at most 0.75% for explicitly qualified A+ and never over 1%);
- at most three positions, one new position/day, three/week, and 2% combined planned open risk;
- no active 8% drawdown latch, three-loss latch, kill switch, duplicate intent, existing position add, or losing-position add;
- immutable stop discipline: long stops can stay or rise, never fall.

At 5% drawdown normal risk is reduced to the configured 0.30%. At 8% drawdown the database latches a halt and generates a review; clearing it requires a recorded human approval. Three completed losses likewise latch a durable halt and review. Exits and protective risk reduction remain available while new entries are halted. The legacy `portfolio_monitor.py` is disabled and performs no network or order action.

## Known baseline limitations

- Legacy modules still contain day/short code for compatibility, but the adaptive swing entry points do not call them.
- The existing data sources do not guarantee complete exchange breadth, halt status, corporate actions, spread history, or earnings dates. Missing required live data means `NO_TRADE`.
- Historical files bundled with this repository are insufficient for a credible multi-regime swing backtest. Results must not be claimed until a versioned dataset is supplied.
- Robinhood's Agentic Trading MCP is real and connectable (`claude mcp add robinhood-trading --transport http https://agent.robinhood.com/mcp/trading`), but this codebase intentionally never calls its order-placement tools automatically; live execution is always a human-supervised `swing-trader live-ticket` + manual execution, per docs/ROBINHOOD_MCP.md.
