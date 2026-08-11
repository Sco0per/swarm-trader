"""Expectancy-first, R-denominated analytics with hard sample-size guards."""

from __future__ import annotations

import math
from collections import defaultdict
from statistics import mean, median, pstdev
from typing import Any, Callable

import numpy as np

from .config import SwingSettings
from .database import SwingDatabase

INSUFFICIENT_SAMPLE = "INSUFFICIENT_SAMPLE"


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
        overall["performance_vs_spy"] = self._performance_vs_spy(overall)

        fields: dict[str, Callable[[dict], str]] = {
            "setup": lambda trade: str(trade.get("setup_type") or "UNKNOWN"),
            "market_regime": lambda trade: str(trade.get("market_regime") or "UNKNOWN"),
            "sector": lambda trade: str(trade.get("sector") or "UNKNOWN"),
            "entry_score_bucket": lambda trade: self._bucket(float(trade.get("candidate_score") or 0), [70, 80, 85, 90]),
            "holding_period": lambda trade: self._bucket(float(trade.get("holding_period_days") or 0), [3, 6, 11]),
            "volatility_bucket": self._volatility_bucket,
        }
        breakdowns = {name: self._group(trades, key) for name, key in fields.items()}
        return {
            "minimum_sample_size": self.settings.minimum_statistical_sample,
            "overall": overall,
            "breakdowns": breakdowns,
            "setup_expectancy": self._setup_cards(breakdowns["setup"]),
            "funnel": self._funnel_metrics(),
        }

    def _insufficient(self, sample_size: int) -> dict[str, Any]:
        return {
            "status": INSUFFICIENT_SAMPLE,
            "sample_size": sample_size,
            "minimum_required": self.settings.minimum_statistical_sample,
        }

    def _guard(self, value: Any, sample_size: int) -> Any:
        return value if sample_size >= self.settings.minimum_statistical_sample else self._insufficient(sample_size)

    def _trade_metrics(self, trades: list[dict]) -> dict[str, Any]:
        usable = [trade for trade in trades if trade.get("realized_r") is not None]
        results = [float(trade["realized_r"]) for trade in usable]
        wins = [value for value in results if value > 0]
        losses = [value for value in results if value < 0]
        n = len(results)
        win_rate = len(wins) / n if n else 0.0
        avg_win = mean(wins) if wins else 0.0
        avg_loss = abs(mean(losses)) if losses else 0.0
        expectancy = win_rate * avg_win - (1 - win_rate) * avg_loss if n else 0.0
        profit_factor = _safe_ratio(sum(wins), abs(sum(losses)))
        mae_values = [float(trade["mae_r"]) for trade in usable if trade.get("mae_r") is not None]
        mfe_values = [float(trade["mfe_r"]) for trade in usable if trade.get("mfe_r") is not None]
        hold_values = [float(trade["holding_period_days"]) for trade in usable if trade.get("holding_period_days") is not None]
        winner_mae = [float(trade["mae_r"]) for trade in usable if float(trade["realized_r"]) > 0 and trade.get("mae_r") is not None]
        loser_mfe = [float(trade["mfe_r"]) for trade in usable if float(trade["realized_r"]) < 0 and trade.get("mfe_r") is not None]
        cumulative = np.cumsum(np.array(results, dtype=float)) if results else np.array([], dtype=float)
        drawdown_r = None
        if len(cumulative):
            curve = np.concatenate(([0.0], cumulative))
            drawdown_r = float(np.min(curve - np.maximum.accumulate(curve)))
        standard_deviation = pstdev(results) if len(results) > 1 else 0.0
        downside = [value for value in results if value < 0]
        downside_deviation = pstdev(downside) if len(downside) > 1 else 0.0
        raw = {
            "win_rate": win_rate,
            "average_winner_r": avg_win,
            "average_loser_r": avg_loss,
            "expectancy_r": expectancy,
            "median_r": median(results) if results else 0.0,
            "profit_factor": profit_factor,
            "max_drawdown_r": drawdown_r,
            "average_mae_r": mean(mae_values) if mae_values else None,
            "average_mfe_r": mean(mfe_values) if mfe_values else None,
            "average_hold_duration_days": mean(hold_values) if hold_values else None,
            "consecutive_losses": self._maximum_consecutive_losses(results),
            "trade_sharpe_r": mean(results) / standard_deviation if standard_deviation else None,
            "trade_sortino_r": mean(results) / downside_deviation if downside_deviation else None,
            "mae_winners_r": mean(winner_mae) if winner_mae else None,
            "mfe_losers_r": mean(loser_mfe) if loser_mfe else None,
            "realized_pnl_dollars": sum(float(trade.get("realized_pnl") or 0) for trade in usable),
        }
        return {
            "trade_count": n,
            "sample_status": "SUFFICIENT" if n >= self.settings.minimum_statistical_sample else INSUFFICIENT_SAMPLE,
            **{name: self._guard(value, n) for name, value in raw.items()},
        }

    def _equity_metrics(self, snapshots: list[dict]) -> dict[str, Any]:
        return_count = max(0, len(snapshots) - 1)
        if return_count == 0:
            marker = self._insufficient(0)
            return {"total_return": marker, "sharpe": marker, "sortino": marker, "max_drawdown": marker}
        values = np.array([float(row["equity"]) for row in snapshots], dtype=float)
        returns = np.diff(values) / values[:-1]
        std = float(np.std(returns, ddof=1)) if len(returns) > 1 else 0.0
        downside = returns[returns < 0]
        downside_std = float(np.std(downside, ddof=1)) if len(downside) > 1 else 0.0
        running_high = np.maximum.accumulate(values)
        raw = {
            "total_return": float(values[-1] / values[0] - 1),
            "sharpe": (float(np.mean(returns)) / std * math.sqrt(252)) if std else None,
            "sortino": (float(np.mean(returns)) / downside_std * math.sqrt(252)) if downside_std else None,
            "max_drawdown": float(np.min(values / running_high - 1)),
        }
        return {name: self._guard(value, return_count) for name, value in raw.items()}

    def _benchmark_metrics(self, snapshots: list[dict]) -> dict[str, dict[str, Any]]:
        grouped: dict[str, list[float]] = defaultdict(list)
        for row in snapshots:
            grouped[str(row["symbol"])].append(float(row["price"]))
        output: dict[str, dict[str, Any]] = {}
        for symbol, prices in grouped.items():
            sample = max(0, len(prices) - 1)
            if sample == 0 or prices[0] <= 0:
                marker = self._insufficient(sample)
                output[symbol] = {"return": marker, "max_drawdown": marker, "observation_count": len(prices)}
                continue
            values = np.array(prices)
            running_high = np.maximum.accumulate(values)
            output[symbol] = {
                "return": self._guard(float(values[-1] / values[0] - 1), sample),
                "max_drawdown": self._guard(float(np.min(values / running_high - 1)), sample),
                "observation_count": len(prices),
            }
        return output

    def _performance_vs_spy(self, overall: dict[str, Any]) -> Any:
        strategy_return = overall.get("total_return")
        spy_return = overall.get("benchmarks", {}).get("SPY", {}).get("return")
        if not isinstance(strategy_return, (int, float)) or not isinstance(spy_return, (int, float)):
            return self._insufficient(overall.get("trade_count", 0))
        return float(strategy_return) - float(spy_return)

    def _group(self, trades: list[dict], key: Callable[[dict], str]) -> dict[str, Any]:
        groups: dict[str, list[dict]] = defaultdict(list)
        for trade in trades:
            groups[key(trade)].append(trade)
        output = {name: self._trade_metrics(rows) for name, rows in sorted(groups.items())}
        for metrics in output.values():
            metrics["performance_vs_spy"] = {"status": "NOT_MEANINGFUL_FOR_TRADE_SEGMENT"}
        return output

    def _setup_cards(self, groups: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
        return {
            setup: {
                "Trades": metrics["trade_count"],
                "Win rate": metrics["win_rate"],
                "Avg winner": metrics["average_winner_r"],
                "Avg loser": metrics["average_loser_r"],
                "Expectancy": metrics["expectancy_r"],
                "Profit factor": metrics["profit_factor"],
                "Average hold": metrics["average_hold_duration_days"],
                "MAE winners": metrics["mae_winners_r"],
                "MFE losers": metrics["mfe_losers_r"],
            }
            for setup, metrics in groups.items()
        }

    def _funnel_metrics(self) -> dict[str, Any]:
        rejections = self.database.rows("SELECT reason_code,COUNT(*) AS count FROM decision_records WHERE subject_type='REJECTION' GROUP BY reason_code ORDER BY reason_code")
        sizing = self.database.sizing_rejection_summary("0001-01-01T00:00:00+00:00")
        return {
            "rejections_by_reason": {row["reason_code"]: int(row["count"]) for row in rejections},
            "valid_candidates_lost_to_whole_share_rounding": sizing["quantity_rounding_to_zero"],
            "valid_candidates_lost_to_insufficient_cash": sizing["insufficient_cash"],
        }

    @staticmethod
    def _maximum_consecutive_losses(results: list[float]) -> int:
        maximum = current = 0
        for result in results:
            current = current + 1 if result < 0 else 0
            maximum = max(maximum, current)
        return maximum

    @staticmethod
    def _bucket(value: float, edges: list[float]) -> str:
        previous = 0.0
        for edge in edges:
            if value < edge:
                return f"{previous:g}-{edge:g}"
            previous = edge
        return f"{edges[-1]:g}+"

    @staticmethod
    def _volatility_bucket(trade: dict) -> str:
        if trade.get("volatility_bucket"):
            return str(trade["volatility_bucket"])
        entry = float(trade.get("entry_price") or 0)
        atr = float(trade.get("atr") or 0)
        if entry <= 0 or atr <= 0:
            return "UNKNOWN"
        ratio = atr / entry
        return "LOW" if ratio < 0.02 else "MEDIUM" if ratio < 0.04 else "HIGH"
