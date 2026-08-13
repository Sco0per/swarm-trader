"""Typed configuration with fail-closed defaults for the swing system."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

ROOT = Path(__file__).resolve().parents[2]


def _bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean (true/false)")


def _float(name: str, default: float) -> float:
    value = os.getenv(name)
    if value in (None, ""):
        return default
    try:
        return float(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number") from exc


def _int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value in (None, ""):
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc


@dataclass(frozen=True)
class ModelSettings:
    scanner_model: str | None = field(default_factory=lambda: os.getenv("SCANNER_MODEL") or None)
    analyst_model: str | None = field(default_factory=lambda: os.getenv("ANALYST_MODEL") or None)
    portfolio_manager_model: str | None = field(default_factory=lambda: os.getenv("PORTFOLIO_MANAGER_MODEL") or None)
    postmortem_model: str | None = field(default_factory=lambda: os.getenv("POSTMORTEM_MODEL") or None)
    research_model: str | None = field(default_factory=lambda: os.getenv("RESEARCH_MODEL") or None)
    fallback_model: str | None = field(default_factory=lambda: os.getenv("MODEL_FALLBACK") or None)


@dataclass(frozen=True)
class TelegramSettings:
    """Optional outbound notification channel; unset means delivery is disabled."""

    bot_token: str | None = field(default_factory=lambda: os.getenv("TELEGRAM_BOT_TOKEN") or None)
    chat_id: str | None = field(default_factory=lambda: os.getenv("TELEGRAM_CHAT_ID") or None)


@dataclass(frozen=True)
class ScheduleSettings:
    """Named-zone schedule values owned by the central settings model."""

    timezone_name: str = "America/New_York"
    initial_scan_et: str = "09:35"
    decision_cycle_et: str = "10:15"
    midday_refresh_et: str = "12:30"
    afternoon_refresh_et: str = "14:30"
    position_health_et: str = "15:45"
    daily_report_et: str = "16:15"
    weekly_lessons_et: str = "Sunday 18:00"

    def __post_init__(self) -> None:
        if self.timezone_name != "America/New_York":
            raise ValueError("SCHEDULE_TIMEZONE must be America/New_York")
        weekday = re.compile(r"^(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday) (?:[01]\d|2[0-3]):[0-5]\d$")
        clock = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
        for name in ("initial_scan_et", "decision_cycle_et", "midday_refresh_et", "afternoon_refresh_et", "position_health_et", "daily_report_et"):
            if not clock.fullmatch(getattr(self, name)):
                raise ValueError(f"{name.upper()} must be a valid 24-hour ET time")
        if not weekday.fullmatch(self.weekly_lessons_et):
            raise ValueError("WEEKLY_LESSONS_ET must be '<weekday> HH:MM'")


@dataclass(frozen=True)
class EntryScoreWeights:
    """Researchable score weights; startup requires an exact 100-point scale."""

    setup_quality: float = 30.0
    relative_strength: float = 20.0
    trend_quality: float = 15.0
    volume_confirmation: float = 10.0
    market_regime: float = 10.0
    sector_strength: float = 5.0
    risk_reward: float = 10.0

    def as_dict(self) -> dict[str, float]:
        return {
            "setup_quality": self.setup_quality,
            "relative_strength": self.relative_strength,
            "trend_quality": self.trend_quality,
            "volume_confirmation": self.volume_confirmation,
            "market_regime": self.market_regime,
            "sector_strength": self.sector_strength,
            "risk_reward": self.risk_reward,
        }

    def __post_init__(self) -> None:
        values = self.as_dict()
        if any(value < 0 or value > 50 for value in values.values()):
            raise ValueError("Entry score weights must each remain between 0 and 50")
        if abs(sum(values.values()) - 100.0) > 1e-9:
            raise ValueError("Entry score weights must total exactly 100")


@dataclass(frozen=True)
class StrategySettings:
    """Bounded deterministic setup parameters (research defaults, not truths)."""

    short_ema_period: int = 20
    atr_period: int = 14
    medium_ma_period: int = 50
    long_ma_period: int = 150
    trend_slope_lookback: int = 10
    minimum_trend_slope: float = 0.0
    relative_strength_lookback: int = 63
    minimum_spy_relative_strength: float = 0.0
    strong_spy_relative_strength: float = 0.03
    minimum_sector_relative_strength: float = 0.0
    pullback_max_atr_distance: float = 1.25
    structural_break_atr: float = 0.5
    minimum_atr_pct: float = 0.005
    maximum_atr_pct: float = 0.08
    consolidation_sessions: int = 20
    consolidation_max_range_pct: float = 0.12
    breakout_buffer_pct: float = 0.003
    breakout_volume_ratio: float = 1.20
    retest_hold_tolerance_pct: float = 0.01
    retest_collapse_tolerance_pct: float = 0.03
    maximum_breakout_extension_atr: float = 1.25
    rs_consolidation_sessions: int = 10
    rs_consolidation_max_atr: float = 4.0
    contraction_volume_ratio: float = 0.90
    confirmation_volume_ratio: float = 1.10
    trend_target_atr_extension: float = 3.0
    measured_move_multiple: float = 2.5
    maximum_bar_age_calendar_days: int = 7

    def __post_init__(self) -> None:
        bounded: Mapping[str, tuple[float, float, float]] = {
            "short_ema_period": (self.short_ema_period, 10, 30),
            "atr_period": (self.atr_period, 10, 30),
            "medium_ma_period": (self.medium_ma_period, 40, 75),
            "long_ma_period": (self.long_ma_period, 100, 200),
            "trend_slope_lookback": (self.trend_slope_lookback, 5, 30),
            "minimum_trend_slope": (self.minimum_trend_slope, 0, 0.05),
            "relative_strength_lookback": (self.relative_strength_lookback, 40, 126),
            "minimum_spy_relative_strength": (self.minimum_spy_relative_strength, 0, 0.20),
            "strong_spy_relative_strength": (self.strong_spy_relative_strength, 0.01, 0.30),
            "minimum_sector_relative_strength": (self.minimum_sector_relative_strength, 0, 0.20),
            "pullback_max_atr_distance": (self.pullback_max_atr_distance, 0.25, 2.0),
            "structural_break_atr": (self.structural_break_atr, 0, 2.0),
            "minimum_atr_pct": (self.minimum_atr_pct, 0, 0.03),
            "maximum_atr_pct": (self.maximum_atr_pct, 0.03, 0.15),
            "consolidation_sessions": (self.consolidation_sessions, 10, 40),
            "consolidation_max_range_pct": (self.consolidation_max_range_pct, 0.03, 0.25),
            "breakout_buffer_pct": (self.breakout_buffer_pct, 0, 0.03),
            "breakout_volume_ratio": (self.breakout_volume_ratio, 1.0, 3.0),
            "retest_hold_tolerance_pct": (self.retest_hold_tolerance_pct, 0, 0.05),
            "retest_collapse_tolerance_pct": (self.retest_collapse_tolerance_pct, 0, 0.10),
            "maximum_breakout_extension_atr": (self.maximum_breakout_extension_atr, 0.25, 3.0),
            "rs_consolidation_sessions": (self.rs_consolidation_sessions, 5, 30),
            "rs_consolidation_max_atr": (self.rs_consolidation_max_atr, 1.5, 8.0),
            "contraction_volume_ratio": (self.contraction_volume_ratio, 0.5, 1.1),
            "confirmation_volume_ratio": (self.confirmation_volume_ratio, 0.8, 3.0),
            "trend_target_atr_extension": (self.trend_target_atr_extension, 0.5, 5.0),
            "measured_move_multiple": (self.measured_move_multiple, 0.5, 3.0),
            "maximum_bar_age_calendar_days": (self.maximum_bar_age_calendar_days, 1, 10),
        }
        for name, (value, lower, upper) in bounded.items():
            if not lower <= value <= upper:
                raise ValueError(f"{name} must remain between {lower} and {upper}")
        if not self.short_ema_period < self.medium_ma_period < self.long_ma_period:
            raise ValueError("Strategy moving-average periods must be strictly ordered")
        if self.minimum_atr_pct >= self.maximum_atr_pct:
            raise ValueError("Strategy ATR bounds must be strictly ordered")
        if self.strong_spy_relative_strength < self.minimum_spy_relative_strength:
            raise ValueError("Strong relative strength cannot be below the minimum")


@dataclass(frozen=True)
class SwingSettings:
    trading_style: str = "swing"
    execution_mode: str = "paper"
    trading_enabled: bool = False
    live_acknowledgement: str = ""
    watchdog_auto_push_enabled: bool = False
    strategy_version: str = "SWING_V1.0"
    config_version: str = "SWING_CONFIG_V1"
    scanner_version: str = "deterministic-swing-scanner-v2"
    database_path: Path = ROOT / "data" / "adaptive_swing.db"
    do_not_trade_path: Path = ROOT / "config" / "do_not_trade.yaml"
    universe_path: Path = ROOT / "config" / "universe" / "us_liquid_2026-08-11.csv"

    normal_risk_pct: float = 0.0075
    absolute_max_risk_pct: float = 0.01
    reduced_risk_pct: float = 0.003
    reduce_risk_drawdown_pct: float = 0.05
    halt_drawdown_pct: float = 0.08
    max_combined_open_risk_pct: float = 0.02
    max_sector_open_risk_pct: float = 0.015
    max_cluster_open_risk_pct: float = 0.01
    max_position_exposure_pct: float = 0.35
    max_open_positions: int = 3
    max_new_positions_day: int = 1
    max_new_positions_week: int = 3
    consecutive_loss_halt: int = 3
    weekly_drawdown_halt_pct: float = 0.04

    minimum_stop_atr: float = 0.75
    maximum_stop_atr: float = 3.0
    maximum_adv_fraction: float = 0.001
    broker_min_quantity: int = 1

    minimum_rr: float = 2.0
    buy_score_threshold: float = 80.0
    preferred_score_threshold: float = 85.0
    a_plus_score_threshold: float = 90.0
    minimum_price: float = 5.0
    minimum_average_volume: int = 1_000_000
    minimum_average_dollar_volume: float = 20_000_000
    minimum_history_sessions: int = 160
    maximum_spread_pct: float = 0.005
    borderline_score_threshold: float = 70.0
    neutral_score_threshold_addition: float = 5.0
    choppy_score_threshold_addition: float = 10.0
    hostile_score_threshold_addition: float = 20.0
    neutral_risk_multiplier: float = 0.75
    choppy_risk_multiplier: float = 0.50
    hostile_risk_multiplier: float = 0.25
    earnings_exclusion_trading_days: int = 5
    quote_freshness_seconds: int = 300
    broker_asset_freshness_seconds: int = 60
    market_clock_freshness_seconds: int = 60
    corporate_event_freshness_seconds: int = 86_400
    earnings_freshness_seconds: int = 86_400
    maximum_entry_slippage_r: float = 0.25
    expected_hold_days: int = 4
    maximum_hold_days: int = 10
    time_stop_minimum_progress_r: float = 0.50
    time_stop_mode: str = "REVIEW"
    stop_management_policy: str = "STRUCTURE_THEN_TRAIL"
    profitable_trigger_r: float = 1.0
    trailing_activation_r: float = 2.0
    trailing_distance_r: float = 0.75
    move_to_breakeven_at_r: float | None = None
    trailing_eligible_setups: tuple[str, ...] = ("TREND_PULLBACK", "RELATIVE_STRENGTH_CONTINUATION")
    cooldown_trading_days: int = 10
    minimum_statistical_sample: int = 30
    llm_material_score_change: float = 5.0
    maximum_scanner_candidates: int = 20
    maximum_pm_candidates: int = 5
    models: ModelSettings = field(default_factory=ModelSettings)
    telegram: TelegramSettings = field(default_factory=TelegramSettings)
    schedule: ScheduleSettings = field(default_factory=ScheduleSettings)
    strategy: StrategySettings = field(default_factory=StrategySettings)
    score_weights: EntryScoreWeights = field(default_factory=EntryScoreWeights)

    def __post_init__(self) -> None:
        if self.trading_style != "swing":
            raise ValueError("Only TRADING_STYLE=swing is supported")
        if self.execution_mode not in {"paper", "live"}:
            raise ValueError("EXECUTION_MODE must be paper or live")
        freshness_bounds = {
            "QUOTE_FRESHNESS_SECONDS": (self.quote_freshness_seconds, 300),
            "BROKER_ASSET_FRESHNESS_SECONDS": (self.broker_asset_freshness_seconds, 60),
            "MARKET_CLOCK_FRESHNESS_SECONDS": (self.market_clock_freshness_seconds, 60),
            "CORPORATE_EVENT_FRESHNESS_SECONDS": (self.corporate_event_freshness_seconds, 86_400),
            "EARNINGS_FRESHNESS_SECONDS": (self.earnings_freshness_seconds, 86_400),
        }
        for name, (value, maximum) in freshness_bounds.items():
            if not 1 <= value <= maximum:
                raise ValueError(f"{name} must be between 1 and {maximum}")
        if not 0 <= self.maximum_entry_slippage_r <= 0.25:
            raise ValueError("MAXIMUM_ENTRY_SLIPPAGE_R may not exceed the immutable 0.25R ceiling")
        if not 1 <= self.expected_hold_days <= 4:
            raise ValueError("EXPECTED_HOLD_DAYS must be between 1 and the immutable 4-day ceiling")
        if not self.expected_hold_days <= self.maximum_hold_days <= 10:
            raise ValueError("MAXIMUM_HOLD_DAYS must be at least EXPECTED_HOLD_DAYS and no greater than 10")
        if self.time_stop_mode not in {"REVIEW", "EXIT"}:
            raise ValueError("TIME_STOP_MODE must be REVIEW or EXIT")
        if self.stop_management_policy not in {"STRUCTURAL_ONLY", "STRUCTURE_THEN_TRAIL"}:
            raise ValueError("Unsupported STOP_MANAGEMENT_POLICY")
        if not (0.5 <= self.time_stop_minimum_progress_r <= 2):
            raise ValueError("TIME_STOP_MINIMUM_PROGRESS_R must remain between 0.50 and 2.00R")
        if not (0 < self.profitable_trigger_r <= 1.0):
            raise ValueError("PROFITABLE_TRIGGER_R must be positive and cannot exceed 1.00R")
        if not (self.profitable_trigger_r <= self.trailing_activation_r <= 2.0):
            raise ValueError("TRAILING_ACTIVATION_R must be ordered and cannot exceed 2.00R")
        if not 0 < self.trailing_distance_r <= 0.75:
            raise ValueError("TRAILING_DISTANCE_R must be positive and cannot exceed 0.75R")
        if self.move_to_breakeven_at_r is not None and not 0 < self.move_to_breakeven_at_r <= 2.0:
            raise ValueError("MOVE_TO_BREAKEVEN_AT_R must be between 0 and 2.00R when configured")
        valid_setups = {"TREND_PULLBACK", "BREAKOUT_RETEST", "RELATIVE_STRENGTH_CONTINUATION"}
        if not set(self.trailing_eligible_setups).issubset(valid_setups):
            raise ValueError("TRAILING_ELIGIBLE_SETUPS contains an invalid setup")
        if not (0 < self.normal_risk_pct <= 0.0075):
            raise ValueError("NORMAL_RISK_PCT may not exceed the immutable 0.75% ceiling")
        if self.absolute_max_risk_pct != 0.01:
            raise ValueError("ABSOLUTE_MAX_RISK_PCT is immutable at 1.00%")
        if not (0.0025 <= self.reduced_risk_pct <= 0.0035):
            raise ValueError("REDUCED_RISK_PCT must remain between 0.25% and 0.35%")
        if self.reduce_risk_drawdown_pct > 0.05 or self.halt_drawdown_pct > 0.08:
            raise ValueError("Drawdown controls may be stricter, but not weaker than 5%/8%")
        if not (0 < self.reduce_risk_drawdown_pct < self.halt_drawdown_pct):
            raise ValueError("Drawdown thresholds must be positive and ordered")
        if not (0 < self.max_combined_open_risk_pct <= 0.02):
            raise ValueError("MAX_COMBINED_OPEN_RISK_PCT may not exceed 2.00%")
        if not (0 < self.max_sector_open_risk_pct <= 0.015):
            raise ValueError("MAX_SECTOR_OPEN_RISK_PCT may not exceed the immutable 1.50% ceiling")
        if not (0 < self.max_cluster_open_risk_pct <= 0.01):
            raise ValueError("MAX_CLUSTER_OPEN_RISK_PCT may not exceed the immutable 1.00% ceiling")
        if not (0 < self.max_position_exposure_pct <= 0.35):
            raise ValueError("MAX_POSITION_EXPOSURE_PCT may not exceed 35%")
        if not (1 <= self.max_open_positions <= 3):
            raise ValueError("MAX_OPEN_POSITIONS must be between one and three")
        if not (1 <= self.max_new_positions_day <= 1) or not (1 <= self.max_new_positions_week <= 3):
            raise ValueError("Entry limits may not exceed one per day and three per week")
        if not (1 <= self.consecutive_loss_halt <= 3):
            raise ValueError("CONSECUTIVE_LOSS_HALT may not exceed three losses")
        if not (0 < self.weekly_drawdown_halt_pct <= 0.05):
            raise ValueError("WEEKLY_DRAWDOWN_HALT_PCT may not exceed the immutable 5.00% ceiling")
        if not (0.5 <= self.minimum_stop_atr <= 1.5):
            raise ValueError("MINIMUM_STOP_ATR must remain between 0.50 and 1.50 ATR")
        if not (1.5 <= self.maximum_stop_atr <= 4.0):
            raise ValueError("MAXIMUM_STOP_ATR must remain between 1.50 and 4.00 ATR")
        if self.minimum_stop_atr >= self.maximum_stop_atr:
            raise ValueError("ATR stop sanity bounds must be strictly ordered")
        if not (0 < self.maximum_adv_fraction <= 0.01):
            raise ValueError("MAXIMUM_ADV_FRACTION may not exceed the immutable 1.00% ceiling")
        if self.broker_min_quantity != 1:
            raise ValueError("BROKER_MIN_QUANTITY is immutable at one whole share")
        if self.buy_score_threshold < 80 or not (self.buy_score_threshold <= self.preferred_score_threshold <= self.a_plus_score_threshold <= 100):
            raise ValueError("Score thresholds must be ordered and BUY_SCORE_THRESHOLD cannot be below 80")
        if not (0 <= self.borderline_score_threshold < self.buy_score_threshold):
            raise ValueError("BORDERLINE_SCORE_THRESHOLD must be below BUY_SCORE_THRESHOLD")
        if self.minimum_rr < 2.0:
            raise ValueError("MINIMUM_RR may not be below 2.00R")
        if self.minimum_price < 5 or self.minimum_average_volume < 1_000_000 or not 20_000_000 <= self.minimum_average_dollar_volume <= 5_000_000_000:
            raise ValueError("Liquidity filters may be stricter, but not weaker than the production floors")
        if not 120 <= self.minimum_history_sessions <= 200:
            raise ValueError("MINIMUM_HISTORY_SESSIONS must remain between 120 and 200")
        if self.minimum_history_sessions < self.strategy.long_ma_period:
            raise ValueError("MINIMUM_HISTORY_SESSIONS must cover the configured long moving average")
        if not (0 < self.maximum_spread_pct <= 0.005):
            raise ValueError("MAXIMUM_SPREAD_PCT must be positive and cannot exceed 0.50%")
        if self.earnings_exclusion_trading_days < 5 or self.cooldown_trading_days < 10:
            raise ValueError("Event and post-loss exclusions may not be weakened")
        if self.minimum_statistical_sample < 30:
            raise ValueError("MINIMUM_STATISTICAL_SAMPLE may not be below 30")
        if not 1 <= self.llm_material_score_change <= 20:
            raise ValueError("LLM_MATERIAL_SCORE_CHANGE must remain between 1 and 20 points")
        if not (1 <= self.maximum_scanner_candidates <= 20) or not (1 <= self.maximum_pm_candidates <= 5):
            raise ValueError("Candidate limits must be positive and may not exceed 20/5")
        additions = (self.neutral_score_threshold_addition, self.choppy_score_threshold_addition, self.hostile_score_threshold_addition)
        if not (0 <= additions[0] <= additions[1] <= additions[2] <= 30):
            raise ValueError("Regime score additions must be nonnegative, ordered, and no greater than 30")
        multipliers = (self.neutral_risk_multiplier, self.choppy_risk_multiplier, self.hostile_risk_multiplier)
        if not (0 < multipliers[2] <= multipliers[1] <= multipliers[0] <= 1):
            raise ValueError("Regime risk multipliers may only reduce baseline risk")
        if self.execution_mode == "live" and self.trading_enabled and self.live_acknowledgement != "I_ACKNOWLEDGE_LIVE_RISK":
            raise ValueError("Live execution requires LIVE_TRADING_ACK=I_ACKNOWLEDGE_LIVE_RISK")


def load_settings() -> SwingSettings:
    """Load settings from the environment; all unsafe switches default off."""
    return SwingSettings(
        trading_style=os.getenv("TRADING_STYLE", "swing").lower(),
        execution_mode=os.getenv("EXECUTION_MODE", "paper").lower(),
        trading_enabled=_bool("TRADING_ENABLED", False),
        live_acknowledgement=os.getenv("LIVE_TRADING_ACK", ""),
        watchdog_auto_push_enabled=_bool("WATCHDOG_AUTO_PUSH_ENABLED", False),
        strategy_version=os.getenv("STRATEGY_VERSION", "SWING_V1.0"),
        config_version=os.getenv("CONFIG_VERSION", "SWING_CONFIG_V1"),
        scanner_version=os.getenv("SCANNER_VERSION", "deterministic-swing-scanner-v2"),
        database_path=Path(os.getenv("SWING_DATABASE_PATH", str(ROOT / "data" / "adaptive_swing.db"))),
        do_not_trade_path=Path(os.getenv("DO_NOT_TRADE_PATH", str(ROOT / "config" / "do_not_trade.yaml"))),
        universe_path=Path(os.getenv("SWING_UNIVERSE_PATH", str(ROOT / "config" / "universe" / "us_liquid_2026-08-11.csv"))),
        normal_risk_pct=_float("NORMAL_RISK_PCT", 0.0075),
        absolute_max_risk_pct=_float("ABSOLUTE_MAX_RISK_PCT", 0.01),
        reduced_risk_pct=_float("REDUCED_RISK_PCT", 0.003),
        reduce_risk_drawdown_pct=_float("REDUCE_RISK_DRAWDOWN_PCT", 0.05),
        halt_drawdown_pct=_float("HALT_DRAWDOWN_PCT", 0.08),
        max_combined_open_risk_pct=_float("MAX_COMBINED_OPEN_RISK_PCT", 0.02),
        max_sector_open_risk_pct=_float("MAX_SECTOR_OPEN_RISK_PCT", 0.015),
        max_cluster_open_risk_pct=_float("MAX_CLUSTER_OPEN_RISK_PCT", 0.01),
        max_position_exposure_pct=_float("MAX_POSITION_EXPOSURE_PCT", 0.35),
        max_open_positions=_int("MAX_OPEN_POSITIONS", 3),
        max_new_positions_day=_int("MAX_NEW_POSITIONS_DAY", 1),
        max_new_positions_week=_int("MAX_NEW_POSITIONS_WEEK", 3),
        consecutive_loss_halt=_int("CONSECUTIVE_LOSS_HALT", 3),
        weekly_drawdown_halt_pct=_float("WEEKLY_DRAWDOWN_HALT_PCT", 0.04),
        minimum_stop_atr=_float("MINIMUM_STOP_ATR", 0.75),
        maximum_stop_atr=_float("MAXIMUM_STOP_ATR", 3.0),
        maximum_adv_fraction=_float("MAXIMUM_ADV_FRACTION", 0.001),
        broker_min_quantity=_int("BROKER_MIN_QUANTITY", 1),
        minimum_rr=_float("MINIMUM_RR", 2.0),
        buy_score_threshold=_float("BUY_SCORE_THRESHOLD", 80.0),
        preferred_score_threshold=_float("PREFERRED_SCORE_THRESHOLD", 85.0),
        a_plus_score_threshold=_float("A_PLUS_SCORE_THRESHOLD", 90.0),
        minimum_price=_float("MINIMUM_PRICE", 5.0),
        minimum_average_volume=_int("MINIMUM_AVERAGE_VOLUME", 1_000_000),
        minimum_average_dollar_volume=_float("MINIMUM_AVERAGE_DOLLAR_VOLUME", 20_000_000),
        minimum_history_sessions=_int("MINIMUM_HISTORY_SESSIONS", 160),
        maximum_spread_pct=_float("MAXIMUM_SPREAD_PCT", 0.005),
        borderline_score_threshold=_float("BORDERLINE_SCORE_THRESHOLD", 70.0),
        neutral_score_threshold_addition=_float("NEUTRAL_SCORE_THRESHOLD_ADDITION", 5.0),
        choppy_score_threshold_addition=_float("CHOPPY_SCORE_THRESHOLD_ADDITION", 10.0),
        hostile_score_threshold_addition=_float("HOSTILE_SCORE_THRESHOLD_ADDITION", 20.0),
        neutral_risk_multiplier=_float("NEUTRAL_RISK_MULTIPLIER", 0.75),
        choppy_risk_multiplier=_float("CHOPPY_RISK_MULTIPLIER", 0.50),
        hostile_risk_multiplier=_float("HOSTILE_RISK_MULTIPLIER", 0.25),
        earnings_exclusion_trading_days=_int("EARNINGS_EXCLUSION_TRADING_DAYS", 5),
        quote_freshness_seconds=_int("QUOTE_FRESHNESS_SECONDS", 300),
        broker_asset_freshness_seconds=_int("BROKER_ASSET_FRESHNESS_SECONDS", 60),
        market_clock_freshness_seconds=_int("MARKET_CLOCK_FRESHNESS_SECONDS", 60),
        corporate_event_freshness_seconds=_int("CORPORATE_EVENT_FRESHNESS_SECONDS", 86_400),
        earnings_freshness_seconds=_int("EARNINGS_FRESHNESS_SECONDS", 86_400),
        maximum_entry_slippage_r=_float("MAXIMUM_ENTRY_SLIPPAGE_R", 0.25),
        expected_hold_days=_int("EXPECTED_HOLD_DAYS", 4),
        maximum_hold_days=_int("MAXIMUM_HOLD_DAYS", 10),
        time_stop_minimum_progress_r=_float("TIME_STOP_MINIMUM_PROGRESS_R", 0.50),
        time_stop_mode=os.getenv("TIME_STOP_MODE", "REVIEW").upper(),
        stop_management_policy=os.getenv("STOP_MANAGEMENT_POLICY", "STRUCTURE_THEN_TRAIL").upper(),
        profitable_trigger_r=_float("PROFITABLE_TRIGGER_R", 1.0),
        trailing_activation_r=_float("TRAILING_ACTIVATION_R", 2.0),
        trailing_distance_r=_float("TRAILING_DISTANCE_R", 0.75),
        move_to_breakeven_at_r=(_float("MOVE_TO_BREAKEVEN_AT_R", 0.0) if os.getenv("MOVE_TO_BREAKEVEN_AT_R") not in (None, "") else None),
        trailing_eligible_setups=tuple(value.strip().upper() for value in os.getenv("TRAILING_ELIGIBLE_SETUPS", "TREND_PULLBACK,RELATIVE_STRENGTH_CONTINUATION").split(",") if value.strip()),
        cooldown_trading_days=_int("COOLDOWN_TRADING_DAYS", 10),
        minimum_statistical_sample=_int("MINIMUM_STATISTICAL_SAMPLE", 30),
        llm_material_score_change=_float("LLM_MATERIAL_SCORE_CHANGE", 5.0),
        maximum_scanner_candidates=_int("MAXIMUM_SCANNER_CANDIDATES", 20),
        maximum_pm_candidates=_int("MAXIMUM_PM_CANDIDATES", 5),
        schedule=ScheduleSettings(
            timezone_name=os.getenv("SCHEDULE_TIMEZONE", "America/New_York"),
            initial_scan_et=os.getenv("SCHEDULE_INITIAL_SCAN_ET", "09:35"),
            decision_cycle_et=os.getenv("SCHEDULE_DECISION_CYCLE_ET", "10:15"),
            midday_refresh_et=os.getenv("SCHEDULE_MIDDAY_REFRESH_ET", "12:30"),
            afternoon_refresh_et=os.getenv("SCHEDULE_AFTERNOON_REFRESH_ET", "14:30"),
            position_health_et=os.getenv("SCHEDULE_POSITION_HEALTH_ET", "15:45"),
            daily_report_et=os.getenv("SCHEDULE_DAILY_REPORT_ET", "16:15"),
            weekly_lessons_et=os.getenv("SCHEDULE_WEEKLY_LESSONS_ET", "Sunday 18:00"),
        ),
        strategy=StrategySettings(
            short_ema_period=_int("STRATEGY_SHORT_EMA_PERIOD", 20),
            atr_period=_int("STRATEGY_ATR_PERIOD", 14),
            medium_ma_period=_int("STRATEGY_MEDIUM_MA_PERIOD", 50),
            long_ma_period=_int("STRATEGY_LONG_MA_PERIOD", 150),
            trend_slope_lookback=_int("STRATEGY_TREND_SLOPE_LOOKBACK", 10),
            minimum_trend_slope=_float("STRATEGY_MINIMUM_TREND_SLOPE", 0.0),
            relative_strength_lookback=_int("STRATEGY_RS_LOOKBACK", 63),
            minimum_spy_relative_strength=_float("STRATEGY_MINIMUM_SPY_RS", 0.0),
            strong_spy_relative_strength=_float("STRATEGY_STRONG_SPY_RS", 0.03),
            minimum_sector_relative_strength=_float("STRATEGY_MINIMUM_SECTOR_RS", 0.0),
            pullback_max_atr_distance=_float("STRATEGY_PULLBACK_MAX_ATR_DISTANCE", 1.25),
            structural_break_atr=_float("STRATEGY_STRUCTURAL_BREAK_ATR", 0.5),
            minimum_atr_pct=_float("STRATEGY_MINIMUM_ATR_PCT", 0.005),
            maximum_atr_pct=_float("STRATEGY_MAXIMUM_ATR_PCT", 0.08),
            consolidation_sessions=_int("STRATEGY_CONSOLIDATION_SESSIONS", 20),
            consolidation_max_range_pct=_float("STRATEGY_CONSOLIDATION_MAX_RANGE_PCT", 0.12),
            breakout_buffer_pct=_float("STRATEGY_BREAKOUT_BUFFER_PCT", 0.003),
            breakout_volume_ratio=_float("STRATEGY_BREAKOUT_VOLUME_RATIO", 1.20),
            retest_hold_tolerance_pct=_float("STRATEGY_RETEST_HOLD_TOLERANCE_PCT", 0.01),
            retest_collapse_tolerance_pct=_float("STRATEGY_RETEST_COLLAPSE_TOLERANCE_PCT", 0.03),
            maximum_breakout_extension_atr=_float("STRATEGY_MAXIMUM_BREAKOUT_EXTENSION_ATR", 1.25),
            rs_consolidation_sessions=_int("STRATEGY_RS_CONSOLIDATION_SESSIONS", 10),
            rs_consolidation_max_atr=_float("STRATEGY_RS_CONSOLIDATION_MAX_ATR", 4.0),
            contraction_volume_ratio=_float("STRATEGY_CONTRACTION_VOLUME_RATIO", 0.90),
            confirmation_volume_ratio=_float("STRATEGY_CONFIRMATION_VOLUME_RATIO", 1.10),
            trend_target_atr_extension=_float("STRATEGY_TREND_TARGET_ATR_EXTENSION", 3.0),
            measured_move_multiple=_float("STRATEGY_MEASURED_MOVE_MULTIPLE", 2.5),
            maximum_bar_age_calendar_days=_int("STRATEGY_MAXIMUM_BAR_AGE_DAYS", 7),
        ),
        score_weights=EntryScoreWeights(
            setup_quality=_float("SCORE_WEIGHT_SETUP_QUALITY", 30.0),
            relative_strength=_float("SCORE_WEIGHT_RELATIVE_STRENGTH", 20.0),
            trend_quality=_float("SCORE_WEIGHT_TREND_QUALITY", 15.0),
            volume_confirmation=_float("SCORE_WEIGHT_VOLUME_CONFIRMATION", 10.0),
            market_regime=_float("SCORE_WEIGHT_MARKET_REGIME", 10.0),
            sector_strength=_float("SCORE_WEIGHT_SECTOR_STRENGTH", 5.0),
            risk_reward=_float("SCORE_WEIGHT_RISK_REWARD", 10.0),
        ),
    )


def load_symbol_blacklist(path: Path) -> set[str]:
    """Read the deliberately simple YAML list without adding a YAML dependency."""
    if not path.is_file():
        raise RuntimeError(f"Required blacklist is not loadable: {path}")
    symbols: set[str] = set()
    active_key = ""
    try:
        rows = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise RuntimeError(f"Required blacklist is not loadable: {path}") from exc
    saw_symbols_key = False
    for raw in rows:
        line = raw.split("#", 1)[0].rstrip()
        if not line:
            continue
        if not line.startswith((" ", "-")) and ":" in line:
            key, inline_value = (part.strip() for part in line.split(":", 1))
            if key in {"symbols", "substantially_identical"} and inline_value:
                if inline_value != "[]":
                    raise RuntimeError(f"Required blacklist has unsupported inline syntax: {path}")
                active_key = key
                saw_symbols_key = saw_symbols_key or key == "symbols"
                continue
        if not line.startswith((" ", "-")) and line.endswith(":"):
            active_key = line[:-1].strip()
            saw_symbols_key = saw_symbols_key or active_key == "symbols"
            continue
        if active_key in {"symbols", "substantially_identical"} and line.lstrip().startswith("-"):
            symbol = line.lstrip()[1:].strip().strip("'\"").upper()
            if symbol:
                symbols.add(symbol)
    if not saw_symbols_key:
        raise RuntimeError(f"Required blacklist is malformed (missing symbols key): {path}")
    return symbols
