"""Broker-native bracket evidence checks; missing protection always fails closed."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from .models import BrokerOrder, OrderIntent


TERMINAL_BAD_LEG_STATUSES = {"rejected", "canceled", "cancelled", "expired", "stopped", "suspended"}


class ProtectionEvidence(BaseModel):
    protected: bool
    reason_code: str
    message: str
    stop_order_ids: list[str] = Field(default_factory=list)
    target_order_ids: list[str] = Field(default_factory=list)
    payload: dict[str, Any] = Field(default_factory=dict)


def verify_broker_bracket(intent: OrderIntent, order: BrokerOrder) -> ProtectionEvidence:
    raw = order.raw or {}
    order_class = str(raw.get("order_class") or raw.get("class") or "").lower()
    if order_class != "bracket":
        return ProtectionEvidence(protected=False, reason_code="PROTECTION_NOT_BRACKET", message="Broker response did not confirm a bracket order", payload=raw)
    legs = [leg for leg in raw.get("legs") or [] if isinstance(leg, dict)]
    stop_legs: list[dict[str, Any]] = []
    target_legs: list[dict[str, Any]] = []
    for leg in legs:
        leg_type = str(leg.get("type") or leg.get("order_type") or "").lower()
        if leg.get("stop_price") not in (None, "") or leg_type in {"stop", "stop_limit", "trailing_stop"}:
            stop_legs.append(leg)
        elif leg.get("limit_price") not in (None, "") or leg_type in {"limit", "take_profit"}:
            target_legs.append(leg)
    for kind, candidates in (("stop", stop_legs), ("target", target_legs)):
        bad = [leg for leg in candidates if str(leg.get("status", "unknown")).lower() in TERMINAL_BAD_LEG_STATUSES]
        if bad:
            return ProtectionEvidence(
                protected=False,
                reason_code=f"PROTECTION_{kind.upper()}_LEG_TERMINAL",
                message=f"Broker {kind} leg is rejected, cancelled, or otherwise terminal",
                payload=raw,
            )
    if order.filled_quantity > 0 and (not stop_legs or not target_legs):
        return ProtectionEvidence(
            protected=False,
            reason_code="PROTECTION_LEGS_MISSING_AFTER_FILL",
            message="Filled quantity exists without evidence of both protective bracket legs",
            payload=raw,
        )
    if not legs:
        return ProtectionEvidence(
            protected=order.filled_quantity <= 0,
            reason_code="PROTECTION_ATOMIC_BRACKET_PENDING_FILL" if order.filled_quantity <= 0 else "PROTECTION_LEGS_MISSING_AFTER_FILL",
            message="Atomic bracket accepted; legs are expected when a fill occurs" if order.filled_quantity <= 0 else "Protective legs are missing after fill",
            payload=raw,
        )
    return ProtectionEvidence(
        protected=True,
        reason_code="PROTECTION_VERIFIED",
        message="Broker confirms active stop-loss and take-profit legs",
        stop_order_ids=[str(leg.get("id") or leg.get("broker_order_id") or "") for leg in stop_legs],
        target_order_ids=[str(leg.get("id") or leg.get("broker_order_id") or "") for leg in target_legs],
        payload=raw,
    )
