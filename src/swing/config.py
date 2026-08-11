"""Typed configuration with fail-closed defaults for the swing system."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _float(name: str, default: float) -> float:
    value = os.getenv(name)
    return float(value) if value not in (None, "") else default


def _int(name: str, default: int) -> int:
    value = os.getenv(name)
    return int(value) if value not in (None, "") else default


@dataclass(frozen=True)
class ModelSettings:
    scanner_model: str | None = field(default_factory=lambda: os.getenv("SCANNER_MODEL") or None)
    analyst_model: str | None = field(default_factory=lambda: os.getenv("ANALYST_MODEL") or None)
    portfolio_manager_model: str | None = field(default_factory=lambda: os.getenv("PORTFOLIO_MANAGER_MODEL") or None)
    postmortem_model: str | None = field(default_factory=lambda: os.getenv("POSTMORTEM_MODEL") or None)
    research_model: str | None = field(default_factory=lambda: os.getenv("RESEARCH_MODEL") or None)
    fallback_model: str | None = field(default_factory=lambda: os.getenv("MODEL_FALLBACK") or None)


@dataclass(frozen=True)
class SwingSettings:
    trading_style: str = "swing"
    execution_mode: str = "paper"
    trading_enabled: bool = False
    live_acknowledgement: str = ""
    strategy_version: str = "SWING_V1.0"
    database_path: Path = ROOT / "data" / "adaptive_swing.db"
    do_not_trade_path: Path = ROOT / "config" / "do_not_trade.yaml"

    normal_risk_pct: float = 0.005
    a_plus_risk_pct: float = 0.0075
    absolute_max_risk_pct: float = 0.01
    reduced_risk_pct: float = 0.003
    reduce_risk_drawdown_pct: float = 0.05
    halt_drawdown_pct: float = 0.08
    max_combined_open_risk_pct: float = 0.02
    max_position_exposure_pct: float = 0.35
    max_open_positions: int = 3
    max_new_positions_day: int = 1
    max_new_positions_week: int = 3
    consecutive_loss_halt: int = 3

    minimum_rr: float = 2.0
    exceptional_minimum_rr: float = 1.5
    buy_score_threshold: float = 80.0
    preferred_score_threshold: float = 85.0
    a_plus_score_threshold: float = 90.0
    minimum_price: float = 5.0
    minimum_average_volume: int = 1_000_000
    maximum_spread_pct: float = 0.005
    earnings_exclusion_trading_days: int = 5
    quote_freshness_seconds: int = 300
    cooldown_trading_days: int = 10
    minimum_statistical_sample: int = 30
    maximum_scanner_candidates: int = 20
    maximum_pm_candidates: int = 5
    models: ModelSettings = field(default_factory=ModelSettings)

    def __post_init__(self) -> None:
        if self.trading_style != "swing":
            raise ValueError("Only TRADING_STYLE=swing is supported")
        if self.execution_mode not in {"backtest", "paper", "live"}:
            raise ValueError("EXECUTION_MODE must be backtest, paper, or live")
        if not (0 < self.normal_risk_pct <= 0.005):
            raise ValueError("NORMAL_RISK_PCT may not exceed the immutable 0.50% ceiling")
        if not (self.normal_risk_pct <= self.a_plus_risk_pct <= 0.0075):
            raise ValueError("A_PLUS_RISK_PCT may not exceed the immutable 0.75% ceiling")
        if not (self.a_plus_risk_pct <= self.absolute_max_risk_pct <= 0.01):
            raise ValueError("ABSOLUTE_MAX_RISK_PCT may not exceed 1.00%")
        if not (0.0025 <= self.reduced_risk_pct <= 0.0035):
            raise ValueError("REDUCED_RISK_PCT must remain between 0.25% and 0.35%")
        if self.reduce_risk_drawdown_pct > 0.05 or self.halt_drawdown_pct > 0.08:
            raise ValueError("Drawdown controls may be stricter, but not weaker than 5%/8%")
        if not (0 < self.reduce_risk_drawdown_pct < self.halt_drawdown_pct):
            raise ValueError("Drawdown thresholds must be positive and ordered")
        if not (0 < self.max_combined_open_risk_pct <= 0.02):
            raise ValueError("MAX_COMBINED_OPEN_RISK_PCT may not exceed 2.00%")
        if not (0 < self.max_position_exposure_pct <= 0.35):
            raise ValueError("MAX_POSITION_EXPOSURE_PCT may not exceed 35%")
        if not (1 <= self.max_open_positions <= 3):
            raise ValueError("MAX_OPEN_POSITIONS must be between one and three")
        if not (1 <= self.max_new_positions_day <= 1) or not (1 <= self.max_new_positions_week <= 3):
            raise ValueError("Entry limits may not exceed one per day and three per week")
        if not (1 <= self.consecutive_loss_halt <= 3):
            raise ValueError("CONSECUTIVE_LOSS_HALT may not exceed three losses")
        if self.buy_score_threshold < 80 or not (
            self.buy_score_threshold <= self.preferred_score_threshold <= self.a_plus_score_threshold <= 100
        ):
            raise ValueError("Score thresholds must be ordered and BUY_SCORE_THRESHOLD cannot be below 80")
        if self.minimum_rr < 2.0 or self.exceptional_minimum_rr < 1.5:
            raise ValueError("Reward/risk minimums may be stricter, but not weaker than 2.0/1.5")
        if self.exceptional_minimum_rr > self.minimum_rr:
            raise ValueError("Exceptional reward/risk cannot exceed the normal minimum")
        if self.minimum_price < 5 or self.minimum_average_volume < 1_000_000:
            raise ValueError("Liquidity filters may be stricter, but not weaker than the production floors")
        if not (0 < self.maximum_spread_pct <= 0.005):
            raise ValueError("MAXIMUM_SPREAD_PCT must be positive and cannot exceed 0.50%")
        if not (1 <= self.quote_freshness_seconds <= 300):
            raise ValueError("QUOTE_FRESHNESS_SECONDS must be between 1 and 300")
        if self.earnings_exclusion_trading_days < 5 or self.cooldown_trading_days < 10:
            raise ValueError("Event and post-loss exclusions may not be weakened")
        if self.minimum_statistical_sample < 30:
            raise ValueError("MINIMUM_STATISTICAL_SAMPLE may not be below 30")
        if not (1 <= self.maximum_scanner_candidates <= 20) or not (1 <= self.maximum_pm_candidates <= 5):
            raise ValueError("Candidate limits must be positive and may not exceed 20/5")
        if self.execution_mode == "live" and self.trading_enabled and self.live_acknowledgement != "I_ACKNOWLEDGE_LIVE_RISK":
            raise ValueError("Live execution requires LIVE_TRADING_ACK=I_ACKNOWLEDGE_LIVE_RISK")


def load_settings() -> SwingSettings:
    """Load settings from the environment; all unsafe switches default off."""
    return SwingSettings(
        trading_style=os.getenv("TRADING_STYLE", "swing").lower(),
        execution_mode=os.getenv("EXECUTION_MODE", "paper").lower(),
        trading_enabled=_bool("TRADING_ENABLED", False),
        live_acknowledgement=os.getenv("LIVE_TRADING_ACK", ""),
        strategy_version=os.getenv("STRATEGY_VERSION", "SWING_V1.0"),
        database_path=Path(os.getenv("SWING_DATABASE_PATH", str(ROOT / "data" / "adaptive_swing.db"))),
        do_not_trade_path=Path(os.getenv("DO_NOT_TRADE_PATH", str(ROOT / "config" / "do_not_trade.yaml"))),
        normal_risk_pct=_float("NORMAL_RISK_PCT", 0.005),
        a_plus_risk_pct=_float("A_PLUS_RISK_PCT", 0.0075),
        absolute_max_risk_pct=_float("ABSOLUTE_MAX_RISK_PCT", 0.01),
        reduced_risk_pct=_float("REDUCED_RISK_PCT", 0.003),
        reduce_risk_drawdown_pct=_float("REDUCE_RISK_DRAWDOWN_PCT", 0.05),
        halt_drawdown_pct=_float("HALT_DRAWDOWN_PCT", 0.08),
        max_combined_open_risk_pct=_float("MAX_COMBINED_OPEN_RISK_PCT", 0.02),
        max_position_exposure_pct=_float("MAX_POSITION_EXPOSURE_PCT", 0.35),
        minimum_rr=_float("MINIMUM_RR", 2.0),
        exceptional_minimum_rr=_float("EXCEPTIONAL_MINIMUM_RR", 1.5),
        buy_score_threshold=_float("BUY_SCORE_THRESHOLD", 80.0),
        preferred_score_threshold=_float("PREFERRED_SCORE_THRESHOLD", 85.0),
        a_plus_score_threshold=_float("A_PLUS_SCORE_THRESHOLD", 90.0),
        minimum_price=_float("MINIMUM_PRICE", 5.0),
        minimum_average_volume=_int("MINIMUM_AVERAGE_VOLUME", 1_000_000),
        maximum_spread_pct=_float("MAXIMUM_SPREAD_PCT", 0.005),
        earnings_exclusion_trading_days=_int("EARNINGS_EXCLUSION_TRADING_DAYS", 5),
        quote_freshness_seconds=_int("QUOTE_FRESHNESS_SECONDS", 300),
        cooldown_trading_days=_int("COOLDOWN_TRADING_DAYS", 10),
        minimum_statistical_sample=_int("MINIMUM_STATISTICAL_SAMPLE", 30),
        maximum_scanner_candidates=_int("MAXIMUM_SCANNER_CANDIDATES", 20),
        maximum_pm_candidates=_int("MAXIMUM_PM_CANDIDATES", 5),
    )


def load_symbol_blacklist(path: Path) -> set[str]:
    """Read the deliberately simple YAML list without adding a YAML dependency."""
    if not path.exists():
        return set()
    symbols: set[str] = set()
    active_key = ""
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].rstrip()
        if not line:
            continue
        if not line.startswith((" ", "-")) and line.endswith(":"):
            active_key = line[:-1].strip()
            continue
        if active_key in {"symbols", "substantially_identical"} and line.lstrip().startswith("-"):
            symbol = line.lstrip()[1:].strip().strip("'\"").upper()
            if symbol:
                symbols.add(symbol)
    return symbols
