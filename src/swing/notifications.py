"""Structured, durable, noise-controlled notification events."""

from __future__ import annotations

from enum import Enum
from typing import Any

from .database import SwingDatabase


class NotificationType(str, Enum):
    NEW_HIGH_QUALITY_CANDIDATE = "NEW_HIGH_QUALITY_CANDIDATE"
    PAPER_TRADE_OPENED = "PAPER_TRADE_OPENED"
    TRADE_REJECTED_BY_RISK = "TRADE_REJECTED_BY_RISK"
    STOP_TRIGGERED = "STOP_TRIGGERED"
    TARGET_TRIGGERED = "TARGET_TRIGGERED"
    POSITION_CLOSED = "POSITION_CLOSED"
    BROKER_DATABASE_MISMATCH = "BROKER_DATABASE_MISMATCH"
    KILL_SWITCH_ACTIVATED = "KILL_SWITCH_ACTIVATED"
    DAILY_SUMMARY = "DAILY_SUMMARY"
    WEEKLY_SUMMARY = "WEEKLY_SUMMARY"
    PAPER_EXPERIMENT_MILESTONE = "PAPER_EXPERIMENT_MILESTONE"
    RUN_CYCLE_SUMMARY = "RUN_CYCLE_SUMMARY"
    RUN_FAILED = "RUN_FAILED"


class NotificationService:
    def __init__(self, database: SwingDatabase):
        self.database = database

    def emit(
        self,
        event_type: NotificationType,
        *,
        severity: str,
        title: str,
        reason_code: str,
        payload: dict[str, Any] | None = None,
        dedupe_key: str | None = None,
    ) -> bool:
        return self.database.record_notification(
            event_type=event_type.value,
            severity=severity,
            title=title,
            reason_code=reason_code,
            payload=payload or {},
            dedupe_key=dedupe_key,
        )

    def daily_summary(self, *, date: str, candidates: int, opened: int, closed: int) -> bool:
        result = "NO_TRADE" if opened == 0 else "ACTIVITY"
        return self.emit(
            NotificationType.DAILY_SUMMARY,
            severity="INFO",
            title=f"Daily summary: {result}",
            reason_code=result,
            payload={"date": date, "candidates": candidates, "opened": opened, "closed": closed},
            dedupe_key=f"daily:{date}",
        )
