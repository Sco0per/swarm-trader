# Swing AutoResearch

`src/autoresearch_swing` accepts testable hypotheses derived from multiple durable observations. It does not edit or deploy the production strategy.

Validation must declare immutable data/config hashes and chronological train, validation, and out-of-sample periods. Walk-forward results and at least two profitable regime slices are required. The balanced fitness combines expectancy, profit factor, Sharpe, Sortino, drawdown, return, and regime stability, while penalizing low sample size, excessive turnover, complexity, performance concentration, and an 8% drawdown breach.

Each comparison is stored in `backtests` and generates `reports/strategy_candidates/<hypothesis_id>.md`. An attractive result only becomes a candidate for human review. Use `poetry run swing-trader approve-strategy <id> --approved-by <human>` after independent review; the database refuses approval without adequate evidence.

The repository has no bundled multi-regime daily dataset sufficient for a credible new swing result, so no performance result is claimed by this implementation.
