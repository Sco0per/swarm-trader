# Paper-to-Live Checklist

Do not enable real money until all items are independently verified:

- Credible multi-regime historical and out-of-sample results are recorded with data/config hashes.
- Alpaca/equivalent paper operation has zero major risk failures, duplicates, unauthorized instruments, and stop widening.
- At least the first 20 paper trades prove behavioral compliance; 21–50 test edge and benchmark-relative results; 51–100 test learning improvement.
- Positive expectancy, preferably profit factor above 1.5, drawdown below 8%, and positive net result after model/data costs are supported by adequate samples.
- Quotes, earnings/events, halts, spread, broker state, partial fills, and reconciliation are reliable.
- GTC bracket child legs survive overnight; partial take-profit fills resize protection correctly; cancel/replace and corporate-action behavior are observed and recorded.
- Unknown outcomes and untracked broker state demonstrably halt entries, then recover correctly after a process restart and successful reconciliation.
- The broker/account is connected, dedicated account identity is verified, long-term holdings are unreachable, and the do-not-trade list is reviewed.
- Emergency procedures are rehearsed and reports/journal/postmortems are complete.
- A human approves the production strategy version and capital amount.

Only then set `EXECUTION_MODE=live`, `TRADING_ENABLED=true`, and `LIVE_TRADING_ACK=I_ACKNOWLEDGE_LIVE_RISK`. Those switches do not waive any deterministic check.
