"""In-memory broker used by tests; it never touches a network."""

from __future__ import annotations

from datetime import datetime, timezone
from threading import Lock
from uuid import uuid4

from ..models import AccountSnapshot, BrokerAsset, BrokerOrder, OrderIntent, Position, Quote
from .base import BrokerProvider


class FakeBrokerProvider(BrokerProvider):
    name = "fake-paper"

    def __init__(
        self, *, equity: float = 2_000, cash: float = 2_000, positions: list[Position] | None = None,
        quotes: dict[str, Quote] | None = None, open_orders: list[dict] | None = None,
        assets: dict[str, BrokerAsset] | None = None,
    ):
        self.account = AccountSnapshot(account_id="fake-agentic-paper", equity=equity, cash=cash, buying_power=cash, is_paper=True)
        self.positions = positions or []
        self.quotes = quotes or {}
        self.assets = assets or {}
        self.orders: dict[str, BrokerOrder] = {}
        self.open_orders = open_orders or []
        self.place_calls = 0
        self.next_status = "accepted"
        self.raise_on_place: Exception | None = None
        self.history: list[dict] | None = None
        self._lock = Lock()

    def get_account(self) -> AccountSnapshot:
        return self.account.model_copy(deep=True)

    def get_buying_power(self) -> float:
        return self.account.buying_power

    def get_positions(self) -> list[Position]:
        return [position.model_copy(deep=True) for position in self.positions]

    def get_open_orders(self) -> list[dict]:
        return [dict(order) for order in self.open_orders]

    def get_asset(self, symbol: str) -> BrokerAsset:
        return self.assets.get(symbol.upper(), BrokerAsset(
            symbol=symbol.upper(), asset_class="us_equity", tradable=True, status="active",
        )).model_copy(deep=True)

    def get_quote(self, symbol: str) -> Quote:
        if symbol.upper() not in self.quotes:
            raise KeyError(f"No fake quote for {symbol}")
        return self.quotes[symbol.upper()].model_copy(deep=True)

    def review_order(self, intent: OrderIntent) -> dict:
        return {
            "approved": intent.side == "buy" and intent.stop_price < intent.limit_price < intent.target_price,
            "estimated_cost": intent.quantity * intent.limit_price,
            "intent_id": intent.intent_id,
        }

    def place_order(self, intent: OrderIntent) -> BrokerOrder:
        with self._lock:
            self.place_calls += 1
            if self.raise_on_place:
                raise self.raise_on_place
            order = BrokerOrder(
                broker_order_id=str(uuid4()), intent_id=intent.intent_id, symbol=intent.ticker, side=intent.side,
                quantity=intent.quantity, status=self.next_status,
                raw={"client_order_id": intent.intent_id, "stop_price": intent.stop_price, "limit_price": intent.limit_price},
            )
            self.orders[order.broker_order_id] = order
        return order

    def cancel_order(self, broker_order_id: str) -> bool:
        order = self.orders.get(broker_order_id)
        if not order:
            return False
        self.orders[broker_order_id] = order.model_copy(update={"status": "canceled"})
        return True

    def replace_stop(self, broker_order_id: str, new_stop: float) -> BrokerOrder:
        if new_stop <= 0:
            raise ValueError("Stop prices must be positive")
        raw_current_stop = self.orders[broker_order_id].raw.get("stop_price")
        if raw_current_stop in (None, ""):
            raise ValueError("Broker order has no current protective stop")
        current_stop = float(raw_current_stop)
        if new_stop < current_stop:
            raise ValueError(f"Refusing to widen a long stop from {current_stop:.2f} to {new_stop:.2f}")
        order = self.orders[broker_order_id].model_copy(update={"raw": {"stop_price": new_stop}})
        self.orders[broker_order_id] = order
        return order

    def close_position(self, symbol: str) -> BrokerOrder:
        position = next(position for position in self.positions if position.symbol == symbol)
        return BrokerOrder(
            broker_order_id=str(uuid4()), intent_id=f"close-{uuid4()}", symbol=symbol, side="sell",
            quantity=abs(position.quantity), status="accepted", submitted_at=datetime.now(timezone.utc),
        )

    def get_order_status(self, broker_order_id: str) -> BrokerOrder:
        return self.orders[broker_order_id].model_copy(deep=True)

    def get_trade_history(self, since: str | None = None) -> list[dict]:
        if self.history is not None:
            return [dict(order) for order in self.history]
        return [order.model_dump(mode="json") for order in self.orders.values()]
