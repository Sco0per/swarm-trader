"""Observation -> hypothesis -> validation -> human approval orchestration."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from src.swing.config import SwingSettings
from src.swing.database import SwingDatabase

from .fitness import FitnessMetrics, balanced_fitness


class SwingResearchEngine:
    def __init__(self, database: SwingDatabase, settings: SwingSettings, reports_dir: Path):
        self.database = database
        self.settings = settings
        self.reports_dir = reports_dir

    def propose(
        self, *, observation: str, proposed_rule: str, supporting_lesson_ids: list[str],
        supporting_sample_size: int, setup_type: str | None, market_regime: str | None,
        candidate_strategy_version: str,
    ) -> str:
        if supporting_sample_size < 5 or len(set(supporting_lesson_ids)) < 2:
            raise ValueError("A hypothesis requires multiple related observations and at least five supporting samples")
        if not candidate_strategy_version.endswith("_CANDIDATE"):
            raise ValueError("Research strategy versions must end in _CANDIDATE")
        hypothesis_id = str(uuid4())
        self.database.add_hypothesis({
            "hypothesis_id": hypothesis_id,
            "observation": observation,
            "proposed_rule": proposed_rule,
            "setup_type": setup_type,
            "market_regime": market_regime,
            "supporting_lesson_ids": supporting_lesson_ids,
            "supporting_sample_size": supporting_sample_size,
            "candidate_strategy_version": candidate_strategy_version,
            "validation_plan": {
                "required": ["train", "validation", "out_of_sample", "walk_forward", "multi_regime", "benchmark"],
                "minimum_trades": self.settings.minimum_statistical_sample,
                "promotion": "explicit_human_only",
            },
        })
        return hypothesis_id

    def record_comparison(
        self, hypothesis_id: str, *, baseline: FitnessMetrics, candidate: FitnessMetrics,
        data_hash: str, config_hash: str, train_period: str, validation_period: str,
        out_of_sample_period: str, regime_results: dict, benchmark: dict, walk_forward: dict,
    ) -> dict:
        hypothesis = self.database.get_hypothesis(hypothesis_id)
        if not hypothesis:
            raise KeyError(hypothesis_id)
        base_score, base_detail = balanced_fitness(baseline)
        candidate_score, candidate_detail = balanced_fitness(candidate)
        stable_regimes = sum(1 for values in regime_results.values() if values.get("expectancy_r", 0) > 0)
        accepted = all([
            candidate.trade_count >= self.settings.minimum_statistical_sample,
            candidate.expectancy_r > 0,
            candidate.profit_factor > 1,
            candidate.max_drawdown < self.settings.halt_drawdown_pct,
            candidate_score > base_score,
            candidate.regime_stability >= 0.5,
            stable_regimes >= 2,
            bool(walk_forward.get("positive_out_of_sample", False)),
        ])
        backtest_id = str(uuid4())
        self.database.add_backtest({
            "backtest_id": backtest_id,
            "hypothesis_id": hypothesis_id,
            "strategy_version": hypothesis["candidate_strategy_version"],
            "data_hash": data_hash,
            "config_hash": config_hash,
            "train_period": train_period,
            "validation_period": validation_period,
            "out_of_sample_period": out_of_sample_period,
            "metrics": candidate.model_dump(),
            "benchmark": benchmark,
            "regime_results": regime_results,
            "walk_forward": walk_forward,
            "accepted": accepted,
        })
        results = {
            "accepted_as_candidate": accepted,
            "production_changed": False,
            "baseline_fitness": base_score,
            "candidate_fitness": candidate_score,
            "baseline_components": base_detail,
            "candidate_components": candidate_detail,
            "backtest_id": backtest_id,
            "human_approval_required": True,
        }
        self.database.update_hypothesis_validation(hypothesis_id, status="VALIDATED" if accepted else "REJECTED", results=results)
        self._write_report(hypothesis, baseline, candidate, results, regime_results, benchmark)
        return results

    def approve(self, hypothesis_id: str, approved_by: str) -> str:
        """The sole promotion path; caller must provide a human identity/audit label."""
        return self.database.approve_candidate_strategy(hypothesis_id, approved_by)

    def _write_report(self, hypothesis: dict, baseline: FitnessMetrics, candidate: FitnessMetrics, results: dict, regimes: dict, benchmark: dict) -> Path:
        output = self.reports_dir / "strategy_candidates"
        output.mkdir(parents=True, exist_ok=True)
        path = output / f"{hypothesis['hypothesis_id']}.md"
        path.write_text(
            "# Strategy Candidate Report\n\n"
            f"Generated: {datetime.now(timezone.utc).isoformat()}\n\n"
            f"Hypothesis: {hypothesis['observation']}\n\n"
            f"Proposed rule: {hypothesis['proposed_rule']}\n\n"
            f"Candidate version: {hypothesis['candidate_strategy_version']}\n\n"
            f"Accepted for human review: {results['accepted_as_candidate']}\n\n"
            "Production changed automatically: **No**\n\n"
            f"Baseline metrics: `{json.dumps(baseline.model_dump(), sort_keys=True)}`\n\n"
            f"Candidate metrics: `{json.dumps(candidate.model_dump(), sort_keys=True)}`\n\n"
            f"Regime results: `{json.dumps(regimes, sort_keys=True)}`\n\n"
            f"Benchmark: `{json.dumps(benchmark, sort_keys=True)}`\n",
            encoding="utf-8",
        )
        return path
