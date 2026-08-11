# Broker Providers

`BrokerProvider` defines account, buying power, positions, open orders, quote, review, placement, cancellation, stop replacement, close, status, and history operations. Risk logic never lives in a provider.

`FakeBrokerProvider` is network-free and used by safety tests. `AlpacaPaperProvider` hard-codes Alpaca's paper trading endpoint, uses current quotes, checks the market clock, and submits a client-order-id GTC bracket only after review. It is the only implemented external provider in the adaptive path.

Before every non-dry submission, `SwingExecutionService` runs the broker reconciler, then refreshes account, buying power, positions, open orders, and quote. SQLite serializes admission across distinct intents and includes unresolved reservations in aggregate limits. A timeout after reservation becomes `UNKNOWN_REQUIRES_RECONCILIATION`, latches a global halt, and forbids retry because the first order may have succeeded.

`ProtectedStopService` is the supported amendment path. It checks the durable stop first; `AlpacaPaperProvider.replace_stop()` then reads the current broker leg and independently refuses a lower stop. Missing broker stop data fails closed.

The legacy percentage monitor is disabled and makes no network calls. Legacy scripts that call Alpaca directly are execution-disabled for new entries. Emergency flatten/exit code remains available for responsible risk reduction.
