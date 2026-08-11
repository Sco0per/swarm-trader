"""Configurable ET schedule and narrow-agent command allowlists."""

from __future__ import annotations

from dataclasses import dataclass

from .config import ScheduleSettings


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


def jobs_for(schedule: ScheduleSettings) -> tuple[ScheduledJob, ...]:
    """Build the allowlisted jobs from the already-validated central settings."""

    return (
        ScheduledJob("initial-scan", "SCHEDULE_INITIAL_SCAN_ET", schedule.initial_scan_et, "scan", "cheap deterministic initial scan"),
        ScheduledJob("decision-cycle", "SCHEDULE_DECISION_CYCLE_ET", schedule.decision_cycle_et, "run", "change-gated LLM decision cycle"),
        ScheduledJob("midday-refresh", "SCHEDULE_MIDDAY_REFRESH_ET", schedule.midday_refresh_et, "scan", "cheap deterministic refresh"),
        ScheduledJob("afternoon-refresh", "SCHEDULE_AFTERNOON_REFRESH_ET", schedule.afternoon_refresh_et, "scan", "cheap deterministic refresh"),
        ScheduledJob("position-health", "SCHEDULE_POSITION_HEALTH_ET", schedule.position_health_et, "reconcile", "broker reconciliation and position health"),
        ScheduledJob("daily-report", "SCHEDULE_DAILY_REPORT_ET", schedule.daily_report_et, "report daily", "daily summary and analytics"),
        ScheduledJob("weekly-lessons", "SCHEDULE_WEEKLY_LESSONS_ET", schedule.weekly_lessons_et, "review-observations", "research-hypothesis aggregation only"),
    )


JOBS = jobs_for(ScheduleSettings())
