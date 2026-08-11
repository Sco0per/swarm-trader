"""Automatic close metrics and postmortem-to-observation workflow."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict

from .agents import StructuredModelBackend
from .config import SwingSettings
from .database import SwingDatabase
from .models import PostmortemClassification, PostmortemRecord
from .notifications import NotificationService, NotificationType


class PostmortemAssessment(BaseModel):
    """Structured LLM judgment for a single closed trade.

    A loss is not automatically a bad trade and a win is not automatically a
    good one -- the model must answer the process questions explicitly.
    """

    model_config = ConfigDict(extra="forbid")
    classification: PostmortemClassification
    followed_strategy: bool
    entry_valid: bool
    regime_correct: bool
    sector_favorable: bool
    relative_strength_favorable: bool
    stop_sensible: bool
    target_realistic: bool
    execution_hurt: bool
    unexpected_event: bool
    rule_rationalization: bool
    normal_variance: bool
    repeats_known_lesson: bool
    mistake_type: str | None = None
    reasoning: str
    original_setup_valid: bool | None = None
    scanner_correct: bool | None = None
    entry_timing_good: bool | None = None
    thesis_valid: bool | None = None
    exit_good: bool | None = None
    result_driver: str = "UNKNOWN"
    would_take_again: bool | None = None


class PostmortemEngine:
    def __init__(
        self,
        database: SwingDatabase,
        *,
        settings: SwingSettings | None = None,
        consecutive_loss_halt: int = 3,
        report_dir: Path | None = None,
    ):
        self.database = database
        self.settings = settings
        self.consecutive_loss_halt = consecutive_loss_halt
        self.report_dir = report_dir or Path(__file__).resolve().parents[2] / "reports"

    def close_and_review(
        self,
        trade_id: str,
        *,
        exit_price: float,
        exit_datetime: datetime | None = None,
        high_during_trade: float,
        low_during_trade: float,
        reason_exit: str,
        assessment: dict | None = None,
        model_name: str | None = None,
        backend: StructuredModelBackend | None = None,
    ) -> PostmortemRecord:
        trade = self.database.get_trade(trade_id)
        if not trade:
            raise KeyError(trade_id)
        if trade["status"] == "CLOSED":
            raise ValueError("Trade is already closed and postmortem must not be duplicated")
        exit_datetime = exit_datetime or datetime.now(timezone.utc)
        if exit_datetime.tzinfo is None:
            raise ValueError("exit_datetime must be timezone-aware")
        entry_price = float(trade["entry_price"])
        shares = float(trade["shares"])
        initial_risk_per_share = entry_price - float(trade["initial_stop"])
        if initial_risk_per_share <= 0:
            raise ValueError("Stored initial risk is invalid")
        initial_risk_dollars = float(trade.get("initial_risk_dollars") or shares * initial_risk_per_share)
        if initial_risk_dollars <= 0:
            raise ValueError("Stored initial risk dollars are invalid")
        realized_pnl = (exit_price - entry_price) * shares
        realized_r = realized_pnl / initial_risk_dollars
        mfe = max(0.0, high_during_trade - entry_price)
        mae = max(0.0, entry_price - low_during_trade)
        mfe_r = mfe * shares / initial_risk_dollars
        mae_r = mae * shares / initial_risk_dollars
        entry_time = datetime.fromisoformat(trade["entry_datetime"])
        holding_days = (exit_datetime - entry_time).total_seconds() / 86_400
        lowered_exit_reason = reason_exit.lower()
        expected_exit = None
        if "target" in lowered_exit_reason:
            expected_exit = float(trade.get("initial_target") or trade["target"])
        elif "stop" in lowered_exit_reason:
            expected_exit = float(trade["final_stop"])
        self.database.update_trade(
            trade_id,
            status="CLOSED",
            exit_datetime=exit_datetime.isoformat(),
            exit_price=exit_price,
            realized_pnl=realized_pnl,
            realized_r=realized_r,
            unrealized_r=0.0,
            mfe=mfe,
            mae=mae,
            mfe_r=mfe_r,
            mae_r=mae_r,
            holding_period_days=holding_days,
            reason_exit=reason_exit,
            exit_slippage=exit_price - expected_exit if expected_exit is not None else None,
        )
        mechanical = self._mechanical_assessment(trade)
        assessment = dict(assessment or {})
        if backend is not None and model_name and "classification" not in assessment:
            try:
                llm_assessment = self._classify_with_llm(
                    backend,
                    model_name,
                    trade,
                    realized_r=realized_r,
                    mfe_r=mfe_r,
                    mae_r=mae_r,
                    holding_days=holding_days,
                    reason_exit=reason_exit,
                    mechanical=mechanical,
                )
                assessment = {**llm_assessment, **assessment}
            except Exception:
                # Fail closed to the deterministic default below; a postmortem
                # must always complete even if the model call fails.
                pass

        def resolved(name: str) -> Any:
            return mechanical.get(name) if mechanical.get(name) is not None else assessment.get(name)

        process_answers = {
            "original_setup_valid": resolved("original_setup_valid"),
            "scanner_correct": resolved("scanner_correct"),
            "entry_valid": resolved("entry_valid"),
            "entry_timing_good": assessment.get("entry_timing_good"),
            "sizing_correct": mechanical.get("sizing_correct"),
            "stop_placement_correct": mechanical.get("stop_placement_correct"),
            "execution_correct": mechanical.get("execution_correct"),
            "thesis_valid": assessment.get("thesis_valid"),
            "exit_good": assessment.get("exit_good"),
            "would_take_again": assessment.get("would_take_again"),
            "earnings_gate_respected": mechanical.get("earnings_gate_respected"),
        }
        known_process = [value for value in process_answers.values() if isinstance(value, bool)]
        process_quality_score = 100.0 * sum(known_process) / len(known_process) if known_process else None
        followed_strategy = all(value is not False for value in mechanical.values()) and bool(assessment.get("followed_strategy", True))
        if not followed_strategy:
            classification = PostmortemClassification.RULE_VIOLATION
        elif assessment.get("classification"):
            classification = PostmortemClassification(assessment["classification"])
        elif realized_pnl < 0:
            classification = PostmortemClassification.VALID_LOSS
        else:
            # A winner is not automatically a good trade; explicit flags still dominate.
            classification = PostmortemClassification.UNKNOWN
        normal_variance = classification == PostmortemClassification.VALID_LOSS and followed_strategy
        record = PostmortemRecord(
            trade_id=trade_id,
            classification=classification,
            followed_strategy=followed_strategy,
            entry_valid=bool(process_answers["entry_valid"]) if process_answers["entry_valid"] is not None else followed_strategy,
            regime_correct=assessment.get("regime_correct"),
            sector_favorable=assessment.get("sector_favorable"),
            relative_strength_favorable=assessment.get("relative_strength_favorable"),
            stop_sensible=assessment.get("stop_sensible"),
            target_realistic=assessment.get("target_realistic"),
            execution_hurt=assessment.get("execution_hurt"),
            unexpected_event=assessment.get("unexpected_event"),
            rule_rationalization=bool(assessment.get("rule_rationalization", False)),
            mfe_r=mfe_r,
            mae_r=mae_r,
            normal_variance=normal_variance,
            mistake_type=assessment.get("mistake_type"),
            lessons=list(assessment.get("lessons", [])),
            evidence={
                "realized_r": realized_r,
                "mfe_r": mfe_r,
                "mae_r": mae_r,
                "holding_days": holding_days,
                "reason_exit": reason_exit,
                **dict(assessment.get("evidence", {})),
            },
            original_setup_valid=process_answers["original_setup_valid"],
            scanner_correct=process_answers["scanner_correct"],
            entry_timing_good=process_answers["entry_timing_good"],
            sizing_correct=process_answers["sizing_correct"],
            stop_placement_correct=process_answers["stop_placement_correct"],
            execution_correct=process_answers["execution_correct"],
            thesis_valid=process_answers["thesis_valid"],
            exit_good=process_answers["exit_good"],
            result_driver=self._result_driver(assessment.get("result_driver")),
            would_take_again=process_answers["would_take_again"],
            process_quality_score=process_quality_score,
            outcome="WIN" if realized_r > 0 else "LOSS" if realized_r < 0 else "BREAKEVEN",
            mechanical_answers=mechanical,
            narrative=assessment.get("reasoning"),
        )
        self.database.record_postmortem(str(uuid4()), record, model_name)
        notifications = NotificationService(self.database)
        lowered_reason = lowered_exit_reason
        if "stop" in lowered_reason:
            notifications.emit(
                NotificationType.STOP_TRIGGERED,
                severity="INFO",
                title=f"Stop triggered: {trade['ticker']}",
                reason_code="STOP_TRIGGERED",
                payload={"trade_id": trade_id, "realized_r": realized_r},
                dedupe_key=f"stop:{trade_id}",
            )
        elif "target" in lowered_reason:
            notifications.emit(
                NotificationType.TARGET_TRIGGERED,
                severity="INFO",
                title=f"Target triggered: {trade['ticker']}",
                reason_code="TARGET_TRIGGERED",
                payload={"trade_id": trade_id, "realized_r": realized_r},
                dedupe_key=f"target:{trade_id}",
            )
        notifications.emit(
            NotificationType.POSITION_CLOSED,
            severity="INFO",
            title=f"Position closed: {trade['ticker']}",
            reason_code="POSITION_CLOSED",
            payload={"trade_id": trade_id, "realized_r": realized_r, "exit_reason": reason_exit},
            dedupe_key=f"closed:{trade_id}",
        )
        description = f"Single-trade observation: {trade['setup_type']} in {trade['market_regime']} " f"was classified {classification.value} with realized result {realized_r:+.2f}R."
        self.database.create_observation(
            str(uuid4()),
            description,
            trade_id,
            confidence=0.1,
            strategy_version=trade["strategy_version"],
            applicable_setup=trade["setup_type"],
            applicable_regime=trade["market_regime"],
            evidence=record.evidence,
        )
        losses = self.database.consecutive_losses()
        if losses >= self.consecutive_loss_halt:
            self.database.activate_halt("loss_streak_halt", f"Loss streak reached {losses}", "automatic_postmortem")
            self.report_dir.mkdir(parents=True, exist_ok=True)
            report = self.report_dir / f"LOSS_STREAK_REVIEW_{datetime.now(timezone.utc).date().isoformat()}.md"
            report.write_text(
                "# Consecutive-Loss Review\n\n" f"Generated: {datetime.now(timezone.utc).isoformat()}\n\n" f"Consecutive completed losses: {losses}\n\n" "New entries remain latched off until explicit human review. Review setup quality, regime, " "execution, data integrity, and whether the production strategy should remain unchanged.\n",
                encoding="utf-8",
            )
        return record

    def _classify_with_llm(
        self,
        backend: StructuredModelBackend,
        model_name: str,
        trade: dict[str, Any],
        *,
        realized_r: float,
        mfe_r: float,
        mae_r: float,
        holding_days: float,
        reason_exit: str,
        mechanical: dict[str, bool | None],
    ) -> dict[str, Any]:
        known_lessons = self.database.validated_lessons_for(trade.get("setup_type"), trade.get("market_regime"))
        payload = {
            "candidate": {"candidate_id": trade.get("candidate_id")},
            "trade": {
                "ticker": trade.get("ticker"),
                "setup_type": trade.get("setup_type"),
                "market_regime": trade.get("market_regime"),
                "sector": trade.get("sector"),
                "entry_price": trade.get("entry_price"),
                "initial_stop": trade.get("initial_stop"),
                "target": trade.get("target"),
                "candidate_score": trade.get("candidate_score"),
                "bull_thesis": trade.get("bull_thesis"),
                "bear_thesis": trade.get("bear_thesis"),
                "stock_relative_strength": trade.get("stock_relative_strength"),
                "sector_relative_strength": trade.get("sector_relative_strength"),
            },
            "outcome": {
                "realized_r": realized_r,
                "mfe_r": mfe_r,
                "mae_r": mae_r,
                "holding_period_days": holding_days,
                "reason_exit": reason_exit,
            },
            "deterministic_process_checks": mechanical,
            "known_validated_lessons": [{"description": lesson["description"]} for lesson in known_lessons],
            "instruction": ("Judge only the subjective process questions from supplied data and do not override deterministic_process_checks. A loss does not automatically mean a bad trade, and a win does not automatically mean a good trade. If the trade matches a known validated lesson describing a failure pattern, set repeats_known_lesson=true and reference it in reasoning."),
        }
        result = backend.complete(role="postmortem", model_name=model_name, payload=payload, schema=PostmortemAssessment)
        parsed = result if isinstance(result, PostmortemAssessment) else PostmortemAssessment.model_validate(result)
        return parsed.model_dump(mode="json")

    @staticmethod
    def _result_driver(value: Any) -> str:
        normalized = str(value or "UNKNOWN").upper()
        return normalized if normalized in {"PROCESS", "LUCK", "MIXED", "UNKNOWN"} else "UNKNOWN"

    def _mechanical_assessment(self, trade: dict[str, Any]) -> dict[str, bool | None]:
        candidate: dict[str, Any] = {}
        if trade.get("candidate_id"):
            rows = self.database.rows(
                "SELECT context_json FROM candidate_scores WHERE candidate_id=?",
                (trade["candidate_id"],),
            )
            if rows:
                try:
                    candidate = json.loads(rows[0]["context_json"])
                except (TypeError, ValueError):
                    candidate = {}
        failures = candidate.get("validator_failures")
        original_setup_valid = not failures if isinstance(failures, list) else None
        scanner_correct = None
        if candidate:
            scanner_correct = bool(original_setup_valid and candidate.get("ticker") == trade.get("ticker") and candidate.get("setup_type") == trade.get("setup_type"))
        risk_payload: dict[str, Any] = {}
        try:
            risk_payload = json.loads(trade.get("risk_validation_result") or "{}")
        except (TypeError, ValueError):
            pass
        entry_valid = risk_payload.get("approved") if isinstance(risk_payload.get("approved"), bool) else None
        initial_risk = float(trade.get("initial_risk_dollars") or trade.get("planned_dollar_risk") or 0)
        planned_risk = float(trade.get("planned_dollar_risk") or 0)
        sizing_correct = initial_risk > 0 and planned_risk > 0 and initial_risk <= planned_risk + 1e-6
        structural_stop = candidate.get("structural_invalidation")
        stop_correct = abs(float(structural_stop) - float(trade["initial_stop"])) <= 1e-6 if structural_stop is not None else None
        execution_correct = None
        entry_slippage = trade.get("entry_slippage")
        if entry_slippage is not None:
            maximum = self.settings.maximum_entry_slippage_r if self.settings else 0.25
            risk_per_share = float(trade["entry_price"]) - float(trade["initial_stop"])
            execution_correct = risk_per_share > 0 and float(entry_slippage) / risk_per_share <= maximum
        earnings_gate_respected = None
        earnings_distance = trade.get("earnings_distance_at_entry")
        if candidate.get("is_etf"):
            earnings_gate_respected = True
        elif earnings_distance is not None:
            minimum = self.settings.earnings_exclusion_trading_days if self.settings else 5
            earnings_gate_respected = int(earnings_distance) > minimum
        return {
            "original_setup_valid": original_setup_valid,
            "scanner_correct": scanner_correct,
            "entry_valid": entry_valid,
            "sizing_correct": sizing_correct,
            "stop_placement_correct": stop_correct,
            "execution_correct": execution_correct,
            "earnings_gate_respected": earnings_gate_respected,
        }
