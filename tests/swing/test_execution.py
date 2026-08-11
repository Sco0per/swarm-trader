from __future__ import annotations

from dataclasses import replace

from src.swing.brokers.fake import FakeBrokerProvider
from src.swing.execution import SwingExecutionService


def test_dry_run_never_places_order(settings, database, proposal, candidate, quote):
    broker = FakeBrokerProvider(quotes={"XYZ": quote})
    result = SwingExecutionService(settings, database, broker).submit(proposal, candidate)
    assert result.status == "DRY_RUN_APPROVED"
    assert broker.place_calls == 0


def test_trading_disabled_blocks_submission(settings, database, proposal, candidate, quote):
    broker = FakeBrokerProvider(quotes={"XYZ": quote})
    result = SwingExecutionService(settings, database, broker).submit(proposal, candidate, dry_run=False)
    assert result.status == "REJECTED"
    assert result.risk.rule == "trading_disabled"
    assert broker.place_calls == 0


def test_partial_fill_is_persisted(settings, database, proposal, candidate, quote):
    enabled = replace(settings, trading_enabled=True)
    broker = FakeBrokerProvider(quotes={"XYZ": quote})
    broker.next_status = "partially_filled"
    broker.next_filled_quantity = 1
    broker.next_average_fill_price = 100
    result = SwingExecutionService(enabled, database, broker).submit(proposal, candidate, dry_run=False)
    assert result.status == "PARTIALLY_FILLED"
    assert database.get_trade(result.trade_id)["status"] == "PARTIALLY_FILLED"
    assert database.get_position_lifecycle(result.trade_id)["state"] == "PROTECTED"


def test_broker_timeout_is_not_retried_as_duplicate(settings, database, proposal, candidate, quote):
    enabled = replace(settings, trading_enabled=True)
    broker = FakeBrokerProvider(quotes={"XYZ": quote})
    broker.raise_on_place = TimeoutError("unknown broker outcome")
    service = SwingExecutionService(enabled, database, broker)
    first = service.submit(proposal, candidate, dry_run=False)
    second = service.submit(proposal, candidate, dry_run=False)
    assert first.status == "UNKNOWN_REQUIRES_RECONCILIATION"
    assert second.risk.rule == "pre_submission_reconciliation"
    assert broker.place_calls == 1
