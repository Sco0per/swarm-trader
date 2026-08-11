"""Canonical fixed-R calculations for journal and excursion analytics."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Iterable


@dataclass(frozen=True)
class Fill:
    quantity: float
    price: float
    timestamp: datetime | None = None

    def __post_init__(self) -> None:
        if self.quantity <= 0 or self.price <= 0:
            raise ValueError("Fill quantity and price must be positive")


@dataclass(frozen=True)
class RMultipleSnapshot:
    initial_risk_dollars: float
    realized_r: float
    unrealized_r: float
    mfe_r: float
    mae_r: float
    stop_locked_r: float


def weighted_average_price(fills: Iterable[Fill]) -> tuple[float, float]:
    rows = tuple(fills)
    quantity = sum(fill.quantity for fill in rows)
    if quantity <= 0:
        raise ValueError("At least one positive fill is required")
    return sum(fill.quantity * fill.price for fill in rows) / quantity, quantity


def initial_risk_dollars(entry_fills: Iterable[Fill], initial_stop: float) -> float:
    """Risk frozen from actual entry fills to the one original stop."""

    rows = tuple(entry_fills)
    if initial_stop <= 0:
        raise ValueError("Initial stop must be positive")
    risk = sum(fill.quantity * (fill.price - initial_stop) for fill in rows)
    if not rows or risk <= 0:
        raise ValueError("Long entry fills must all produce positive aggregate initial risk")
    return risk


def calculate_r_multiples(
    *,
    entry_fills: Iterable[Fill],
    exit_fills: Iterable[Fill] = (),
    initial_stop: float,
    current_price: float,
    current_stop: float,
    high_during_trade: float,
    low_during_trade: float,
) -> RMultipleSnapshot:
    """Calculate fixed-denominator R for a long position, including partial exits.

    Entry lots are consumed FIFO for realized P&L. Remaining lots are marked at
    ``current_price``. MFE/MAE use the aggregate actual-entry average and daily
    bar extremes supplied by the caller. A stop above cost produces positive
    ``stop_locked_r``; it never changes the original denominator.
    """

    entries = tuple(entry_fills)
    exits = tuple(exit_fills)
    average_entry, entered_quantity = weighted_average_price(entries)
    risk = initial_risk_dollars(entries, initial_stop)
    exited_quantity = sum(fill.quantity for fill in exits)
    if exited_quantity > entered_quantity + 1e-9:
        raise ValueError("Exit quantity cannot exceed entered quantity")
    if min(current_price, current_stop, high_during_trade, low_during_trade) <= 0:
        raise ValueError("Prices and stops must be positive")

    # The system journals an aggregate average entry; partial exits therefore
    # realize against that immutable aggregate cost basis.
    realized_pnl = sum(fill.quantity * (fill.price - average_entry) for fill in exits)
    remaining = entered_quantity - exited_quantity
    unrealized_pnl = remaining * (current_price - average_entry)
    mfe_pnl = entered_quantity * (max(high_during_trade, average_entry) - average_entry)
    mae_loss = entered_quantity * (average_entry - min(low_during_trade, average_entry))
    stop_locked_pnl = remaining * (current_stop - average_entry)
    return RMultipleSnapshot(
        initial_risk_dollars=risk,
        realized_r=realized_pnl / risk,
        unrealized_r=unrealized_pnl / risk,
        mfe_r=mfe_pnl / risk,
        mae_r=mae_loss / risk,
        stop_locked_r=stop_locked_pnl / risk,
    )


def excursion_from_daily_bars(*, entry_price: float, quantity: float, initial_risk_dollars_value: float, bars: Iterable[dict]) -> tuple[float, float]:
    """Return MFE_R and MAE_R from daily high/low fixture or provider bars."""

    rows = tuple(bars)
    if entry_price <= 0 or quantity <= 0 or initial_risk_dollars_value <= 0:
        raise ValueError("Entry, quantity, and initial risk must be positive")
    if not rows:
        return 0.0, 0.0
    high = max(float(row["high"]) for row in rows)
    low = min(float(row["low"]) for row in rows)
    return (
        quantity * max(0.0, high - entry_price) / initial_risk_dollars_value,
        quantity * max(0.0, entry_price - low) / initial_risk_dollars_value,
    )


class TradeMeasurementService:
    """Persist daily-bar unrealized R and cumulative excursion for open trades."""

    def __init__(self, database):
        self.database = database

    def record_daily_bar(
        self,
        trade_id: str,
        *,
        high: float,
        low: float,
        close: float,
        source: str,
        observed_at: datetime | None = None,
    ) -> RMultipleSnapshot:
        trade = self.database.get_trade(trade_id)
        if not trade:
            raise KeyError(trade_id)
        if trade["status"] not in {"SUBMITTED", "OPEN", "PARTIALLY_FILLED"}:
            raise ValueError("Daily excursion tracking only accepts active trades")
        entry = float(trade["entry_price"])
        shares = float(trade["shares"])
        risk = float(trade.get("initial_risk_dollars") or shares * (entry - float(trade["initial_stop"])))
        if shares <= 0 or risk <= 0:
            raise ValueError("Active trade lacks filled shares or fixed initial risk")
        previous_high, previous_low = self.database.trade_price_extremes(trade_id, entry)
        cumulative_high = max(previous_high, high)
        cumulative_low = min(previous_low, low)
        mfe_r = shares * max(0.0, cumulative_high - entry) / risk
        mae_r = shares * max(0.0, entry - cumulative_low) / risk
        unrealized_r = shares * (close - entry) / risk
        current_stop = float(trade["final_stop"])
        stop_locked_r = shares * (current_stop - entry) / risk
        snapshot = RMultipleSnapshot(
            initial_risk_dollars=risk,
            realized_r=float(trade.get("realized_r") or 0.0),
            unrealized_r=unrealized_r,
            mfe_r=mfe_r,
            mae_r=mae_r,
            stop_locked_r=stop_locked_r,
        )
        self.database.record_trade_snapshot(
            trade_id,
            price=close,
            stop=current_stop,
            source=source,
            unrealized_pnl=shares * (close - entry),
            mfe=max(0.0, cumulative_high - entry),
            mae=max(0.0, entry - cumulative_low),
            high=high,
            low=low,
            unrealized_r=unrealized_r,
            mfe_r=mfe_r,
            mae_r=mae_r,
            payload={"bar_granularity": "1d", "observed_at": observed_at.isoformat() if observed_at else None},
        )
        self.database.update_trade(
            trade_id,
            unrealized_r=unrealized_r,
            mfe=max(0.0, cumulative_high - entry),
            mae=max(0.0, entry - cumulative_low),
            mfe_r=mfe_r,
            mae_r=mae_r,
        )
        return snapshot
