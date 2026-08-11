# Learning Engine

Durable learning follows one irreversible sequence:

```text
closed trade -> postmortem -> OBSERVATION -> multi-trade HYPOTHESIS
-> chronological validation and walk-forward test -> candidate report
-> explicit human approval -> production strategy version
```

Every close stores realized P&L/R, MFE/MAE and their R values, holding time, exit reason, and a postmortem. Losses use an explicit classification; a rule-following loss can be `VALID_LOSS`. A winner is not assumed valid.

A single trade produces only a low-confidence `OBSERVATION`. At least two related lessons and five supporting samples are needed to create a hypothesis. A lesson cannot jump from observation directly to validated. Production approval requires at least 30 supporting samples, an accepted out-of-sample backtest, a candidate strategy version, and a named human approver.

Analytics prioritize expectancy, profit factor, average win/loss R, drawdown, Sharpe/Sortino where sample length permits, and exact-period SPY/QQQ comparisons. Every segment reports sample size and marks samples below 30 exploratory.
