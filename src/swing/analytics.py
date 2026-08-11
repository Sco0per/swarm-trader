"""Expectancy-first analytics with sample-size warnings and benchmark comparison."""

from __future__ import annotations

import math
from collections import defaultdict
from statistics import mean, pstdev
from typing import Any, Callable

import numpy as np

from .config import SwingSettings
from .database import SwingDatabase


def _safe_ratio(numerator: float, denominator: float) -> float | None:
    return numerator / denominator if denominator else None


class ExperienceEngine:
    def __init__(self, database: SwingDatabase, settings: SwingSettings):
        self.database = database
        self.settings = settings

    def calculate(self) -> dict[str, Any]:
        trades = self.database.closed_trades()
        equity = self.database.rows("SELECT * FROM equity_snapshots ORDER BY observed_at")
        benchmarks = self.database.rows("SELECT * FROM benchmark_snapshots ORDER BY observed_at")
        overall = self._trade_metrics(trades)
        overall.update(self._equity_metrics(equity))
        overall["benchmarks"] = self._benchmark_metrics(benchmarks)
        if overall.get("total_return") is not None:
            for symbol, metric in overall["benchmarks"].items():
                metric["excess_return"] = overall["total_return"] - metric["return"]
        breakdowns = {}
        fields: dict[str, Callable[[dict], str]] = {
            "setup_type": lambda trade: str(trade.get("setup_type") or "UNKNOWN"),
            "market_regime": lambda trade: str(trade.get("market_regime") or "UNKNOWN"),
            "sector": lambda trade: str(trade.get("sector") or "UNKNOWN"),
            "stock": lambda trade: str(trade.get("ticker") or "UNKNOWN"),
            "score_bucket": lambda trade: f"{int(float(trade.get('candidate_score') or 0) // 5) * 5}-{int(float(trade.get('candidate_score') or 0) // 5) * 5 + 4}",
            "holding_period": lambda trade: self._bucket(float(trade.get("holding_period_days") or 0), [2, 5, 10, 20]),
            "rr_bucket": lambda trade: self._bucket(float(trade.get("planned_rr") or 0), [1.5, 2, 2.5, 3]),
        }
        for name, key in fields.items():
            breakdowns[name] = self._group(trades, key)
        # Schema-stable placeholders surface missing collection instead of fabricating it.
        for unavailable in ("entry_type", "volatility_bucket", "relative_strength_bucket", "day_of_week", "earnings_proximity", "stop_methodology"):
            breakdowns[unavailable] = {"status": "not_collected_or_insufficient"}
        return {"overall": overall, "breakdowns": breakdowns}

    def _trade_metrics(self, trades: list[dict]) -> dict[str, Any]:
        results = [float(trade["realized_r"]) for trade in trades if trade.get("realized_r") is not None]
        wins = [value for value in results if value > 0]
        losses = [value for value in results if value < 0]
        win_rate = len(wins) / len(results) if results else 0.0
        avg_win = mean(wins) if wins else 0.0
        avg_loss_magnitude = abs(mean(losses)) if losses else 0.0
        expectancy = win_rate * avg_win - (1 - win_rate) * avg_loss_magnitude if results else 0.0
        profit_factor = _safe_ratio(sum(wins), abs(sum(losses)))
        return {
            "sample_size": len(results),
            "statistically_meaningful": len(results) >= self.settings.minimum_statistical_sample,
            "sample_warning": None if len(results) >= self.settings.minimum_statistical_sample else "Exploratory only; sample is below configured minimum",
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": win_rate,
            "average_win_r": avg_win,
            "average_loss_r": avg_loss_magnitude,
            "expectancy_r": expectancy,
            "profit_factor": profit_factor,
            "average_holding_period_days": mean([float(t["holding_period_days"]) for t in trades if t.get("holding_period_days") is not None]) if trades else 0.0,
            "realized_pnl": sum(float(t.get("realized_pnl") or 0) for t in trades),
        }

    def _equity_metrics(self, snapshots: list[dict]) -> dict[str, Any]:
        if len(snapshots) < 2:
            return {"total_return": None, "sharpe": None, "sortino": None, "max_drawdown": None}
        values = np.array([float(row["equity"]) for row in snapshots], dtype=float)
        returns = np.diff(values) / values[:-1]
        total_return = values[-1] / values[0] - 1
        std = float(np.std(returns, ddof=1)) if len(returns) > 1 else 0
        downside = returns[returns < 0]
        downside_std = float(np.std(downside, ddof=1)) if len(downside) > 1 else 0
        running_high = np.maximum.accumulate(values)
        max_drawdown = float(np.min(values / running_high - 1))
        return {
            "total_return": total_return,
            "sharpe": (float(np.mean(returns)) / std * math.sqrt(252)) if std else None,
            "sortino": (float(np.mean(returns)) / downside_std * math.sqrt(252)) if downside_std else None,
            "max_drawdown": max_drawdown,
        }

    def _benchmark_metrics(self, snapshots: list[dict]) -> dict[str, dict[str, float]]:
        grouped: dict[str, list[float]] = defaultdict(list)
        for row in snapshots:
            grouped[str(row["symbol"])].append(float(row["price"]))
        output = {}
        for symbol, prices in grouped.items():
            if len(prices) >= 2 and prices[0] > 0:
                values = np.array(prices)
                running_high = np.maximum.accumulate(values)
                output[symbol] = {
                    "return": float(values[-1] / values[0] - 1),
                    "max_drawdown": float(np.min(values / running_high - 1)),
                    "sample_size": len(values),
                }
        return output

    def _group(self, trades: list[dict], key: Callable[[dict], str]) -> dict[str, Any]:
        groups: dict[str, list[dict]] = defaultdict(list)
        for trade in trades:
            groups[key(trade)].append(trade)
        return {name: self._trade_metrics(rows) for name, rows in sorted(groups.items())}

    @staticmethod
    def _bucket(value: float, edges: list[float]) -> str:
        previous = 0.0
        for edge in edges:
            if value < edge:
                return f"{previous:g}-{edge:g}"
            previous = edge
        return f"{edges[-1]:g}+"
