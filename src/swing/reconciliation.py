"""Mandatory broker/database reconciliation and automatic closure review."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field

from .agents import StructuredModelBackend
from .brokers.base import BrokerProvider
from .config import SwingSettings
from .database import SwingDatabase
from .postmortem import PostmortemEngine


ACTIVE_ORDER_STATUSES = {"accepted", "new", "pending_new", "partially_filled", "held", "open"}
TERMINAL_EMPTY_STATUSES = {"canceled", "cancelled", "expired", "rejected", "stopped", "suspended"}


class ReconciliationResult(BaseModel):
    reconciled: bool
    recovered_admissions: int = 0
    updated_trades: int = 0
    closed_trades: int = 0
    discrepancies: list[str] = Field(default_factory=list)


def _flatten_orders(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    flattened: list[dict[str, Any]] = []

    def visit(row: dict[str, Any], parent_id: str | None = None) -> None:
        current = dict(row)
        if parent_id and not current.get("parent_order_id"):
            current["parent_order_id"] = parent_id
        flattened.append(current)
        for leg in row.get("legs") or []:
            if isinstance(leg, dict):
                visit(leg, str(row.get("id") or row.get("broker_order_id") or parent_id or ""))

    for item in rows:
        if isinstance(item, dict):
            visit(item)
    return flattened


def _value(row: dict[str, Any], *names: str, default: Any = None) -> Any:
    for name in names:
        value = row.get(name)
        if value not in (None, ""):
            return value
    return default


class BrokerReconciler:
    """Fail closed unless every active local/broker record has a known outcome."""

    def __init__(
        self, settings: SwingSettings, database: SwingDatabase, broker: BrokerProvider,
        *, postmortem_backend: StructuredModelBackend | None = None,
    ):
        self.settings = settings
        self.database = database
        self.broker = broker
        self.postmortem_backend = postmortem_backend
        self.postmortems = PostmortemEngine(
            database,
            consecutive_loss_halt=settings.consecutive_loss_halt,
        )

    def reconcile(self) -> ReconciliationResult:
        discrepancies: list[str] = []
        recovered = updated = closed = 0
        try:
            account = self.broker.get_account()
            positions = self.broker.get_positions()
            open_orders = self.broker.get_open_orders()
            history = _flatten_orders(self.broker.get_trade_history())
        except Exception as exc:
            discrepancies.append(f"Broker snapshot failed: {exc}")
            return self._finish(recovered, updated, closed, discrepancies)

        positions_by_symbol = {}
        for position in positions:
            if position.quantity == 0:
                continue
            if position.quantity < 0 or position.side.lower() != "long":
                discrepancies.append(f"Unsupported broker position side: {position.symbol} {position.side}")
                continue
            positions_by_symbol[position.symbol.upper()] = position
        history_by_id = {
            str(_value(row, "id", "broker_order_id")): row
            for row in history
            if _value(row, "id", "broker_order_id") is not None
        }

        for admission in self.database.active_admissions():
            if admission.get("trade_id"):
                continue
            intent_id = str(admission["intent_id"])
            broker_order_id = admission.get("broker_order_id")
            match = next(
                (
                    row for row in history
                    if str(_value(row, "client_order_id", "intent_id", default="")) in {intent_id, intent_id[:48]}
                    or (broker_order_id and str(_value(row, "id", "broker_order_id", default="")) == str(broker_order_id))
                ),
                None,
            )
            if match is None:
                discrepancies.append(f"Unknown outcome for intent {intent_id}")
                continue
            status = str(_value(match, "status", default="unknown")).lower()
            filled_qty = float(_value(match, "filled_qty", "filled_quantity", default=0) or 0)
            if status in TERMINAL_EMPTY_STATUSES and filled_qty <= 0:
                self.database.update_admission(intent_id, "CANCELED" if status != "rejected" else "REJECTED")
                recovered += 1
                continue
            try:
                payload = json.loads(admission["payload_json"])
                trade = dict(payload["trade"])
                order_id = str(_value(match, "id", "broker_order_id"))
                trade["broker_order_id"] = order_id
                trade["status"] = "PARTIALLY_FILLED" if status == "partially_filled" else "SUBMITTED"
                trade["shares"] = filled_qty
                fill_price = _value(match, "filled_avg_price", "average_fill_price")
                if fill_price is not None:
                    trade["entry_price"] = float(fill_price)
                    trade["position_value"] = float(fill_price) * float(trade["shares"])
                submitted = _value(match, "submitted_at", "filled_at")
                if submitted:
                    trade["entry_datetime"] = str(submitted).replace("Z", "+00:00")
                self.database.add_trade(trade)
                self.database.update_admission(intent_id, "SUBMITTED", trade_id=trade["trade_id"], broker_order_id=order_id)
                self.database.record_execution_event(
                    intent_id=intent_id, decision_id=trade["decision_id"], trade_id=trade["trade_id"],
                    broker_order_id=order_id, ticker=trade["ticker"], side="buy", quantity=float(trade["shares"]),
                    event_type="ORDER_RECOVERED", status=status, payload=match,
                )
                recovered += 1
            except Exception as exc:
                discrepancies.append(f"Could not recover intent {intent_id}: {exc}")

        open_trades = self.database.open_trades()
        known_symbols = {str(trade["ticker"]).upper() for trade in open_trades}
        known_order_ids = {str(trade["broker_order_id"]) for trade in open_trades if trade.get("broker_order_id")}
        known_intents = {str(trade["intent_id"]) for trade in open_trades if trade.get("intent_id")}
        for trade in open_trades:
            symbol = str(trade["ticker"]).upper()
            position = positions_by_symbol.get(symbol)
            root = history_by_id.get(str(trade.get("broker_order_id")))
            root_filled = float(_value(root or {}, "filled_qty", "filled_quantity", default=0) or 0)
            root_fill_price = _value(root or {}, "filled_avg_price", "average_fill_price")
            if position is not None:
                filled_shares = max(float(trade["shares"]), abs(position.quantity), root_filled)
                self.database.update_trade(
                    trade["trade_id"],
                    status="OPEN",
                    shares=filled_shares,
                    entry_price=float(root_fill_price) if root_fill_price is not None else position.average_entry_price,
                    position_value=abs(position.quantity) * position.current_price,
                )
                self.database.record_trade_snapshot(
                    trade["trade_id"], price=position.current_price, stop=float(trade["final_stop"]),
                    unrealized_pnl=(position.current_price - position.average_entry_price) * abs(position.quantity),
                    source=self.broker.name,
                )
                updated += 1
                continue

            entry_time = datetime.fromisoformat(str(trade["entry_datetime"]))
            expected_shares = max(float(trade["shares"]), root_filled)
            if expected_shares > float(trade["shares"]):
                updates: dict[str, Any] = {"shares": expected_shares}
                trade["shares"] = expected_shares
                if root_fill_price is not None:
                    updates["entry_price"] = float(root_fill_price)
                    updates["position_value"] = expected_shares * float(root_fill_price)
                    trade["entry_price"] = float(root_fill_price)
                self.database.update_trade(trade["trade_id"], **updates)
            sell_fills: list[dict[str, Any]] = []
            for row in history:
                if (
                    str(_value(row, "symbol", default="")).upper() != symbol
                    or str(_value(row, "side", default="")).lower() != "sell"
                    or float(_value(row, "filled_qty", "filled_quantity", default=0) or 0) <= 0
                    or _value(row, "filled_avg_price", "average_fill_price") is None
                ):
                    continue
                filled_raw = _value(row, "filled_at", "updated_at", "submitted_at")
                linked_to_entry = str(_value(row, "parent_order_id", default="")) == str(trade.get("broker_order_id"))
                if filled_raw:
                    filled_time = datetime.fromisoformat(str(filled_raw).replace("Z", "+00:00"))
                    if filled_time < entry_time:
                        continue
                elif not linked_to_entry:
                    continue
                sell_fills.append(row)
            exited_quantity = sum(
                float(_value(row, "filled_qty", "filled_quantity", default=0) or 0) for row in sell_fills
            )
            if sell_fills and expected_shares > 0 and exited_quantity + 1e-9 >= expected_shares:
                exit_price = sum(
                    float(_value(row, "filled_qty", "filled_quantity"))
                    * float(_value(row, "filled_avg_price", "average_fill_price"))
                    for row in sell_fills
                ) / exited_quantity
                dated_fills = [
                    _value(row, "filled_at", "updated_at", "submitted_at") for row in sell_fills
                    if _value(row, "filled_at", "updated_at", "submitted_at")
                ]
                exit_raw = max(dated_fills) if dated_fills else None
                exit_time = (
                    datetime.fromisoformat(str(exit_raw).replace("Z", "+00:00"))
                    if exit_raw else datetime.now(timezone.utc)
                )
                high, low = self.database.trade_price_extremes(trade["trade_id"], float(trade["entry_price"]))
                self.postmortems.close_and_review(
                    trade["trade_id"], exit_price=exit_price, exit_datetime=exit_time,
                    high_during_trade=high, low_during_trade=low,
                    reason_exit="broker reconciliation detected a filled exit",
                    assessment={"evidence": {"broker_exit_fills": sell_fills}},
                    backend=self.postmortem_backend,
                    model_name=self.settings.models.postmortem_model or self.settings.models.fallback_model,
                )
                if trade.get("intent_id"):
                    self.database.update_admission(str(trade["intent_id"]), "RECONCILED")
                closed += 1
                continue
            if sell_fills:
                discrepancies.append(
                    f"Trade {trade['trade_id']} ({symbol}) has only {exited_quantity:g}/{float(trade['shares']):g} "
                    "verified exit shares and no remaining broker position"
                )
                continue
            root_status = str(_value(root or {}, "status", default="unknown")).lower()
            root_filled = float(_value(root or {}, "filled_qty", "filled_quantity", default=0) or 0)
            if root_status in TERMINAL_EMPTY_STATUSES and root_filled <= 0:
                final_status = "REJECTED" if root_status == "rejected" else "CANCELED"
                self.database.update_trade(trade["trade_id"], status=final_status)
                if trade.get("intent_id"):
                    self.database.update_admission(str(trade["intent_id"]), final_status)
                updated += 1
            elif root_status in ACTIVE_ORDER_STATUSES and root_filled <= 0:
                # A known, accepted entry can legitimately be awaiting its first fill.
                self.database.update_trade(trade["trade_id"], status="SUBMITTED")
                updated += 1
            else:
                discrepancies.append(f"Local trade {trade['trade_id']} ({symbol}) has no broker position or verified exit")

        for symbol in sorted(set(positions_by_symbol) - known_symbols):
            discrepancies.append(f"Untracked broker position: {symbol}")

        for order in _flatten_orders(open_orders):
            order_id = str(_value(order, "id", "broker_order_id", default=""))
            parent_id = str(_value(order, "parent_order_id", default=""))
            client_id = str(_value(order, "client_order_id", "intent_id", default=""))
            side = str(_value(order, "side", default="")).lower()
            symbol = str(_value(order, "symbol", default="")).upper()
            known = (
                order_id in known_order_ids
                or parent_id in known_order_ids
                or client_id in known_intents
                or client_id in {intent[:48] for intent in known_intents}
                or (side == "sell" and symbol in known_symbols)
            )
            if not known:
                discrepancies.append(f"Untracked open broker order: {order_id or client_id or symbol}")

        self.database.record_equity_snapshot(
            equity=account.equity,
            cash=account.cash,
            buying_power=account.buying_power,
            drawdown_pct=max(0.0, (self.database.equity_high(account.equity) - account.equity) / self.database.equity_high(account.equity)),
            execution_mode=self.settings.execution_mode,
        )
        return self._finish(recovered, updated, closed, discrepancies)

    def _finish(
        self, recovered: int, updated: int, closed: int, discrepancies: list[str],
    ) -> ReconciliationResult:
        reconciled = not discrepancies
        self.database.set_state("reconciliation_halt", not reconciled, "broker_reconciler")
        self.database.set_state(
            "last_reconciliation",
            {"at": datetime.now(timezone.utc).isoformat(), "reconciled": reconciled, "discrepancies": discrepancies},
            "broker_reconciler",
        )
        return ReconciliationResult(
            reconciled=reconciled,
            recovered_admissions=recovered,
            updated_trades=updated,
            closed_trades=closed,
            discrepancies=discrepancies,
        )
