"""Alpaca paper-only provider for integration QA."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import requests

from ..models import AccountSnapshot, BrokerAsset, BrokerOrder, OrderIntent, Position, Quote
from .base import BrokerProvider, BrokerUnavailable


class AlpacaPaperProvider(BrokerProvider):
    name = "alpaca-paper"
    TRADING_BASE = "https://paper-api.alpaca.markets/v2"
    DATA_BASE = "https://data.alpaca.markets/v2"

    def __init__(self, api_key: str, api_secret: str, timeout: float = 10):
        if not api_key or not api_secret:
            raise ValueError("Alpaca paper credentials are required")
        self.headers = {"APCA-API-KEY-ID": api_key, "APCA-API-SECRET-KEY": api_secret}
        self.timeout = timeout

    def _request(self, method: str, url: str, **kwargs) -> Any:
        try:
            response = requests.request(method, url, headers=self.headers, timeout=self.timeout, **kwargs)
            response.raise_for_status()
            return response.json() if response.content else {}
        except requests.RequestException as exc:
            raise BrokerUnavailable(str(exc)) from exc

    def get_account(self) -> AccountSnapshot:
        raw = self._request("GET", f"{self.TRADING_BASE}/account")
        return AccountSnapshot(
            account_id=str(raw["id"]), equity=float(raw["equity"]), cash=max(0, float(raw["cash"])),
            buying_power=max(0, float(raw["buying_power"])), is_paper=True,
            is_margin_enabled=float(raw.get("multiplier", 1) or 1) > 1, dedicated_agentic_account=None,
        )

    def get_buying_power(self) -> float:
        return self.get_account().buying_power

    def get_positions(self) -> list[Position]:
        rows = self._request("GET", f"{self.TRADING_BASE}/positions")
        return [Position(
            symbol=row["symbol"], quantity=float(row["qty"]), average_entry_price=float(row["avg_entry_price"]),
            current_price=float(row["current_price"]), market_value=float(row["market_value"]), side=row.get("side", "long"),
        ) for row in rows]

    def get_open_orders(self) -> list[dict]:
        rows = self._request("GET", f"{self.TRADING_BASE}/orders", params={"status": "open", "nested": "true"})
        return list(rows)

    def get_asset(self, symbol: str) -> BrokerAsset:
        raw = self._request("GET", f"{self.TRADING_BASE}/assets/{symbol}")
        attributes = {str(value).lower() for value in raw.get("attributes") or []}
        explicit_leverage = None
        if attributes.intersection({"leveraged", "inverse", "volatility_linked"}):
            explicit_leverage = True
        return BrokerAsset(
            symbol=str(raw["symbol"]).upper(),
            asset_class=str(raw.get("class", "unknown")).lower(),
            tradable=bool(raw.get("tradable", False)),
            status=str(raw.get("status", "unknown")).lower(),
            marginable=bool(raw["marginable"]) if "marginable" in raw else None,
            is_leveraged_or_inverse=explicit_leverage,
            raw=raw,
        )

    def get_quote(self, symbol: str) -> Quote:
        raw = self._request("GET", f"{self.DATA_BASE}/stocks/{symbol}/quotes/latest")
        quote = raw.get("quote", raw)
        timestamp = datetime.fromisoformat(str(quote["t"]).replace("Z", "+00:00"))
        return Quote(
            symbol=symbol, bid=float(quote["bp"]), ask=float(quote["ap"]),
            last=(float(quote["bp"]) + float(quote["ap"])) / 2,
            source="alpaca", retrieved_at=datetime.now(timezone.utc), market_timestamp=timestamp,
        )

    def review_order(self, intent: OrderIntent) -> dict:
        if intent.side != "buy" or not (intent.stop_price < intent.limit_price < intent.target_price):
            return {"approved": False, "reason": "invalid long bracket geometry"}
        clock = self._request("GET", f"{self.TRADING_BASE}/clock")
        if not bool(clock.get("is_open", False)):
            return {"approved": False, "reason": "market is closed", "clock": clock}
        return {"approved": True, "estimated_cost": intent.quantity * intent.limit_price, "paper_only": True}

    def place_order(self, intent: OrderIntent) -> BrokerOrder:
        payload = {
            "symbol": intent.ticker, "qty": str(intent.quantity), "side": "buy", "type": "limit",
            "limit_price": str(round(intent.limit_price, 2)), "time_in_force": "gtc", "order_class": "bracket",
            "take_profit": {"limit_price": str(round(intent.target_price, 2))},
            "stop_loss": {"stop_price": str(round(intent.stop_price, 2))},
            "client_order_id": intent.intent_id[:48],
        }
        raw = self._request("POST", f"{self.TRADING_BASE}/orders", json=payload)
        return self._order(raw, intent.intent_id)

    def _order(self, raw: dict, intent_id: str) -> BrokerOrder:
        submitted = raw.get("submitted_at")
        return BrokerOrder(
            broker_order_id=str(raw["id"]), intent_id=intent_id, symbol=raw["symbol"], side=raw["side"],
            quantity=float(raw.get("qty") or 0), status=raw["status"], filled_quantity=float(raw.get("filled_qty") or 0),
            average_fill_price=float(raw["filled_avg_price"]) if raw.get("filled_avg_price") else None,
            submitted_at=datetime.fromisoformat(submitted.replace("Z", "+00:00")) if submitted else datetime.now(timezone.utc), raw=raw,
        )

    def cancel_order(self, broker_order_id: str) -> bool:
        self._request("DELETE", f"{self.TRADING_BASE}/orders/{broker_order_id}")
        return True

    def replace_stop(self, broker_order_id: str, new_stop: float) -> BrokerOrder:
        if new_stop <= 0:
            raise ValueError("Stop prices must be positive")
        current = self.get_order_status(broker_order_id)
        raw_current_stop = current.raw.get("stop_price")
        if raw_current_stop in (None, ""):
            raise BrokerUnavailable("Broker did not return the current protective stop; refusing replacement")
        current_stop = float(raw_current_stop)
        if new_stop < current_stop:
            raise ValueError(f"Refusing to widen a long stop from {current_stop:.2f} to {new_stop:.2f}")
        raw = self._request("PATCH", f"{self.TRADING_BASE}/orders/{broker_order_id}", json={"stop_price": str(round(new_stop, 2))})
        return self._order(raw, str(raw.get("client_order_id", "unknown")))

    def close_position(self, symbol: str) -> BrokerOrder:
        raw = self._request("DELETE", f"{self.TRADING_BASE}/positions/{symbol}")
        return self._order(raw, f"close-{symbol}-{int(datetime.now(timezone.utc).timestamp())}")

    def get_order_status(self, broker_order_id: str) -> BrokerOrder:
        raw = self._request("GET", f"{self.TRADING_BASE}/orders/{broker_order_id}")
        return self._order(raw, str(raw.get("client_order_id", "unknown")))

    def get_trade_history(self, since: str | None = None) -> list[dict]:
        params = {"status": "all", "nested": "true", "limit": 500}
        if since:
            params["after"] = since
        return list(self._request("GET", f"{self.TRADING_BASE}/orders", params=params))
