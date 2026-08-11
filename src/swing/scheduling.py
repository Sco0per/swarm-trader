"""Configurable ET schedule and narrow-agent command allowlists."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ScheduledJob:
    name: str
    schedule_env: str
    default_et: str
    command: str
    purpose: str

    @property
    def agent_prompt(self) -> str:
        return f"Run exactly one permitted command: `poetry run swing-trader {self.command}`. " f"Purpose: {self.purpose}. Every other shell command and every broker/live subcommand is forbidden. " "Return only the structured command result; never print environment variables or secrets."


JOBS = (
    ScheduledJob("initial-scan", "SCHEDULE_INITIAL_SCAN_ET", "09:35", "scan", "cheap deterministic initial scan"),
    ScheduledJob("decision-cycle", "SCHEDULE_DECISION_CYCLE_ET", "10:15", "run", "change-gated LLM decision cycle"),
    ScheduledJob("midday-refresh", "SCHEDULE_MIDDAY_REFRESH_ET", "12:30", "scan", "cheap deterministic refresh"),
    ScheduledJob("afternoon-refresh", "SCHEDULE_AFTERNOON_REFRESH_ET", "14:30", "scan", "cheap deterministic refresh"),
    ScheduledJob("position-health", "SCHEDULE_POSITION_HEALTH_ET", "15:45", "reconcile", "broker reconciliation and position health"),
    ScheduledJob("daily-report", "SCHEDULE_DAILY_REPORT_ET", "16:15", "report daily", "daily summary and analytics"),
    ScheduledJob("weekly-lessons", "SCHEDULE_WEEKLY_LESSONS_ET", "Sunday 18:00", "review-observations", "research-hypothesis aggregation only"),
)
