"""Persistent operational, performance, and graduation reports."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .analytics import ExperienceEngine
from .config import SwingSettings
from .database import SwingDatabase
from .notifications import NotificationService, NotificationType


class ReportingService:
    def __init__(self, database: SwingDatabase, settings: SwingSettings, reports_dir: Path):
        self.database = database
        self.settings = settings
        self.reports_dir = reports_dir
        self.analytics = ExperienceEngine(database, settings)

    def dashboard_payload(self) -> dict[str, Any]:
        performance = self.analytics.calculate()
        latest_equity_rows = self.database.rows("SELECT * FROM equity_snapshots ORDER BY observed_at DESC LIMIT 1")
        equity = latest_equity_rows[0] if latest_equity_rows else {}
        costs = self.database.rows("SELECT role, model_name, SUM(cost) AS cost FROM model_costs GROUP BY role, model_name ORDER BY role, model_name")
        counts = self.database.table_counts()
        violations = self.database.rows("SELECT COUNT(*) AS total, SUM(CASE WHEN severity='MAJOR' THEN 1 ELSE 0 END) AS major FROM rule_violations")[0]
        return {
            "account": {
                "equity": equity.get("equity"),
                "cash": equity.get("cash"),
                "buying_power": equity.get("buying_power"),
                "drawdown": equity.get("drawdown_pct"),
                "execution_mode": self.settings.execution_mode,
                "trading_enabled": self.settings.trading_enabled,
                "kill_switch": bool(self.database.get_state("kill_switch", False)),
                "drawdown_halt": bool(self.database.get_state("drawdown_halt", False)),
                "open_risk": sum(float(row.get("planned_dollar_risk") or 0) for row in self.database.open_trades()),
            },
            "performance": performance,
            "trading_quality": {"rule_violations": violations, "closed_trades": len(self.database.closed_trades())},
            "learning": {
                "observations": counts["lessons"],
                "hypotheses": counts["hypotheses"],
                "postmortems": counts["postmortems"],
                "backtests": counts["backtests"],
            },
            "model_costs": costs,
        }

    def generate(self, report_type: str) -> Path:
        valid = {"daily", "weekly", "20-trade", "50-trade", "100-trade", "graduation"}
        if report_type not in valid:
            raise ValueError(f"Unknown report type; choose from {sorted(valid)}")
        payload = self.dashboard_payload()
        count = payload["performance"]["overall"]["trade_count"]
        checkpoints = {"20-trade": 20, "50-trade": 50, "100-trade": 100}
        required = checkpoints.get(report_type)
        if required and count < required:
            raise ValueError(f"{report_type} report requires at least {required} closed trades; found {count}")
        slug = report_type.replace("-", "_").upper()
        self.reports_dir.mkdir(parents=True, exist_ok=True)
        generated_at = datetime.now(timezone.utc)
        path = self.reports_dir / f"{slug}_{generated_at.date().isoformat()}.md"
        graduation = self._graduation(payload) if report_type == "graduation" else None
        path.write_text(
            f"# {report_type.title()} Review\n\n"
            f"Generated: {generated_at.isoformat()}\n\n"
            f"Strategy version: {self.settings.strategy_version}\n\n"
            f"Execution mode: {self.settings.execution_mode}\n\n"
            f"Trading enabled: {self.settings.trading_enabled}\n\n"
            "## Audited payload\n\n"
            f"```json\n{json.dumps(payload, indent=2, default=str, sort_keys=True)}\n```\n" + (f"\n## Graduation assessment\n\n```json\n{json.dumps(graduation, indent=2, sort_keys=True)}\n```\n" if graduation else ""),
            encoding="utf-8",
        )
        self.database.persist_report(report_type, generated_at.isoformat(), self.settings.strategy_version, payload)
        notifications = NotificationService(self.database)
        if report_type == "daily":
            notifications.daily_summary(
                date=generated_at.date().isoformat(),
                candidates=int(self.database.table_counts()["candidate_scores"]),
                opened=len(self.database.open_trades()),
                closed=len(self.database.closed_trades()),
            )
        elif report_type == "weekly":
            notifications.emit(
                NotificationType.WEEKLY_SUMMARY,
                severity="INFO",
                title="Weekly experiment summary",
                reason_code="WEEKLY_SUMMARY",
                payload={"date": generated_at.date().isoformat(), "trade_count": count},
                dedupe_key=f"weekly:{generated_at.date().isoformat()}",
            )
        if count in {20, 50, 100}:
            notifications.emit(
                NotificationType.PAPER_EXPERIMENT_MILESTONE,
                severity="INFO",
                title=f"Paper experiment milestone: {count} trades",
                reason_code="PAPER_EXPERIMENT_MILESTONE",
                payload={"trade_count": count},
                dedupe_key=f"milestone:{count}",
            )
        return path

    def _graduation(self, payload: dict[str, Any]) -> dict[str, Any]:
        metrics = payload["performance"]["overall"]
        costs = sum(float(row.get("cost") or 0) for row in payload["model_costs"])
        benchmark_returns = [row["return"] for row in metrics.get("benchmarks", {}).values() if isinstance(row.get("return"), (int, float))]
        strategy_return = metrics.get("total_return") if isinstance(metrics.get("total_return"), (int, float)) else None
        expectancy = metrics.get("expectancy_r") if isinstance(metrics.get("expectancy_r"), (int, float)) else None
        profit_factor = metrics.get("profit_factor") if isinstance(metrics.get("profit_factor"), (int, float)) else None
        average_winner = metrics.get("average_winner_r") if isinstance(metrics.get("average_winner_r"), (int, float)) else None
        average_loser = metrics.get("average_loser_r") if isinstance(metrics.get("average_loser_r"), (int, float)) else None
        max_drawdown = metrics.get("max_drawdown") if isinstance(metrics.get("max_drawdown"), (int, float)) else None
        realized_pnl = metrics.get("realized_pnl_dollars") if isinstance(metrics.get("realized_pnl_dollars"), (int, float)) else None
        criteria = {
            "50_to_100_trades": metrics["trade_count"] >= 50,
            "positive_expectancy": expectancy is not None and expectancy > 0,
            "profit_factor_over_1_5": profit_factor is not None and profit_factor > 1.5,
            "winner_loss_ratio_over_1_5": average_winner is not None and average_loser is not None and average_loser > 0 and average_winner > 1.5 * average_loser,
            "drawdown_below_8pct": max_drawdown is not None and abs(max_drawdown) < 0.08,
            "zero_major_risk_failures": int(payload["trading_quality"]["rule_violations"].get("major") or 0) == 0,
            "positive_after_model_costs": realized_pnl is not None and realized_pnl - costs > 0,
            "not_explained_by_benchmark": strategy_return is not None and all(strategy_return > value for value in benchmark_returns) if benchmark_returns else False,
        }
        return {
            "eligible_for_human_consideration": all(criteria.values()),
            "criteria": criteria,
            "automatic_capital_increase": False,
            "note": "Only a human can approve or fund a larger experiment.",
        }
