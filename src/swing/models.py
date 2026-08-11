"""Schema-validated records passed between swing components."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class SetupType(str, Enum):
    TREND_PULLBACK = "TREND_PULLBACK"
    BREAKOUT_RETEST = "BREAKOUT_RETEST"
    RELATIVE_STRENGTH_CONTINUATION = "RELATIVE_STRENGTH_CONTINUATION"


class MarketRegime(str, Enum):
    STRONG_BULL = "STRONG_BULL"
    BULL = "BULL"
    NEUTRAL = "NEUTRAL"
    CHOPPY = "CHOPPY"
    BEAR = "BEAR"
    HIGH_VOLATILITY_RISK_OFF = "HIGH_VOLATILITY_RISK_OFF"


class Decision(str, Enum):
    BUY = "BUY"
    WATCH = "WATCH"
    HOLD = "HOLD"
    EXIT = "EXIT"
    NO_TRADE = "NO_TRADE"


class LLMReviewDecision(str, Enum):
    """Advisory-only model verdicts; none of these authorize an order."""

    APPROVE = "APPROVE"
    REJECT = "REJECT"
    WATCH = "WATCH"


class TradeStatus(str, Enum):
    PLANNED = "PLANNED"
    SUBMITTED = "SUBMITTED"
    OPEN = "OPEN"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    CLOSED = "CLOSED"
    CANCELED = "CANCELED"
    REJECTED = "REJECTED"


class PositionLifecycleState(str, Enum):
    OPEN = "OPEN"
    PROTECTED = "PROTECTED"
    PROFITABLE = "PROFITABLE"
    TRAILING = "TRAILING"
    EXIT_PENDING = "EXIT_PENDING"
    CLOSED = "CLOSED"


class TimeStopAction(str, Enum):
    HOLD = "HOLD"
    REVIEW = "REVIEW"
    EXIT = "EXIT"


class LessonStatus(str, Enum):
    OBSERVATION = "OBSERVATION"
    PROVISIONAL = "PROVISIONAL"
    VALIDATED = "VALIDATED"
    REJECTED = "REJECTED"
    RETIRED = "RETIRED"


class PostmortemClassification(str, Enum):
    VALID_LOSS = "VALID_LOSS"
    VALID_WIN = "VALID_WIN"
    BAD_SETUP_SELECTION = "BAD_SETUP_SELECTION"
    BAD_ENTRY = "BAD_ENTRY"
    BAD_STOP = "BAD_STOP"
    BAD_MARKET_REGIME = "BAD_MARKET_REGIME"
    BAD_SECTOR_CONTEXT = "BAD_SECTOR_CONTEXT"
    EVENT_RISK_ERROR = "EVENT_RISK_ERROR"
    OVEREXTENDED_ENTRY = "OVEREXTENDED_ENTRY"
    RULE_VIOLATION = "RULE_VIOLATION"
    EXECUTION_ERROR = "EXECUTION_ERROR"
    DATA_ERROR = "DATA_ERROR"
    UNKNOWN = "UNKNOWN"


class TimestampedData(BaseModel):
    source: str = Field(min_length=1)
    retrieved_at: datetime
    market_timestamp: datetime | None = None

    @field_validator("retrieved_at", "market_timestamp")
    @classmethod
    def timezone_required(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("market data timestamps must be timezone-aware")
        return value


class Quote(TimestampedData):
    symbol: str
    last: float = Field(gt=0)
    bid: float | None = Field(default=None, gt=0)
    ask: float | None = Field(default=None, gt=0)

    @property
    def spread_pct(self) -> float | None:
        if self.bid is None or self.ask is None:
            return None
        midpoint = (self.bid + self.ask) / 2
        return (self.ask - self.bid) / midpoint if midpoint else None


class SwingCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidate_id: str = Field(default_factory=lambda: str(uuid4()))
    ticker: str
    setup_type: SetupType
    score: float = Field(ge=0, le=100)
    score_components: dict[str, float]
    score_route: Literal["reject", "watchlist", "strong", "very_strong"] = "strong"
    validator_features: dict[str, Any] = Field(default_factory=dict)
    validator_failures: list[str] = Field(default_factory=list)
    market_regime: MarketRegime
    sector: str
    sector_etf: str | None = None
    sector_trend: str
    sector_relative_strength: float
    stock_relative_strength_spy: float
    stock_relative_strength_sector: float | None = None
    price: float = Field(gt=0)
    average_daily_volume: float = Field(gt=0)
    spread_pct: float | None = Field(default=None, ge=0)
    rsi: float | None = None
    macd: float | None = None
    atr: float = Field(gt=0)
    adx: float | None = None
    ma20: float | None = None
    ma50: float | None = None
    ma200: float | None = None
    volume_ratio: float | None = None
    support: float = Field(gt=0)
    resistance: float = Field(gt=0)
    structural_invalidation: float = Field(gt=0)
    major_event_status: Literal["clear", "blocked", "unknown", "stale", "conflicting", "malformed"] = "unknown"
    major_event_retrieved_at: datetime | None = None
    earnings_trading_days: int | None = None
    earnings_data_status: Literal["clear", "unavailable", "stale", "conflicting", "malformed"] = "unavailable"
    earnings_retrieved_at: datetime | None = None
    completed_daily_bar: bool = True
    is_etf: bool = False
    is_leveraged_or_inverse: bool = False
    is_halted: bool = False
    data: TimestampedData

    @field_validator("ticker")
    @classmethod
    def normalize_symbol(cls, value: str) -> str:
        value = value.strip().upper()
        if not value or not value.replace(".", "").replace("-", "").isalnum():
            raise ValueError("invalid ticker")
        return value

    @field_validator("major_event_retrieved_at", "earnings_retrieved_at")
    @classmethod
    def safety_timestamp_timezone_required(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("safety-data timestamps must be timezone-aware")
        return value


class TradeProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision_id: str = Field(default_factory=lambda: str(uuid4()))
    ticker: str
    decision: Decision
    setup_type: SetupType
    entry: float = Field(gt=0)
    stop: float = Field(gt=0)
    target: float = Field(gt=0)
    confidence_score: float = Field(ge=0, le=100)
    candidate_score: float = Field(ge=0, le=100)
    market_regime: MarketRegime
    bull_case: str
    bear_case: str
    invalidation: str
    event_risk: str
    strategy_version: str
    exceptional_rr_evidence: bool = False
    requested_risk_pct: float | None = Field(default=None, gt=0)
    reentry_context: dict[str, str] | None = None

    @field_validator("ticker")
    @classmethod
    def normalize_symbol(cls, value: str) -> str:
        return value.strip().upper()

    @model_validator(mode="after")
    def long_geometry(self) -> "TradeProposal":
        if self.decision == Decision.BUY and not (self.stop < self.entry < self.target):
            raise ValueError("long proposal requires stop < entry < target")
        return self

    @property
    def planned_rr(self) -> float:
        return (self.target - self.entry) / (self.entry - self.stop)


class Position(BaseModel):
    symbol: str
    quantity: float
    average_entry_price: float
    current_price: float
    market_value: float
    side: str = "long"
    initial_stop: float | None = None
    current_stop: float | None = None
    planned_open_risk: float = 0.0


class BrokerAsset(BaseModel):
    symbol: str
    asset_class: str
    tradable: bool
    status: str
    marginable: bool | None = None
    is_leveraged_or_inverse: bool | None = None
    retrieved_at: datetime = Field(default_factory=utc_now)
    raw: dict[str, Any] = Field(default_factory=dict)

    @field_validator("retrieved_at")
    @classmethod
    def asset_timestamp_timezone_required(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("broker asset timestamp must be timezone-aware")
        return value


class AccountSnapshot(BaseModel):
    account_id: str
    equity: float = Field(gt=0)
    cash: float = Field(ge=0)
    buying_power: float = Field(ge=0)
    is_paper: bool
    is_margin_enabled: bool = False
    dedicated_agentic_account: bool | None = None
    retrieved_at: datetime = Field(default_factory=utc_now)


class PortfolioRiskState(BaseModel):
    account: AccountSnapshot
    positions: list[Position] = Field(default_factory=list)
    open_orders: list[dict[str, Any]] = Field(default_factory=list)
    equity_high: float
    drawdown_pct: float = 0.0
    weekly_drawdown_pct: float = 0.0
    planned_open_risk: float = 0.0
    new_positions_today: int = 0
    new_positions_this_week: int = 0
    consecutive_losses: int = 0
    drawdown_halt_latched: bool = False
    loss_streak_halt_latched: bool = False
    reconciliation_halt_latched: bool = False
    kill_switch: bool = False


class RiskDecision(BaseModel):
    approved: bool
    reason: str
    rule: str | None = None
    reason_code: str | None = None
    quantity: int = 0
    allowed_dollar_risk: float = 0.0
    planned_dollar_risk: float = 0.0
    planned_account_risk_pct: float = 0.0
    planned_rr: float = 0.0
    applied_risk_pct: float = 0.0
    authoritative_stop: float | None = None
    risk_constraints: dict[str, int] = Field(default_factory=dict)


class OrderIntent(BaseModel):
    intent_id: str
    decision_id: str
    ticker: str
    side: str = "buy"
    quantity: int = Field(gt=0)
    entry_type: str = "limit"
    limit_price: float = Field(gt=0)
    stop_price: float = Field(gt=0)
    target_price: float = Field(gt=0)
    strategy_version: str
    created_at: datetime = Field(default_factory=utc_now)


class TechnicalThesis(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    trend: str
    support: float = Field(gt=0)
    trigger: float = Field(gt=0)


class TradeThesis(BaseModel):
    """Write-once contract describing why the original position was opened."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    ticker: str
    setup: SetupType
    entry: float = Field(gt=0)
    initial_stop: float = Field(gt=0)
    initial_target: float = Field(gt=0)
    initial_risk_dollars: float = Field(gt=0)
    initial_risk_per_share: float = Field(gt=0)
    initial_rr: float = Field(ge=0)
    entry_score: float = Field(ge=0, le=100)
    market_regime: str
    expected_hold_days: int = Field(gt=0)
    max_hold_days: int = Field(gt=0)
    technical_thesis: TechnicalThesis
    invalidations: tuple[str, ...] = Field(min_length=1)

    @field_validator("ticker")
    @classmethod
    def normalize_thesis_symbol(cls, value: str) -> str:
        return value.strip().upper()

    @model_validator(mode="after")
    def valid_contract(self) -> "TradeThesis":
        if not self.initial_stop < self.entry < self.initial_target:
            raise ValueError("thesis requires initial_stop < entry < initial_target")
        if self.expected_hold_days > self.max_hold_days:
            raise ValueError("expected_hold_days cannot exceed max_hold_days")
        return self


class BrokerOrder(BaseModel):
    broker_order_id: str
    intent_id: str
    symbol: str
    side: str
    quantity: float
    status: str
    filled_quantity: float = 0.0
    average_fill_price: float | None = None
    submitted_at: datetime = Field(default_factory=utc_now)
    raw: dict[str, Any] = Field(default_factory=dict)


class PostmortemRecord(BaseModel):
    trade_id: str
    classification: PostmortemClassification
    followed_strategy: bool
    entry_valid: bool
    regime_correct: bool | None = None
    sector_favorable: bool | None = None
    relative_strength_favorable: bool | None = None
    stop_sensible: bool | None = None
    target_realistic: bool | None = None
    execution_hurt: bool | None = None
    unexpected_event: bool | None = None
    rule_rationalization: bool = False
    mfe_r: float | None = None
    mae_r: float | None = None
    normal_variance: bool = False
    mistake_type: str | None = None
    lessons: list[str] = Field(default_factory=list)
    evidence: dict[str, Any] = Field(default_factory=dict)
