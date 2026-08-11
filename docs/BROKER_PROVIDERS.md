# Broker Providers

`BrokerProvider` defines account, buying power, positions, open orders, quote, review, placement, cancellation, stop replacement, close, status, and history operations. Risk logic never lives in a provider.

`FakeBrokerProvider` is network-free and used by safety tests. `AlpacaPaperProvider` hard-codes Alpaca's paper trading endpoint, uses current quotes, checks the market clock, and submits a client-order-id GTC bracket only after review. It is the only implemented external provider in the adaptive path.

Before every non-dry submission, `SwingExecutionService` runs the broker reconciler, then refreshes account, buying power, positions, open orders, and quote. SQLite serializes admission across distinct intents and includes unresolved reservations in aggregate limits. A timeout after reservation becomes `UNKNOWN_REQUIRES_RECONCILIATION`, latches a global halt, and forbids retry because the first order may have succeeded.

`ProtectedStopService` is the supported amendment path. It checks the durable stop first; `AlpacaPaperProvider.replace_stop()` then reads the current broker leg and independently refuses a lower stop. Missing broker stop data fails closed.

The former root-level broker scripts and legacy Alpaca integration module were removed. Repository-wide static tests require the Alpaca paper provider to be the only file containing a broker order endpoint. Safety exits use the supported execution/lifecycle/reconciliation boundary; there is no account-wide legacy flatten command.
