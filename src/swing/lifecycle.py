"""Deterministic, persisted position lifecycle and researchable exit policies."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel

from .config import SwingSettings
from .database import SwingDatabase
from .models import PositionLifecycleState, TimeStopAction, TradeThesis

LEGAL_TRANSITIONS: dict[PositionLifecycleState, frozenset[PositionLifecycleState]] = {
    PositionLifecycleState.OPEN: frozenset({PositionLifecycleState.PROTECTED, PositionLifecycleState.EXIT_PENDING}),
    PositionLifecycleState.PROTECTED: frozenset({PositionLifecycleState.PROFITABLE, PositionLifecycleState.EXIT_PENDING}),
    PositionLifecycleState.PROFITABLE: frozenset({PositionLifecycleState.TRAILING, PositionLifecycleState.EXIT_PENDING}),
    PositionLifecycleState.TRAILING: frozenset({PositionLifecycleState.EXIT_PENDING}),
    PositionLifecycleState.EXIT_PENDING: frozenset({PositionLifecycleState.CLOSED}),
    PositionLifecycleState.CLOSED: frozenset(),
}


class IllegalLifecycleTransition(RuntimeError):
    pass


class LifecycleDecision(BaseModel):
    state: PositionLifecycleState
    action: Literal["HOLD", "REVIEW", "EXIT", "TIGHTEN_STOP"] = "HOLD"
    reason: str
    reason_code: str
    proposed_stop: float | None = None


@dataclass(frozen=True)
class TimeStopInputs:
    days_held: int
    progress_r: float
    expected_hold_days: int
    max_hold_days: int
    trend_degradation: bool


class TimeStopPolicy:
    def __init__(self, settings: SwingSettings):
        self.settings = settings

    def evaluate(self, inputs: TimeStopInputs) -> LifecycleDecision:
        if inputs.days_held < 0:
            raise ValueError("days_held cannot be negative")
        due_to_maximum = inputs.days_held >= inputs.max_hold_days
        dead_after_expected_window = inputs.days_held >= inputs.expected_hold_days and inputs.progress_r < self.settings.time_stop_minimum_progress_r and inputs.trend_degradation
        if not due_to_maximum and not dead_after_expected_window:
            return LifecycleDecision(
                state=PositionLifecycleState.PROTECTED,
                action="HOLD",
                reason="Time-stop criteria are not met",
                reason_code="HOLD_TIME_STOP_NOT_DUE",
            )
        action = TimeStopAction(self.settings.time_stop_mode)
        reason_code = "EXIT_TIME_STOP" if action == TimeStopAction.EXIT else "REVIEW_TIME_STOP"
        return LifecycleDecision(
            state=PositionLifecycleState.EXIT_PENDING if action == TimeStopAction.EXIT else PositionLifecycleState.PROTECTED,
            action=action.value,
            reason="Maximum hold reached" if due_to_maximum else "Expected swing did not develop and trend degraded",
            reason_code=reason_code,
        )


class StopManagementPolicy:
    """Named policy; breakeven movement is disabled unless explicitly configured."""

    def __init__(self, settings: SwingSettings):
        self.settings = settings

    @property
    def name(self) -> str:
        return self.settings.stop_management_policy

    def evaluate(
        self,
        *,
        thesis: TradeThesis,
        current_stop: float,
        current_price: float,
        high_watermark: float,
    ) -> LifecycleDecision:
        progress_r = (current_price - thesis.entry) / thesis.initial_risk_per_share
        profitable_state = PositionLifecycleState.PROFITABLE if progress_r >= self.settings.profitable_trigger_r else PositionLifecycleState.PROTECTED
        if self.name == "STRUCTURAL_ONLY":
            return LifecycleDecision(state=profitable_state, reason="Structural-only policy retains broker stop", reason_code="HOLD_STRUCTURAL_STOP")
        eligible = thesis.setup.value in self.settings.trailing_eligible_setups
        if not eligible or progress_r < self.settings.trailing_activation_r:
            return LifecycleDecision(state=profitable_state, reason="Trailing is not appropriate for this setup/progress", reason_code="HOLD_TRAILING_NOT_ACTIVE")
        proposed = max(current_stop, high_watermark - self.settings.trailing_distance_r * thesis.initial_risk_per_share)
        if self.settings.move_to_breakeven_at_r is not None and progress_r >= self.settings.move_to_breakeven_at_r:
            proposed = max(proposed, thesis.entry)
        if proposed <= current_stop:
            return LifecycleDecision(state=PositionLifecycleState.TRAILING, reason="Existing stop is already at least as protective", reason_code="HOLD_STOP_ALREADY_TIGHTER")
        return LifecycleDecision(
            state=PositionLifecycleState.TRAILING,
            action="TIGHTEN_STOP",
            reason=f"{self.name} activated at {progress_r:.2f}R",
            reason_code="TIGHTEN_POLICY_TRAIL",
            proposed_stop=proposed,
        )


class PositionLifecycleService:
    def __init__(self, settings: SwingSettings, database: SwingDatabase):
        self.settings = settings
        self.database = database
        self.time_stop = TimeStopPolicy(settings)
        self.stop_policy = StopManagementPolicy(settings)

    def create_open(self, trade_id: str, *, current_stop: float, trigger: str = "broker_fill") -> None:
        self.database.create_position_lifecycle(trade_id, PositionLifecycleState.OPEN, current_stop=current_stop, trigger=trigger)

    def transition(
        self,
        trade_id: str,
        new_state: PositionLifecycleState,
        *,
        trigger: str,
        reason_code: str,
        context: dict | None = None,
    ) -> None:
        current = self.database.get_position_lifecycle(trade_id)
        if current is None:
            raise IllegalLifecycleTransition(f"Trade {trade_id} has no persisted lifecycle")
        old_state = PositionLifecycleState(current["state"])
        if new_state not in LEGAL_TRANSITIONS[old_state]:
            raise IllegalLifecycleTransition(f"Illegal lifecycle transition {old_state.value} -> {new_state.value}")
        if not self.database.transition_position_lifecycle(
            trade_id,
            expected_state=old_state,
            new_state=new_state,
            trigger=trigger,
            reason_code=reason_code,
            context=context or {},
        ):
            raise IllegalLifecycleTransition(f"Concurrent lifecycle transition rejected for {trade_id}")

    def mark_protected(self, trade_id: str, *, evidence: dict) -> None:
        self.transition(
            trade_id,
            PositionLifecycleState.PROTECTED,
            trigger="broker_protection_verified",
            reason_code="PROTECTION_VERIFIED",
            context=evidence,
        )

    def request_exit(self, trade_id: str, *, trigger: str, reason_code: str, context: dict | None = None) -> None:
        current = self.database.get_position_lifecycle(trade_id)
        if current is None:
            raise IllegalLifecycleTransition(f"Trade {trade_id} has no persisted lifecycle")
        if PositionLifecycleState(current["state"]) == PositionLifecycleState.EXIT_PENDING:
            return
        self.transition(trade_id, PositionLifecycleState.EXIT_PENDING, trigger=trigger, reason_code=reason_code, context=context)

    def mark_closed(self, trade_id: str, *, reason_code: str, context: dict | None = None) -> None:
        self.transition(
            trade_id,
            PositionLifecycleState.CLOSED,
            trigger="broker_exit_reconciled",
            reason_code=reason_code,
            context=context,
        )

    def evaluate_exit(
        self,
        trade_id: str,
        *,
        thesis_invalidated: bool = False,
        technical_trend_failure: bool = False,
        event_risk_exit: bool = False,
        manual_safety_exit: bool = False,
        time_stop_inputs: TimeStopInputs | None = None,
    ) -> LifecycleDecision:
        triggers = (
            (manual_safety_exit, "EXIT_MANUAL_SAFETY", "manual_safety_exit"),
            (event_risk_exit, "EXIT_EVENT_RISK", "event_risk_exit"),
            (thesis_invalidated, "EXIT_THESIS_INVALIDATED", "thesis_invalidation"),
            (technical_trend_failure, "EXIT_TECHNICAL_TREND_FAILURE", "technical_trend_failure"),
        )
        for active, reason_code, trigger in triggers:
            if active:
                self.request_exit(trade_id, trigger=trigger, reason_code=reason_code)
                return LifecycleDecision(state=PositionLifecycleState.EXIT_PENDING, action="EXIT", reason=trigger.replace("_", " "), reason_code=reason_code)
        if time_stop_inputs is not None:
            decision = self.time_stop.evaluate(time_stop_inputs)
            if decision.action == "EXIT":
                self.request_exit(trade_id, trigger="time_stop", reason_code=decision.reason_code, context={"inputs": time_stop_inputs.__dict__})
            return decision
        current = self.database.get_position_lifecycle(trade_id)
        state = PositionLifecycleState(current["state"]) if current else PositionLifecycleState.OPEN
        return LifecycleDecision(state=state, reason="No deterministic exit trigger", reason_code="HOLD_NO_EXIT_TRIGGER")

    def evaluate_stop_management(
        self,
        trade_id: str,
        *,
        current_price: float,
        high_watermark: float,
    ) -> LifecycleDecision:
        thesis = self.database.get_trade_thesis(trade_id)
        current = self.database.get_position_lifecycle(trade_id)
        if thesis is None or current is None:
            raise ValueError("Stop management requires both immutable thesis and persisted lifecycle")
        state = PositionLifecycleState(current["state"])
        if state not in {PositionLifecycleState.PROTECTED, PositionLifecycleState.PROFITABLE, PositionLifecycleState.TRAILING}:
            raise IllegalLifecycleTransition(f"Stop policy cannot evaluate lifecycle state {state.value}")
        decision = self.stop_policy.evaluate(
            thesis=thesis,
            current_stop=float(current["current_stop"]),
            current_price=current_price,
            high_watermark=high_watermark,
        )
        if state == PositionLifecycleState.PROTECTED and decision.state in {PositionLifecycleState.PROFITABLE, PositionLifecycleState.TRAILING}:
            self.transition(
                trade_id,
                PositionLifecycleState.PROFITABLE,
                trigger="profit_threshold",
                reason_code="POSITION_REACHED_PROFITABLE_R",
                context={"current_price": current_price},
            )
            state = PositionLifecycleState.PROFITABLE
        if state == PositionLifecycleState.PROFITABLE and decision.state == PositionLifecycleState.TRAILING:
            self.transition(
                trade_id,
                PositionLifecycleState.TRAILING,
                trigger="named_stop_policy",
                reason_code=decision.reason_code,
                context={"policy": self.stop_policy.name, "proposed_stop": decision.proposed_stop},
            )
        self.database.update_position_runtime(trade_id, high_watermark=high_watermark)
        return decision
