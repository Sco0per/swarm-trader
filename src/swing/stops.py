"""The sole supported boundary for protective-stop amendments."""

from __future__ import annotations

from .brokers.base import BrokerProvider
from .database import SwingDatabase
from .models import BrokerOrder
from .risk import SwingRiskManager


class ProtectedStopService:
    """Persist and audit a stop tightening after deterministic validation."""

    def __init__(self, database: SwingDatabase, broker: BrokerProvider, risk_manager: SwingRiskManager):
        self.database = database
        self.broker = broker
        self.risk_manager = risk_manager

    def tighten(self, trade_id: str, broker_stop_order_id: str, new_stop: float) -> BrokerOrder:
        trade = self.database.get_trade(trade_id)
        if trade is None:
            raise KeyError(trade_id)
        if trade["status"] not in {"SUBMITTED", "OPEN", "PARTIALLY_FILLED"}:
            raise ValueError("Protective stops can only be changed for an active trade")
        current_stop = float(trade["final_stop"])
        decision = self.risk_manager.validate_stop_change(trade["ticker"], current_stop, new_stop)
        if not decision.approved:
            raise ValueError(decision.reason)
        order = self.broker.replace_stop(broker_stop_order_id, new_stop)
        self.database.update_trade(trade_id, final_stop=new_stop)
        self.database.record_execution_event(
            intent_id=trade.get("intent_id") or f"stop-{trade_id}",
            decision_id=trade.get("decision_id"),
            trade_id=trade_id,
            broker_order_id=broker_stop_order_id,
            ticker=trade["ticker"],
            side="sell",
            quantity=float(trade["shares"]),
            event_type="STOP_TIGHTENED",
            status=order.status,
            payload={"previous_stop": current_stop, "new_stop": new_stop, "broker": order.model_dump(mode="json")},
        )
        return order
