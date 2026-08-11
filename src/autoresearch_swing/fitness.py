"""Balanced fitness that penalizes fragility, complexity, and tiny samples."""

from __future__ import annotations

from pydantic import BaseModel, Field


class FitnessMetrics(BaseModel):
    expectancy_r: float
    profit_factor: float = Field(ge=0)
    sharpe: float
    sortino: float
    max_drawdown: float = Field(ge=0)
    total_return: float
    trade_count: int = Field(ge=0)
    turnover: float = Field(ge=0)
    regime_stability: float = Field(ge=0, le=1)
    performance_concentration: float = Field(ge=0, le=1)
    strategy_complexity: int = Field(ge=0)


def balanced_fitness(metrics: FitnessMetrics) -> tuple[float, dict[str, float]]:
    """Return an auditable score; no single return statistic can dominate."""
    components = {
        "expectancy": 30.0 * max(-1.0, min(2.0, metrics.expectancy_r)) / 2.0,
        "profit_factor": 15.0 * max(0.0, min(2.0, metrics.profit_factor - 1.0)) / 2.0,
        "sharpe": 10.0 * max(-1.0, min(2.0, metrics.sharpe)) / 2.0,
        "sortino": 10.0 * max(-1.0, min(3.0, metrics.sortino)) / 3.0,
        "drawdown": 15.0 * max(0.0, 1 - metrics.max_drawdown / 0.08),
        "return": 5.0 * max(-1.0, min(1.0, metrics.total_return / 0.20)),
        "stability": 15.0 * metrics.regime_stability,
    }
    penalties = {
        "low_sample": 30.0 * max(0.0, (30 - metrics.trade_count) / 30),
        "extreme_turnover": max(0.0, metrics.turnover - 4.0) * 2.0,
        "complexity": max(0, metrics.strategy_complexity - 3) * 2.5,
        "concentration": max(0.0, metrics.performance_concentration - 0.50) * 30.0,
        "drawdown_breach": 40.0 if metrics.max_drawdown >= 0.08 else 0.0,
    }
    score = sum(components.values()) - sum(penalties.values())
    detail = {**components, **{f"penalty_{key}": -value for key, value in penalties.items()}}
    return score, detail
