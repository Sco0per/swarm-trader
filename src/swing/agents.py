"""Cost-controlled, structured swing analysis pipeline."""

from __future__ import annotations

from typing import Any, Protocol, TypeVar
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from .config import SwingSettings
from .database import SwingDatabase
from .models import Decision, LLMReviewDecision, MarketRegime, SetupType, SwingCandidate, TradeProposal


class TechnicalSwingAnalysis(BaseModel):
    model_config = ConfigDict(extra="forbid")
    trend_structure: str
    setup_valid: bool
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
    decision: LLMReviewDecision
    setup_type: SetupType
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

    def complete(self, *, role: str, model_name: str, payload: dict[str, Any], schema: type[SchemaT]) -> SchemaT | dict[str, Any]:
        ...


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
        elif role in {"bear", "portfolio_manager"}:
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
                decision_id,
                role,
                self.SCHEMA_VERSION,
                parsed.model_dump(mode="json"),
                "VALID",
                candidate_id=candidate.candidate_id,
                model_name=model_name,
                decision=getattr(getattr(parsed, "decision", None), "value", None),
            )
            return parsed
        except Exception as exc:
            self.database.record_agent_decision(
                decision_id,
                role,
                self.SCHEMA_VERSION,
                {},
                "INVALID",
                candidate_id=candidate.candidate_id,
                model_name=model_name,
                validation_error=str(exc),
            )
            self.database.record_violation(
                "llm_schema",
                f"{role} output was unavailable or schema-invalid",
                ticker=candidate.ticker,
                blocked=True,
            )
            self.database.record_risk_rejection(
                reason_code="REJECT_LLM_SCHEMA",
                rule="llm_schema",
                reason=f"{role} output was unavailable or schema-invalid",
                ticker=candidate.ticker,
                decision_id=None,
                intent_id=None,
                candidate=candidate.model_dump(mode="json"),
                computed_values={"role": role},
            )
            raise

    def analyze(self, candidates: list[SwingCandidate]) -> list[TradeProposal]:
        """Return schema-valid BUY proposals; no model or invalid output means no trade."""
        # A model never gets to review a hard-failed or watchlist-only setup.
        screened = [candidate for candidate in candidates if candidate.score_route in {"strong", "very_strong"} and not candidate.validator_failures]
        screened.sort(key=lambda candidate: (candidate.score_route != "very_strong", -candidate.score, candidate.ticker))
        finalists: list[tuple[SwingCandidate, TechnicalSwingAnalysis, FundamentalEventAnalysis, BearAnalysis]] = []
        for candidate in screened[: self.settings.maximum_scanner_candidates]:
            identity = {
                "ticker": candidate.ticker,
                "setup_type": candidate.setup_type.value,
                "score": candidate.score,
                "market_regime": candidate.market_regime.value,
                "sector": candidate.sector,
            }
            technical_context = {
                **identity,
                "price": candidate.price,
                "atr": candidate.atr,
                "support": candidate.support,
                "resistance": candidate.resistance,
                "moving_averages": {"ma20": candidate.ma20, "ma50": candidate.ma50, "ma200": candidate.ma200},
                "relative_strength": {
                    "vs_spy": candidate.stock_relative_strength_spy,
                    "vs_sector": candidate.stock_relative_strength_sector,
                },
                "volume_ratio": candidate.volume_ratio,
                "validator_features": candidate.validator_features,
            }
            catalyst_context = {
                **identity,
                "earnings_trading_days": candidate.earnings_trading_days,
                "earnings_data_status": candidate.earnings_data_status,
                "major_event_status": candidate.major_event_status,
            }
            try:
                technical = self._call(
                    "technical",
                    candidate,
                    {"candidate": technical_context, "instruction": "Find technical contradictions and invalidation risks; identify unknowns explicitly."},
                    TechnicalSwingAnalysis,
                )
                fundamental = self._call(
                    "fundamental",
                    candidate,
                    {
                        "candidate": catalyst_context,
                        "instruction": (
                            "Act as the catalyst/fundamental analyst. Look for earnings, guidance, offerings, dilution, corporate actions, lawsuits, regulation, acquisitions, FDA events, executive departures, sector news, macro catalysts, and abnormal headline risk. Missing evidence is a data gap."
                        ),
                    },
                    FundamentalEventAnalysis,
                )
                if not technical.setup_valid or fundamental.has_blocking_event_risk or fundamental.data_gaps:
                    continue
                known_lessons = [{"description": lesson["description"], "confidence": lesson["confidence"]} for lesson in self.database.validated_lessons_for(candidate.setup_type.value, candidate.market_regime.value)]
                bear = self._call(
                    "bear",
                    candidate,
                    {
                        "candidate": identity,
                        "technical": technical.model_dump(),
                        "fundamental": fundamental.model_dump(),
                        "known_lessons": known_lessons,
                        "instruction": ("Act independently and kill weak, extended, event-exposed, or rationalized trades. " "Answer every named checklist field explicitly. If known_lessons describes a validated " "failure pattern this trade matches, set repeats_known_bad_pattern=true and kill_trade=true."),
                    },
                    BearAnalysis,
                )
                if bear.kill_trade:
                    continue
                finalists.append((candidate, technical, fundamental, bear))
            except (ModelUnavailable, ValueError, TypeError, RuntimeError):
                continue

        proposals: list[TradeProposal] = []
        for candidate, technical, fundamental, bear in finalists[: self.settings.maximum_pm_candidates]:
            payload = {
                "candidate": {
                    "ticker": candidate.ticker,
                    "setup_type": candidate.setup_type.value,
                    "score": candidate.score,
                    "market_regime": candidate.market_regime.value,
                    "sector": candidate.sector,
                },
                "technical": technical.model_dump(),
                "fundamental": fundamental.model_dump(),
                "bear": bear.model_dump(),
                "allowed_decisions": [decision.value for decision in LLMReviewDecision],
                "constraints": "This is advisory only. Python owns entry, stop, target, quantity, risk, execution, and every limit. APPROVE does not authorize execution.",
            }
            try:
                pm = self._call("portfolio_manager", candidate, payload, PMStructuredDecision)
                if pm.ticker.strip().upper() != candidate.ticker or pm.setup_type != candidate.setup_type or pm.market_regime != candidate.market_regime:
                    raise ValueError("Portfolio reviewer identity/setup/regime does not match deterministic candidate")
                if pm.decision != LLMReviewDecision.APPROVE:
                    continue
                proposals.append(
                    TradeProposal(
                        ticker=pm.ticker,
                        decision=Decision.BUY,
                        setup_type=pm.setup_type,
                        entry=candidate.price,
                        stop=candidate.structural_invalidation,
                        target=candidate.resistance,
                        confidence_score=pm.confidence_score,
                        candidate_score=candidate.score,
                        market_regime=pm.market_regime,
                        bull_case=pm.bull_case,
                        bear_case=pm.bear_case,
                        invalidation=pm.invalidation,
                        event_risk=pm.event_risk,
                        strategy_version=self.settings.strategy_version,
                    )
                )
            except (ModelUnavailable, ValueError, TypeError, RuntimeError):
                continue
        return proposals
