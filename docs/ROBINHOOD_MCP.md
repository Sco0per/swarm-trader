# Robinhood Agentic Trading — Status

## What's confirmed

Robinhood's Agentic Trading MCP is a real, public product (launched 2026-05-27), verified directly against Robinhood's own documentation, not a third-party guess:

- **Connection**: `claude mcp add robinhood-trading --transport http https://agent.robinhood.com/mcp/trading`, then `/mcp` inside Claude Code to authenticate (desktop browser required). Use **local** scope (the default), not project scope — this ties to a personal brokerage account and must never be committed to version control.
- **Account model**: requires a primary Robinhood investing account in good standing; the first connection auto-opens onboarding to create and fund a **dedicated Agentic account**, separate from the user's main portfolio. "Your agent can only place trades in your Robinhood Agentic account" — Robinhood enforces this on their end.
- **No sandbox or paper mode.** Every connected agent trades real money in the dedicated account. There is no Robinhood-side way to dry-run an order.
- **Permission model is per-session, not a stored credential.** Robinhood's own docs describe a live agent session choosing "allow all," "ask every time," or "deny" for each action — this is scoped for a human-supervised interactive agent, not a headless script holding an API key.

## Why this framework never automates Robinhood order placement

Two of the findings above are decisive:

1. No paper/sandbox mode exists, so there is no safe way to test an automated integration against Robinhood before it's live.
2. The product's own permission model is built around a human watching a live agent session approve or deny each action — not a stored credential a cron job can use unattended.

Building a headless Python client that authenticates once and then calls Robinhood programmatically from `swing-trader run` would fight both of these properties, and would remove the human checkpoint the project's own design requires before any live order. So this framework does not do that.

## What this framework does instead: human-supervised live tickets

1. A human reads the current Robinhood Agentic account state — equity, cash, open positions, a fresh quote — either from the Robinhood app or by asking Claude in a session with the Robinhood MCP connected, and writes it into a small JSON snapshot (see `swing-trader live-ticket --help`).
2. `swing-trader live-ticket <snapshot.json>` runs that snapshot through the **exact same, unchanged** deterministic risk engine (`src/swing/risk.py`) that governs paper trading — same 0.5%/0.75%/1% risk tiers, same position/day/week limits, same drawdown and loss-streak halts — and prints either a rejection reason or an approved order ticket (exact quantity, entry, stop, target, planned dollar risk). It never calls a broker's order-placement method; `HumanSuppliedBrokerProvider` (`src/swing/brokers/human_supervised.py`) structurally cannot place, cancel, or modify an order — those methods always raise, by design, not as a pending stub.
3. The human executes that exact ticket themselves — through a Claude session with the Robinhood MCP connected, or the Robinhood app directly.
4. `swing-trader record-live-fill` and `swing-trader record-live-exit` journal the real fill and eventual close, so live trades get the same durable journaling, R/MFE/MAE tracking, and postmortem treatment as paper trades.

This requires `EXECUTION_MODE=live`, `TRADING_ENABLED=true`, and `LIVE_TRADING_ACK=I_ACKNOWLEDGE_LIVE_RISK` — the same three flags that already gated every other live-execution path in this codebase.

## What's still open

- `RobinhoodMCPProvider` (`src/swing/brokers/robinhood_mcp.py`) remains an intentionally non-executing skeleton. It is not used by any command today. If a future need arises for the *read* side to be automated (e.g. pulling account state programmatically instead of a human typing it into a snapshot), it would require inspecting Robinhood's actual MCP tool schemas from an authenticated session and mapping only verified tool names — never guessed ones. Order-placement methods on any Robinhood provider should stay permanently disabled regardless, per the reasoning above.
- No automated reconciliation exists against Robinhood (unlike the Alpaca paper path's `swing-trader reconcile`) — `record-live-fill`/`record-live-exit` are honest, manual, human-attested journaling, not broker-verified truth. Treat the journal's live rows as what a human reported, not as independently confirmed.
- Long-term/main-portfolio protection depends on `HumanSuppliedBrokerProvider` refusing to construct unless the snapshot explicitly asserts `dedicated_agentic_account: true` — this is a code-level guard, but the real backstop is Robinhood's own account isolation described above.

Live Robinhood execution through this framework means: the framework computes what a human-approved-risk trade should look like; a human, not this code, ever moves real money.
