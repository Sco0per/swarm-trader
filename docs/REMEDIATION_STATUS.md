# Audit Remediation Status

Updated: 2026-08-09

## Completed in code

- Disabled the unsafe legacy percentage-stop monitor. Non-positive thresholds cannot trigger, and `run_monitor()` performs no network or order action.
- Changed Alpaca swing bracket orders from DAY to GTC.
- Added a protected stop service. It validates against the durable stop, and the Alpaca provider independently reads the broker's current stop before refusing any widening.
- Added serialized SQLite entry admissions. Pending and unknown outcomes count toward daily, weekly, position, and combined-open-risk limits.
- Added mandatory reconciliation before every non-dry submission. Unknown broker outcomes latch `reconciliation_halt`; a successful reconciliation is the only automatic clear path.
- Added restart recovery, position/order/fill matching, snapshot persistence, broker-exit detection, and automatic close postmortems.
- Added a durable three-loss halt and human-clear workflow.
- Made safety settings non-weakenable through environment/config overrides.
- Rejected future quote, market, candidate, and account timestamps.
- Repaired the obsolete API rate-limit test target so the full suite collects and runs.

## Verified locally

```text
tests/swing: 55 passed
full repository: 99 passed
Pyright: 0 errors, 0 warnings
```

Regression coverage includes the zero-stop defect, A+ sizing and immutable ceilings, Alpaca GTC payloads, provider/service stop widening, simultaneous admissions, unknown outcomes, restart recovery, automatic postmortems, future timestamps, durable loss halts, and scanner filtering/scoring.

## Still blocked before paper activation

- No Alpaca paper credentials or dedicated paper account were available for real integration QA.
- Bracket child-leg creation, overnight persistence, partial fills, cancel/replace behavior, and corporate actions still require broker-side observation. Alpaca GTC orders use DNR/DNC behavior, so corporate-action handling must be explicitly tested.
- The production universe/market/earnings/halt feed and concrete structured-model backend are not configured end to end.
- `config/do_not_trade.yaml` must be populated from the operator's long-term holdings and substantially identical securities.
- AutoResearch remains an experiment-recording/validation shell rather than a self-verifying full backtest engine.
- Robinhood's Agentic Trading MCP is real and connectable, but automated order placement through it is permanently disabled by design (see docs/ROBINHOOD_MCP.md); live execution is a human-supervised `swing-trader live-ticket` workflow, never an unattended one.

Until these external and integration gates are complete, use dry-run review only and leave `TRADING_ENABLED=false`.

## Operator commands

```bash
poetry run pytest
poetry run swing-trader status
poetry run swing-trader reconcile
poetry run swing-trader paper-review intent.json
```

After a reviewed halt, use `clear-drawdown-halt` or `clear-loss-streak-halt` with `--approved-by`. Never clear `reconciliation_halt` manually; run a successful reconciliation instead.
