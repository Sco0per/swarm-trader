"""Cost-controlled, structured swing analysis pipeline."""

from __future__ import annotations

from typing import Any, Protocol, TypeVar
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from .config import SwingSettings
from .database import SwingDatabase
from .models import Decision, MarketRegime, SetupType, SwingCandidate, TradeProposal


class TechnicalSwingAnalysis(BaseModel):
    model_config = ConfigDict(extra="forbid")
    trend_structure: str
    setup_valid: bool
    entry: float = Field(gt=0)
    stop: float = Field(gt=0)
    target: float = Field(gt=0)
    support_resistance: str
    volume_analysis: str
    invalidation: str
    risks: list[str] = Field(default_factory=list)


class FundamentalEventAnalysis(BaseModel):
    model_config = ConfigDict(extra="forbid")
    earnings_trading_days: int | None = None
    has_blocking_event_risk: bool
    material_events: list[str] = Field(default_factory=list)
    fundamental_context: str
    data_gaps: list[str] = Field(default_factory=list)


class BullAnalysis(BaseModel):
    model_config = ConfigDict(extra="forbid")
    strongest_evidence: list[str]
    thesis: str
    confidence: float = Field(ge=0, le=100)


class BearAnalysis(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kill_trade: bool
    # Named red-team checklist: each question must be explicitly answered,
    # not left to free-text prose, so a weak trade cannot slip through on a
    # generic "looks fine" narrative.
    is_extended_from_support: bool
    is_chasing_price: bool
    regime_is_weak: bool
    sector_is_weakening: bool
    volume_is_unconvincing: bool
    support_is_weak: bool
    target_is_unrealistic: bool
    earnings_too_close: bool
    repeats_known_bad_pattern: bool
    fatal_reasons: list[str] = Field(default_factory=list)
    weaknesses: list[str]
    thesis: str
    confidence: float = Field(ge=0, le=100)


class PMStructuredDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ticker: str
    decision: Decision
    setup_type: SetupType
    entry: float = Field(gt=0)
    stop: float = Field(gt=0)
    target: float = Field(gt=0)
    confidence_score: float = Field(ge=0, le=100)
    market_regime: MarketRegime
    bull_case: str
    bear_case: str
    invalidation: str
    event_risk: str
    reasoning: str


SchemaT = TypeVar("SchemaT", bound=BaseModel)


class StructuredModelBackend(Protocol):
    """Provider-neutral adapter; implementations must request schema-constrained output."""

    def complete(self, *, role: str, model_name: str, payload: dict[str, Any], schema: type[SchemaT]) -> SchemaT | dict[str, Any]: ...


class ModelUnavailable(RuntimeError):
    pass


class AgentPipeline:
    """Use analyst models only after scanning, and PM models only for finalists."""

    SCHEMA_VERSION = "swing-agent-v1"

    def __init__(self, settings: SwingSettings, database: SwingDatabase, backend: StructuredModelBackend):
        self.settings = settings
        self.database = database
        self.backend = backend

    def _model(self, role: str) -> str:
        models = self.settings.models
        if role in {"technical", "fundamental"}:
            selected = models.analyst_model
        elif role in {"bull", "bear", "portfolio_manager"}:
            selected = models.portfolio_manager_model
        else:
            selected = None
        selected = selected or models.fallback_model
        if not selected:
            raise ModelUnavailable(f"No configured model for {role}; set the role model or MODEL_FALLBACK")
        return selected

    def _call(self, role: str, candidate: SwingCandidate, payload: dict[str, Any], schema: type[SchemaT]) -> SchemaT:
        model_name = self._model(role)
        decision_id = str(uuid4())
        try:
            result = self.backend.complete(role=role, model_name=model_name, payload=payload, schema=schema)
            parsed = result if isinstance(result, schema) else schema.model_validate(result)
            self.database.record_agent_decision(
                decision_id, role, self.SCHEMA_VERSION, parsed.model_dump(mode="json"), "VALID",
                candidate_id=candidate.candidate_id, model_name=model_name,
                decision=getattr(getattr(parsed, "decision", None), "value", None),
            )
            return parsed
        except Exception as exc:
            self.database.record_agent_decision(
                decision_id, role, self.SCHEMA_VERSION, {}, "INVALID",
                candidate_id=candidate.candidate_id, model_name=model_name, validation_error=str(exc),
            )
            raise

    def analyze(self, candidates: list[SwingCandidate]) -> list[TradeProposal]:
        """Return schema-valid BUY proposals; no model or invalid output means no trade."""
        # A model never gets to review a hard-failed or watchlist-only setup.
        screened = [
            candidate
            for candidate in candidates
            if candidate.score_route in {"strong", "very_strong"} and not candidate.validator_failures
        ]
        screened.sort(key=lambda candidate: (candidate.score_route != "very_strong", -candidate.score, candidate.ticker))
        finalists: list[tuple[SwingCandidate, TechnicalSwingAnalysis, FundamentalEventAnalysis, BullAnalysis, BearAnalysis]] = []
        for candidate in screened[: self.settings.maximum_scanner_candidates]:
            base = {"candidate": candidate.model_dump(mode="json"), "instruction": "Use only supplied data; identify unknowns explicitly."}
            try:
                technical = self._call("technical", candidate, base, TechnicalSwingAnalysis)
                fundamental = self._call("fundamental", candidate, base, FundamentalEventAnalysis)
                if not technical.setup_valid or fundamental.has_blocking_event_risk or fundamental.data_gaps:
                    continue
                bull = self._call("bull", candidate, {**base, "technical": technical.model_dump(), "fundamental": fundamental.model_dump()}, BullAnalysis)
                known_lessons = [
                    {"description": lesson["description"], "confidence": lesson["confidence"]}
                    for lesson in self.database.validated_lessons_for(candidate.setup_type.value, candidate.market_regime.value)
                ]
                bear = self._call(
                    "bear", candidate,
                    {**base, "technical": technical.model_dump(), "fundamental": fundamental.model_dump(), "bull": bull.model_dump(),
                     "known_lessons": known_lessons,
                     "instruction": (
                         "Act independently and kill weak, extended, event-exposed, or rationalized trades. "
                         "Answer every named checklist field explicitly. If known_lessons describes a validated "
                         "failure pattern this trade matches, set repeats_known_bad_pattern=true and kill_trade=true."
                     )},
                    BearAnalysis,
                )
                if bear.kill_trade:
                    continue
                finalists.append((candidate, technical, fundamental, bull, bear))
            except (ModelUnavailable, ValueError, TypeError, RuntimeError):
                continue

        proposals: list[TradeProposal] = []
        for candidate, technical, fundamental, bull, bear in finalists[: self.settings.maximum_pm_candidates]:
            payload = {
                "candidate": candidate.model_dump(mode="json"),
                "technical": technical.model_dump(),
                "fundamental": fundamental.model_dump(),
                "bull": bull.model_dump(),
                "bear": bear.model_dump(),
                "allowed_decisions": [decision.value for decision in Decision],
                "constraints": "Risk code has final authority. Do not choose quantity. NO_TRADE is acceptable.",
            }
            try:
                pm = self._call("portfolio_manager", candidate, payload, PMStructuredDecision)
                if pm.decision != Decision.BUY:
                    continue
                proposals.append(TradeProposal(
                    ticker=pm.ticker,
                    decision=pm.decision,
                    setup_type=pm.setup_type,
                    entry=pm.entry,
                    stop=pm.stop,
                    target=pm.target,
                    confidence_score=pm.confidence_score,
                    candidate_score=candidate.score,
                    market_regime=pm.market_regime,
                    bull_case=pm.bull_case,
                    bear_case=pm.bear_case,
                    invalidation=pm.invalidation,
                    event_risk=pm.event_risk,
                    strategy_version=self.settings.strategy_version,
                ))
            except (ModelUnavailable, ValueError, TypeError, RuntimeError):
                continue
        return proposals
