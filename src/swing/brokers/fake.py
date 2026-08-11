"""In-memory broker used by tests; it never touches a network."""

from __future__ import annotations

from datetime import datetime, timezone
from threading import Lock
from uuid import uuid4

from ..models import (
    AccountSnapshot,
    BrokerAsset,
    BrokerOrder,
    OrderIntent,
    Position,
    Quote,
)
from .base import BrokerProvider


class FakeBrokerProvider(BrokerProvider):
    name = "fake-paper"

    def __init__(
        self,
        *,
        equity: float = 2_000,
        cash: float = 2_000,
        positions: list[Position] | None = None,
        quotes: dict[str, Quote] | None = None,
        open_orders: list[dict] | None = None,
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
        self.next_filled_quantity = 0.0
        self.next_average_fill_price: float | None = None
        self.reject_stop_leg = False
        self.reject_target_leg = False
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
        if self.open_orders:
            return [dict(order) for order in self.open_orders]
        rows: list[dict] = []
        for order in self.orders.values():
            if order.status.lower() in {"rejected", "canceled", "cancelled", "expired", "filled"}:
                continue
            root = order.model_dump(mode="json")
            root.update(order.raw)
            root["id"] = order.broker_order_id
            root["symbol"] = order.symbol
            root["legs"] = [dict(leg) for leg in order.raw.get("legs") or []]
            rows.append(root)
        return rows

    def get_asset(self, symbol: str) -> BrokerAsset:
        return self.assets.get(
            symbol.upper(),
            BrokerAsset(
                symbol=symbol.upper(),
                asset_class="us_equity",
                tradable=True,
                status="active",
            ),
        ).model_copy(deep=True)

    def get_quote(self, symbol: str) -> Quote:
        if symbol.upper() not in self.quotes:
            raise KeyError(f"No fake quote for {symbol}")
        return self.quotes[symbol.upper()].model_copy(deep=True)

    def review_order(self, intent: OrderIntent) -> dict:
        return {
            "approved": intent.side == "buy" and intent.stop_price < intent.limit_price < intent.target_price,
            "market_open": True,
            "reviewed_at": datetime.now(timezone.utc).isoformat(),
            "asset_tradable": True,
            "estimated_cost": intent.quantity * intent.limit_price,
            "intent_id": intent.intent_id,
        }

    def place_order(self, intent: OrderIntent) -> BrokerOrder:
        with self._lock:
            self.place_calls += 1
            if self.raise_on_place:
                raise self.raise_on_place
            order = BrokerOrder(
                broker_order_id=str(uuid4()),
                intent_id=intent.intent_id,
                symbol=intent.ticker,
                side=intent.side,
                quantity=intent.quantity,
                status=self.next_status,
                filled_quantity=self.next_filled_quantity,
                average_fill_price=self.next_average_fill_price,
                raw={
                    "client_order_id": intent.intent_id,
                    "order_class": "bracket",
                    "stop_price": intent.stop_price,
                    "limit_price": intent.limit_price,
                    "legs": [
                        {"id": f"stop-{intent.intent_id}", "side": "sell", "type": "stop", "stop_price": intent.stop_price, "status": "rejected" if self.reject_stop_leg else "new"},
                        {"id": f"target-{intent.intent_id}", "side": "sell", "type": "limit", "limit_price": intent.target_price, "status": "rejected" if self.reject_target_leg else "new"},
                    ]
                    if self.next_filled_quantity > 0
                    else [],
                },
            )
            self.orders[order.broker_order_id] = order
            if self.next_filled_quantity > 0:
                fill_price = self.next_average_fill_price or intent.limit_price
                self.positions = [position for position in self.positions if position.symbol.upper() != intent.ticker.upper()]
                self.positions.append(Position(symbol=intent.ticker, quantity=self.next_filled_quantity, average_entry_price=fill_price, current_price=fill_price, market_value=self.next_filled_quantity * fill_price, current_stop=intent.stop_price))
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
        self.positions = [item for item in self.positions if item.symbol != symbol]
        return BrokerOrder(
            broker_order_id=str(uuid4()),
            intent_id=f"close-{uuid4()}",
            symbol=symbol,
            side="sell",
            quantity=abs(position.quantity),
            status="accepted",
            submitted_at=datetime.now(timezone.utc),
        )

    def get_order_status(self, broker_order_id: str) -> BrokerOrder:
        return self.orders[broker_order_id].model_copy(deep=True)

    def get_trade_history(self, since: str | None = None) -> list[dict]:
        if self.history is not None:
            return [dict(order) for order in self.history]
        rows: list[dict] = []
        for order in self.orders.values():
            row = order.model_dump(mode="json")
            row.update(order.raw)
            row["id"] = order.broker_order_id
            row["symbol"] = order.symbol
            row["filled_qty"] = order.filled_quantity
            row["filled_avg_price"] = order.average_fill_price
            rows.append(row)
        return rows
