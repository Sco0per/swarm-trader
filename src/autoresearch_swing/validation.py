"""Time-series split helpers that keep future data out of training."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta


@dataclass(frozen=True)
class TimeSplit:
    train_start: date
    train_end: date
    validation_start: date
    validation_end: date
    out_of_sample_start: date
    out_of_sample_end: date

    def __post_init__(self) -> None:
        if not (
            self.train_start <= self.train_end < self.validation_start <= self.validation_end
            < self.out_of_sample_start <= self.out_of_sample_end
        ):
            raise ValueError("Time-series splits must be ordered, non-overlapping, and causal")


def chronological_split(start: date, end: date, train_fraction: float = 0.60, validation_fraction: float = 0.20) -> TimeSplit:
    if not (0 < train_fraction < 1 and 0 < validation_fraction < 1 and train_fraction + validation_fraction < 1):
        raise ValueError("Invalid split fractions")
    days = (end - start).days + 1
    if days < 365:
        raise ValueError("At least one calendar year is required for a credible split")
    train_end = start + timedelta(days=int(days * train_fraction) - 1)
    validation_start = train_end + timedelta(days=1)
    validation_end = validation_start + timedelta(days=int(days * validation_fraction) - 1)
    return TimeSplit(start, train_end, validation_start, validation_end, validation_end + timedelta(days=1), end)


def walk_forward_windows(start: date, end: date, train_days: int = 504, test_days: int = 126) -> list[tuple[date, date, date, date]]:
    if train_days <= 0 or test_days <= 0:
        raise ValueError("Window lengths must be positive")
    windows = []
    train_start = start
    while True:
        train_end = train_start + timedelta(days=train_days - 1)
        test_start = train_end + timedelta(days=1)
        test_end = min(end, test_start + timedelta(days=test_days - 1))
        if test_start > end:
            break
        windows.append((train_start, train_end, test_start, test_end))
        if test_end >= end:
            break
        train_start += timedelta(days=test_days)
    return windows
