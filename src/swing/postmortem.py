"""Automatic close metrics and postmortem-to-observation workflow."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict

from .agents import StructuredModelBackend
from .database import SwingDatabase
from .models import PostmortemClassification, PostmortemRecord


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


class PostmortemEngine:
    def __init__(self, database: SwingDatabase, *, consecutive_loss_halt: int = 3, report_dir: Path | None = None):
        self.database = database
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
        realized_pnl = (exit_price - entry_price) * shares
        realized_r = (exit_price - entry_price) / initial_risk_per_share
        mfe = max(0.0, high_during_trade - entry_price)
        mae = max(0.0, entry_price - low_during_trade)
        entry_time = datetime.fromisoformat(trade["entry_datetime"])
        holding_days = (exit_datetime - entry_time).total_seconds() / 86_400
        self.database.update_trade(
            trade_id,
            status="CLOSED",
            exit_datetime=exit_datetime.isoformat(),
            exit_price=exit_price,
            realized_pnl=realized_pnl,
            realized_r=realized_r,
            mfe=mfe,
            mae=mae,
            mfe_r=mfe / initial_risk_per_share,
            mae_r=mae / initial_risk_per_share,
            holding_period_days=holding_days,
            reason_exit=reason_exit,
        )
        assessment = dict(assessment or {})
        if backend is not None and model_name and "classification" not in assessment:
            try:
                llm_assessment = self._classify_with_llm(
                    backend,
                    model_name,
                    trade,
                    realized_r=realized_r,
                    mfe_r=mfe / initial_risk_per_share,
                    mae_r=mae / initial_risk_per_share,
                    holding_days=holding_days,
                    reason_exit=reason_exit,
                )
                assessment = {**llm_assessment, **assessment}
            except Exception:
                # Fail closed to the deterministic default below; a postmortem
                # must always complete even if the model call fails.
                pass
        followed_strategy = bool(assessment.get("followed_strategy", True))
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
            entry_valid=bool(assessment.get("entry_valid", followed_strategy)),
            regime_correct=assessment.get("regime_correct"),
            sector_favorable=assessment.get("sector_favorable"),
            relative_strength_favorable=assessment.get("relative_strength_favorable"),
            stop_sensible=assessment.get("stop_sensible"),
            target_realistic=assessment.get("target_realistic"),
            execution_hurt=assessment.get("execution_hurt"),
            unexpected_event=assessment.get("unexpected_event"),
            rule_rationalization=bool(assessment.get("rule_rationalization", False)),
            mfe_r=mfe / initial_risk_per_share,
            mae_r=mae / initial_risk_per_share,
            normal_variance=normal_variance,
            mistake_type=assessment.get("mistake_type"),
            lessons=list(assessment.get("lessons", [])),
            evidence={
                "realized_r": realized_r,
                "mfe_r": mfe / initial_risk_per_share,
                "mae_r": mae / initial_risk_per_share,
                "holding_days": holding_days,
                "reason_exit": reason_exit,
                **dict(assessment.get("evidence", {})),
            },
        )
        self.database.record_postmortem(str(uuid4()), record, model_name)
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
            "known_validated_lessons": [{"description": lesson["description"]} for lesson in known_lessons],
            "instruction": ("Judge this closed trade honestly and only from the supplied data. A loss does not automatically " "mean a bad trade, and a win does not automatically mean a good trade -- judge process, not just " "outcome. If the trade matches a known validated lesson describing a failure pattern, set " "repeats_known_lesson=true and reference it in reasoning."),
        }
        result = backend.complete(role="postmortem", model_name=model_name, payload=payload, schema=PostmortemAssessment)
        parsed = result if isinstance(result, PostmortemAssessment) else PostmortemAssessment.model_validate(result)
        return parsed.model_dump(mode="json")
