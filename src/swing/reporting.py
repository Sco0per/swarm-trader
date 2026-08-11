"""Persistent operational, performance, and graduation reports."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .analytics import ExperienceEngine
from .config import SwingSettings
from .database import SwingDatabase


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
        costs = self.database.rows(
            "SELECT role, model_name, SUM(cost) AS cost FROM model_costs GROUP BY role, model_name ORDER BY role, model_name"
        )
        counts = self.database.table_counts()
        violations = self.database.rows(
            "SELECT COUNT(*) AS total, SUM(CASE WHEN severity='MAJOR' THEN 1 ELSE 0 END) AS major FROM rule_violations"
        )[0]
        return {
            "account": {
                "equity": equity.get("equity"), "cash": equity.get("cash"), "buying_power": equity.get("buying_power"),
                "drawdown": equity.get("drawdown_pct"), "execution_mode": self.settings.execution_mode,
                "trading_enabled": self.settings.trading_enabled,
                "kill_switch": bool(self.database.get_state("kill_switch", False)),
                "drawdown_halt": bool(self.database.get_state("drawdown_halt", False)),
                "open_risk": sum(float(row.get("planned_dollar_risk") or 0) for row in self.database.open_trades()),
            },
            "performance": performance,
            "trading_quality": {"rule_violations": violations, "closed_trades": len(self.database.closed_trades())},
            "learning": {
                "observations": counts["lessons"], "hypotheses": counts["hypotheses"],
                "postmortems": counts["postmortems"], "backtests": counts["backtests"],
            },
            "model_costs": costs,
        }

    def generate(self, report_type: str) -> Path:
        valid = {"daily", "weekly", "20-trade", "50-trade", "100-trade", "graduation"}
        if report_type not in valid:
            raise ValueError(f"Unknown report type; choose from {sorted(valid)}")
        payload = self.dashboard_payload()
        count = payload["performance"]["overall"]["sample_size"]
        checkpoints = {"20-trade": 20, "50-trade": 50, "100-trade": 100}
        required = checkpoints.get(report_type)
        if required and count < required:
            raise ValueError(f"{report_type} report requires at least {required} closed trades; found {count}")
        slug = report_type.replace("-", "_").upper()
        self.reports_dir.mkdir(parents=True, exist_ok=True)
        path = self.reports_dir / f"{slug}_{datetime.now().date().isoformat()}.md"
        graduation = self._graduation(payload) if report_type == "graduation" else None
        path.write_text(
            f"# {report_type.title()} Review\n\n"
            f"Generated: {datetime.now(timezone.utc).isoformat()}\n\n"
            f"Strategy version: {self.settings.strategy_version}\n\n"
            f"Execution mode: {self.settings.execution_mode}\n\n"
            f"Trading enabled: {self.settings.trading_enabled}\n\n"
            "## Audited payload\n\n"
            f"```json\n{json.dumps(payload, indent=2, default=str, sort_keys=True)}\n```\n"
            + (f"\n## Graduation assessment\n\n```json\n{json.dumps(graduation, indent=2, sort_keys=True)}\n```\n" if graduation else ""),
            encoding="utf-8",
        )
        return path

    def _graduation(self, payload: dict[str, Any]) -> dict[str, Any]:
        metrics = payload["performance"]["overall"]
        costs = sum(float(row.get("cost") or 0) for row in payload["model_costs"])
        benchmark_returns = [row["return"] for row in metrics.get("benchmarks", {}).values()]
        strategy_return = metrics.get("total_return")
        criteria = {
            "50_to_100_trades": metrics["sample_size"] >= 50,
            "positive_expectancy": metrics["expectancy_r"] > 0,
            "profit_factor_over_1_5": (metrics.get("profit_factor") or 0) > 1.5,
            "winner_loss_ratio_over_1_5": metrics["average_win_r"] > 1.5 * metrics["average_loss_r"] if metrics["average_loss_r"] else False,
            "drawdown_below_8pct": metrics.get("max_drawdown") is not None and abs(metrics["max_drawdown"]) < 0.08,
            "zero_major_risk_failures": int(payload["trading_quality"]["rule_violations"].get("major") or 0) == 0,
            "positive_after_model_costs": float(metrics.get("realized_pnl") or 0) - costs > 0,
            "not_explained_by_benchmark": strategy_return is not None and all(strategy_return > value for value in benchmark_returns) if benchmark_returns else False,
        }
        return {
            "eligible_for_human_consideration": all(criteria.values()),
            "criteria": criteria,
            "automatic_capital_increase": False,
            "note": "Only a human can approve or fund a larger experiment.",
        }
