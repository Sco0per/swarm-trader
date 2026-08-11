# Emergency Procedures

Immediately stop new entries:

```bash
poetry run swing-trader kill-switch on --approved-by <operator>
poetry run swing-trader status
```

Inspect broker positions and orders independently. Cancel unintended open entries, preserve or tighten protective stops, and exit only when responsible. Never widen a stop or retry an uncertain intent. For a broker timeout, reconcile the persisted intent/client order ID against broker order history before any manual action.

At an 8% drawdown, new entries latch off and a dated drawdown review is generated. Investigate data, fills, rule violations, setup/regime statistics, benchmark performance, and model behavior. After documented human review only:

```bash
poetry run swing-trader clear-drawdown-halt --approved-by <operator>
```

Keep the kill switch on if uncertainty remains. Rotate any exposed credential outside the repository and preserve the database/reports for audit.
