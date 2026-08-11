"""Human-supervised broker for live Robinhood execution.

Live trading through Robinhood's Agentic Trading MCP (https://agent.robinhood.com/mcp/trading)
is deliberately never automated by this codebase -- see docs/ROBINHOOD_MCP.md for why
(no paper/sandbox mode exists, and the product is scoped for a supervised agent
session with per-action approval, not a headless credential). Instead:

1. A human reads the current Robinhood Agentic account state (equity, cash,
   positions, a fresh quote) -- from the Robinhood app, or by asking Claude in
   a session with the Robinhood MCP connected -- and writes it into a small
   JSON snapshot (see cli.py's `live-ticket` command for the expected shape).
2. `swing-trader live-ticket <snapshot.json>` runs that snapshot through the
   SAME unchanged risk engine (src/swing/risk.py) used for paper trading and
   prints an approved order ticket (exact quantity, entry, stop, target) -- or
   a rejection reason. It NEVER places an order.
3. The human executes that exact ticket themselves, through a Claude session
   with the Robinhood MCP connected (or the Robinhood app directly).
4. `swing-trader record-live-fill` / `record-live-exit` journal the resulting
   real fill and eventual close, so live trades get the same journaling,
   reconciliation-equivalent bookkeeping, and postmortem treatment as paper
   trades.

HumanSuppliedBrokerProvider implements only the read side of BrokerProvider,
from a static snapshot a human typed in moments ago. Every state-changing
method is permanently disabled -- not a pending stub, a structural guarantee
that this class can never place, cancel, or modify a real order.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from ..models import (
    AccountSnapshot,
    BrokerAsset,
    BrokerOrder,
    OrderIntent,
    Position,
    Quote,
)
from .base import BrokerProvider, BrokerUnavailable


class HumanSuppliedBrokerProvider(BrokerProvider):
    """Read-only view of a real Robinhood Agentic account, from a human-supplied snapshot."""

    name = "robinhood-human-supervised"

    def __init__(self, snapshot: dict[str, Any]):
        account = snapshot["account"]
        if account.get("dedicated_agentic_account") is not True:
            raise ValueError("snapshot['account']['dedicated_agentic_account'] must be explicitly true; this " "framework must never compute sizing against any account other than the dedicated " "Robinhood Agentic account")
        if account.get("is_paper", False):
            raise ValueError("Robinhood has no paper mode; snapshot['account']['is_paper'] must be false")
        self._account = AccountSnapshot(
            account_id=account["account_id"],
            equity=float(account["equity"]),
            cash=float(account["cash"]),
            buying_power=float(account.get("buying_power", account["cash"])),
            is_paper=False,
            is_margin_enabled=bool(account.get("is_margin_enabled", False)),
            dedicated_agentic_account=True,
            retrieved_at=datetime.fromisoformat(account["retrieved_at"]),
        )
        self._positions = [Position(**position) for position in snapshot.get("positions", [])]
        self._open_orders = list(snapshot.get("open_orders", []))
        asset = snapshot.get("asset", {})
        quote = snapshot["quote"]
        market = snapshot.get("market") or {}
        if market.get("is_open") is not True or not market.get("retrieved_at"):
            raise ValueError("snapshot['market'] must explicitly confirm is_open=true with a freshness timestamp")
        self._market_open = True
        self._market_reviewed_at = datetime.fromisoformat(market["retrieved_at"])
        self._asset = BrokerAsset(
            symbol=(asset.get("symbol") or quote["symbol"]).upper(),
            asset_class=asset.get("asset_class", "us_equity"),
            tradable=asset.get("tradable", True),
            status=asset.get("status", "active"),
            marginable=asset.get("marginable"),
            is_leveraged_or_inverse=asset.get("is_leveraged_or_inverse", False),
            retrieved_at=datetime.fromisoformat(asset.get("retrieved_at") or quote["retrieved_at"]),
        )
        self._quote = Quote(
            symbol=quote["symbol"].upper(),
            last=float(quote["last"]),
            bid=float(quote["bid"]) if quote.get("bid") is not None else None,
            ask=float(quote["ask"]) if quote.get("ask") is not None else None,
            source="human-supplied-robinhood",
            retrieved_at=datetime.fromisoformat(quote["retrieved_at"]),
            market_timestamp=datetime.fromisoformat(quote.get("market_timestamp") or quote["retrieved_at"]),
        )
        self._trade_history = list(snapshot.get("trade_history", []))

    def get_account(self) -> AccountSnapshot:
        return self._account.model_copy(deep=True)

    def get_buying_power(self) -> float:
        return self._account.buying_power

    def get_positions(self) -> list[Position]:
        return [position.model_copy(deep=True) for position in self._positions]

    def get_open_orders(self) -> list[dict]:
        return list(self._open_orders)

    def get_asset(self, symbol: str) -> BrokerAsset:
        if symbol.upper() != self._asset.symbol:
            raise BrokerUnavailable(f"Snapshot only covers {self._asset.symbol}, not {symbol}")
        return self._asset.model_copy(deep=True)

    def get_quote(self, symbol: str) -> Quote:
        if symbol.upper() != self._quote.symbol:
            raise BrokerUnavailable(f"Snapshot only covers {self._quote.symbol}, not {symbol}")
        return self._quote.model_copy(deep=True)

    def get_trade_history(self, since: str | None = None) -> list[dict]:
        return list(self._trade_history)

    def get_order_status(self, broker_order_id: str) -> BrokerOrder:
        raise BrokerUnavailable("Order status must be checked in Robinhood directly; this provider is snapshot-only")

    def review_order(self, intent: OrderIntent) -> dict:
        # Purely informational -- a human, not this code, decides whether to execute.
        return {
            "approved": intent.side == "buy" and intent.stop_price < intent.limit_price < intent.target_price,
            "market_open": self._market_open,
            "reviewed_at": self._market_reviewed_at.isoformat(),
            "estimated_cost": intent.quantity * intent.limit_price,
            "note": "This is a computed ticket only. No order was reviewed by or sent to Robinhood.",
        }

    def _blocked(self, *_args, **_kwargs):
        raise BrokerUnavailable("Live order placement is intentionally never automated by this framework. Execute the " "printed ticket yourself through a Claude session with the Robinhood MCP connected, then " "journal the real fill with `swing-trader record-live-fill`.")

    place_order = _blocked
    cancel_order = _blocked
    replace_stop = _blocked
    close_position = _blocked
