"""Broker-neutral interface. Providers execute; they never make decisions."""

from __future__ import annotations

from abc import ABC, abstractmethod

from ..models import (
    AccountSnapshot,
    BrokerAsset,
    BrokerOrder,
    OrderIntent,
    Position,
    Quote,
)


class BrokerUnavailable(RuntimeError):
    pass


class BrokerProvider(ABC):
    name: str

    @abstractmethod
    def get_account(self) -> AccountSnapshot:
        ...

    @abstractmethod
    def get_buying_power(self) -> float:
        ...

    @abstractmethod
    def get_positions(self) -> list[Position]:
        ...

    @abstractmethod
    def get_open_orders(self) -> list[dict]:
        ...

    @abstractmethod
    def get_asset(self, symbol: str) -> BrokerAsset:
        ...

    @abstractmethod
    def get_quote(self, symbol: str) -> Quote:
        ...

    @abstractmethod
    def review_order(self, intent: OrderIntent) -> dict:
        ...

    @abstractmethod
    def place_order(self, intent: OrderIntent) -> BrokerOrder:
        ...

    @abstractmethod
    def cancel_order(self, broker_order_id: str) -> bool:
        ...

    @abstractmethod
    def replace_stop(self, broker_order_id: str, new_stop: float) -> BrokerOrder:
        """Tighten a long protective stop; implementations must reject widening."""
        ...

    @abstractmethod
    def close_position(self, symbol: str) -> BrokerOrder:
        ...

    @abstractmethod
    def get_order_status(self, broker_order_id: str) -> BrokerOrder:
        ...

    @abstractmethod
    def get_trade_history(self, since: str | None = None) -> list[dict]:
        ...
