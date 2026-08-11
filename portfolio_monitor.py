#!/usr/bin/env python3
"""Disabled legacy monitor retained only as a fail-closed compatibility shim.

The old implementation inferred percentage stops from mode configuration. Swing
mode supplied zero values, which made flat and profitable positions satisfy the
trigger comparisons. It also submitted uncoordinated market sells. Durable
technical stops and broker reconciliation now live under ``src.swing``.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone


DISABLED_REASON = (
    "Legacy percentage-stop monitor is disabled. Use broker-native bracket legs, "
    "the protected stop service, and `python -m src.swing.cli reconcile`."
)


def check_hard_stop(position: dict, stop_loss_pct: float) -> tuple[bool, str]:
    """Compatibility helper; non-positive thresholds are always disabled."""
    if stop_loss_pct <= 0:
        return False, ""
    avg_entry = float(position.get("avg_entry_price", 0))
    current_price = float(position.get("current_price", 0))
    if avg_entry <= 0 or current_price <= 0:
        return False, ""
    pct_change = (current_price - avg_entry) / avg_entry
    if pct_change <= -stop_loss_pct:
        return True, f"Hard stop triggered: {pct_change:.2%} from entry"
    return False, ""


def check_trailing_stop(
    position: dict, intraday_high: float | None, trailing_stop_pct: float,
) -> tuple[bool, str]:
    """Compatibility helper; non-positive thresholds are always disabled."""
    if trailing_stop_pct <= 0 or intraday_high is None:
        return False, ""
    current_price = float(position.get("current_price", 0))
    if intraday_high <= 0 or current_price <= 0:
        return False, ""
    drop_from_high = (current_price - intraday_high) / intraday_high
    if drop_from_high <= -trailing_stop_pct:
        return True, f"Trailing stop triggered: {drop_from_high:.2%} from intraday high"
    return False, ""


def run_monitor(dry_run: bool = False, mode: str | None = None) -> dict:
    """Return without reading an account or placing an order."""
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "status": "DISABLED_FAIL_CLOSED",
        "trading_mode": mode or "swing",
        "dry_run_requested": dry_run,
        "positions_checked": 0,
        "stops_triggered": 0,
        "actions": [],
        "reason": DISABLED_REASON,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Disabled legacy portfolio monitor")
    parser.add_argument("--dry-run", "--no-sell", action="store_true")
    parser.add_argument("--mode", choices=["swing"], default="swing")
    args = parser.parse_args()
    print(json.dumps(run_monitor(dry_run=args.dry_run, mode=args.mode), indent=2))
