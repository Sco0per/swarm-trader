# Risk Model

Current broker equity, not a fixed account constant, drives all sizing. For a validated long entry:

```text
risk_per_share = fresh_quote - deterministic_structural_stop
allowed_risk = equity × applied_risk_percent
risk_quantity = floor(allowed_risk / risk_per_share)
```

Baseline risk is 0.75%. Models and operator intent files have no risk-override field. Regime and drawdown controls may only reduce that baseline; the defense-in-depth absolute maximum is immutable at 1.00%. At 5% portfolio drawdown applied risk falls to 0.30%. A 4% weekly drawdown, 8% portfolio drawdown, or three consecutive losses latches a halt requiring human review.

The whole-share risk quantity is reduced, never increased, by `min(cash, buying_power)`, 35% position value, 2.00% combined open risk, 1.50% sector risk, 1.00% cluster risk, and 0.10% of ADV. The system permits three open positions, one new position per day, and three per week. Quantity below one whole share rejects.

Entry validation also requires a deterministic valid setup, score at least 80, a structural stop 0.75–3.00 ATR below the fresh quote, at least 2.0R, price at least $5, at least one million shares and $20 million average daily volume, spread no wider than 0.50%, chase no greater than 0.25R, explicit broker tradability/restriction/halt state, fresh quote/asset/clock/bar/event data, earnings more than five trading sessions away for stocks, and explicit broader-event clearance. There is no lower-R exception.

Unsupported instruments, non-long decisions, blacklisted holdings, duplicate intents, existing/pending same-symbol positions, averaging down, missing cluster mapping, hostile regimes, and stale or future-dated data reject. A stop may remain unchanged or rise; it can never move down.

Every paper submission requires clean broker reconciliation. Unknown outcomes, untracked positions/orders, missing protection, and unverifiable closes latch new entries off. Final admission is serialized in the database and rechecks daily/weekly counts plus portfolio, sector, and cluster risk.
