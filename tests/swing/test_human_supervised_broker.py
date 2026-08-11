from __future__ import annotations

from datetime import datetime, timezone
from typing import cast

import pytest

from src.swing.brokers.base import BrokerUnavailable
from src.swing.brokers.human_supervised import HumanSuppliedBrokerProvider
from src.swing.config import SwingSettings
from src.swing.execution import SwingExecutionService
from src.swing.models import OrderIntent


def _snapshot(**overrides):
    now = datetime.now(timezone.utc).isoformat()
    base = {
        "account": {
            "account_id": "robinhood-agentic-1",
            "equity": 2000.0,
            "cash": 2000.0,
            "buying_power": 2000.0,
            "is_paper": False,
            "is_margin_enabled": False,
            "dedicated_agentic_account": True,
            "retrieved_at": now,
        },
        "positions": [],
        "open_orders": [],
        "asset": {"symbol": "XYZ", "asset_class": "us_equity", "tradable": True, "status": "active"},
        "quote": {"symbol": "XYZ", "last": 100.0, "bid": 99.95, "ask": 100.05, "retrieved_at": now, "market_timestamp": now},
        "trade_history": [],
    }
    base.update(overrides)
    return base


def test_rejects_non_dedicated_account():
    snapshot = _snapshot()
    snapshot["account"]["dedicated_agentic_account"] = False
    with pytest.raises(ValueError):
        HumanSuppliedBrokerProvider(snapshot)


def test_rejects_missing_dedicated_flag():
    snapshot = _snapshot()
    del snapshot["account"]["dedicated_agentic_account"]
    with pytest.raises(ValueError):
        HumanSuppliedBrokerProvider(snapshot)


def test_rejects_paper_flag():
    snapshot = _snapshot()
    snapshot["account"]["is_paper"] = True
    with pytest.raises(ValueError):
        HumanSuppliedBrokerProvider(snapshot)


def test_state_changing_methods_always_raise():
    broker = HumanSuppliedBrokerProvider(_snapshot())
    with pytest.raises(BrokerUnavailable):
        broker.place_order(cast(OrderIntent, None))
    with pytest.raises(BrokerUnavailable):
        broker.cancel_order("x")
    with pytest.raises(BrokerUnavailable):
        broker.replace_stop("x", 1.0)
    with pytest.raises(BrokerUnavailable):
        broker.close_position("XYZ")
    with pytest.raises(BrokerUnavailable):
        broker.get_order_status("x")


def test_reads_reflect_snapshot():
    broker = HumanSuppliedBrokerProvider(_snapshot())
    account = broker.get_account()
    assert account.equity == 2000.0
    assert account.is_paper is False
    assert account.dedicated_agentic_account is True
    quote = broker.get_quote("XYZ")
    assert quote.last == 100.0
    asset = broker.get_asset("XYZ")
    assert asset.tradable is True
    with pytest.raises(BrokerUnavailable):
        broker.get_quote("OTHER")
    with pytest.raises(BrokerUnavailable):
        broker.get_asset("OTHER")


def test_live_ticket_flow_uses_unchanged_risk_engine(database, settings, candidate, proposal):
    live_settings = SwingSettings(
        database_path=settings.database_path,
        do_not_trade_path=settings.do_not_trade_path,
        execution_mode="live",
        trading_enabled=True,
        live_acknowledgement="I_ACKNOWLEDGE_LIVE_RISK",
    )
    broker = HumanSuppliedBrokerProvider(_snapshot())
    result = SwingExecutionService(live_settings, database, broker).submit(proposal, candidate, dry_run=True)
    assert result.status == "DRY_RUN_APPROVED"
    assert result.risk.approved is True
    assert result.risk.quantity == 3  # floor(2000 * 0.0075 / (100 - 96))


def test_live_ticket_never_calls_place_order_even_if_dry_run_is_disabled(database, settings, candidate, proposal):
    """Defense in depth: even a caller mistake (dry_run=False) must fail closed, not place a real order."""
    live_settings = SwingSettings(
        database_path=settings.database_path,
        do_not_trade_path=settings.do_not_trade_path,
        execution_mode="live",
        trading_enabled=True,
        live_acknowledgement="I_ACKNOWLEDGE_LIVE_RISK",
    )
    broker = HumanSuppliedBrokerProvider(_snapshot())
    result = SwingExecutionService(live_settings, database, broker).submit(proposal, candidate, dry_run=False)
    assert result.status == "UNKNOWN_REQUIRES_RECONCILIATION"
    assert database.get_state("reconciliation_halt") is True
