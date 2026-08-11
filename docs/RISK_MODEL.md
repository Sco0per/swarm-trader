# Risk Model

The account's current broker equity—not a fixed $2,000 constant—drives all sizing. The technical invalidation is selected first. For a long entry:

```text
risk_per_share = live_entry_quote - technical_stop
allowed_dollar_risk = current_equity * applied_risk_pct
quantity = floor(allowed_dollar_risk / risk_per_share)
```

Normal risk is 0.50%. An explicitly qualified A+ candidate (score and confidence at least 90 in a bull/strong-bull regime) may request up to 0.75%. The absolute hard maximum is 1.00%. Configuration may make these and the aggregate limits stricter, but cannot weaken them. At 5% drawdown normal risk falls to 0.30%. At 8%, the new-entry halt latches in SQLite and requires a human-recorded review to clear. Three consecutive completed losses also latch a review halt; a later winner cannot implicitly clear it.

The calculated quantity is only reduced by cash, buying power, 35% maximum exposure, and portfolio limits; it is never increased to make a share fit. Combined planned open risk is at most 2% of equity. The system permits three positions, one new position per day, and three per week. Zero trades is valid.

Entry validation also requires a score of 80, fresh bid/ask data, price above $5, average daily volume above 1,000,000, acceptable spread, no halt, known earnings distance beyond five trading days for stocks, an approved setup, and normally at least 2R structural reward. The 1.5–2R exception requires previously validated exact-context evidence.

Leveraged/inverse/volatility ETFs, shorts, margin, blacklisted holdings, duplicate intents, adds, averaging down, chasing beyond 0.25R, and unsuitable regimes are rejected. Future timestamps are rejected as well as stale ones. A long stop may remain unchanged or rise; it can never move down. The durable stop service and broker provider both enforce that invariant. Exits and protective risk reduction remain possible during a halt.

Every non-dry entry requires a clean broker reconciliation. Unknown outcomes, untracked positions, untracked orders, and unverifiable closures latch an entry halt. Final admission occurs inside a serialized SQLite write transaction so concurrent decisions cannot independently pass stale daily, weekly, position, or combined-risk counts.
