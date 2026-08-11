from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from src.swing.brokers.fake import FakeBrokerProvider
from src.swing.database import SwingDatabase
from src.swing.execution import SwingExecutionService
from src.swing.lifecycle import (
    IllegalLifecycleTransition,
    LEGAL_TRANSITIONS,
    PositionLifecycleService,
    StopManagementPolicy,
    TimeStopInputs,
    TimeStopPolicy,
)
from src.swing.models import (
    BrokerAsset,
    Position,
    PositionLifecycleState,
    SetupType,
    TechnicalThesis,
    TradeThesis,
)
from src.swing.reconciliation import BrokerReconciler
from src.swing.risk import SwingRiskManager


def _thesis(candidate) -> TradeThesis:
    return TradeThesis(
        ticker=candidate.ticker,
        setup=candidate.setup_type,
        entry=100,
        initial_stop=96,
        initial_target=110,
        initial_risk_dollars=12,
        initial_risk_per_share=4,
        initial_rr=2.5,
        entry_score=candidate.score,
        market_regime=candidate.market_regime.value,
        expected_hold_days=4,
        max_hold_days=10,
        technical_thesis=TechnicalThesis(trend="bullish", support=96, trigger=100),
        invalidations=("daily close below structural support", "setup structure failure"),
    )


def _seed_trade(database, settings, candidate, *, state: PositionLifecycleState = PositionLifecycleState.OPEN) -> str:
    database.record_candidate(candidate)
    trade_id = str(uuid4())
    database.add_trade_with_thesis(
        {
            "trade_id": trade_id,
            "decision_id": str(uuid4()),
            "candidate_id": candidate.candidate_id,
            "ticker": candidate.ticker,
            "setup_type": candidate.setup_type.value,
            "status": "OPEN",
            "strategy_version": settings.strategy_version,
            "entry_datetime": datetime.now(timezone.utc).isoformat(),
            "entry_price": 100,
            "initial_stop": 96,
            "final_stop": 96,
            "target": 110,
            "shares": 3,
            "planned_dollar_risk": 12,
            "planned_account_risk_pct": 0.006,
            "planned_rr": 2.5,
            "market_regime": candidate.market_regime.value,
            "sector": candidate.sector,
            "candidate_score": candidate.score,
            "risk_validation_result": "{}",
        },
        _thesis(candidate),
    )
    database.create_position_lifecycle(trade_id, state, current_stop=96, trigger="test_seed")
    return trade_id


def _filled_trade(settings, database, proposal, candidate, quote):
    enabled = replace(settings, trading_enabled=True)
    broker = FakeBrokerProvider(quotes={candidate.ticker: quote})
    broker.next_status = "filled"
    broker.next_filled_quantity = 3
    broker.next_average_fill_price = quote.last
    result = SwingExecutionService(enabled, database, broker).submit(proposal, candidate, dry_run=False)
    assert result.status == "OPEN"
    return enabled, broker, result


def test_filled_bracket_is_immediately_protected_and_thesis_is_immutable(settings, database, proposal, candidate, quote):
    _, _, result = _filled_trade(settings, database, proposal, candidate, quote)
    assert database.get_position_lifecycle(result.trade_id)["state"] == "PROTECTED"
    thesis = database.get_trade_thesis(result.trade_id)
    assert thesis.ticker == "XYZ"
    assert thesis.initial_stop == 96
    with pytest.raises(database.integrity_errors):
        database.overwrite_trade_thesis(result.trade_id, thesis.model_copy(update={"entry_score": 99}))
    with pytest.raises(ValueError, match="Immutable original trade fields"):
        database.update_trade(result.trade_id, initial_stop=95)


def test_every_canonical_lifecycle_transition_is_persisted(settings, database, proposal, candidate, quote):
    _, _, result = _filled_trade(settings, database, proposal, candidate, quote)
    lifecycle = PositionLifecycleService(settings, database)
    lifecycle.transition(result.trade_id, PositionLifecycleState.PROFITABLE, trigger="one_r", reason_code="PROFITABLE_TRIGGER")
    lifecycle.transition(result.trade_id, PositionLifecycleState.TRAILING, trigger="policy", reason_code="TRAILING_TRIGGER")
    lifecycle.transition(result.trade_id, PositionLifecycleState.EXIT_PENDING, trigger="technical_failure", reason_code="EXIT_TECHNICAL_TREND_FAILURE")
    lifecycle.transition(result.trade_id, PositionLifecycleState.CLOSED, trigger="broker_fill", reason_code="CLOSED_BROKER_EXIT")
    assert database.get_position_lifecycle(result.trade_id)["state"] == "CLOSED"
    states = [row["to_state"] for row in database.rows("SELECT to_state FROM lifecycle_events WHERE trade_id=? ORDER BY lifecycle_event_id", (result.trade_id,))]
    assert states == ["OPEN", "PROTECTED", "PROFITABLE", "TRAILING", "EXIT_PENDING", "CLOSED"]


def test_restart_recovers_mid_lifecycle_from_durable_state(settings, database, proposal, candidate, quote):
    _, _, result = _filled_trade(settings, database, proposal, candidate, quote)
    PositionLifecycleService(settings, database).transition(
        result.trade_id,
        PositionLifecycleState.PROFITABLE,
        trigger="one_r",
        reason_code="PROFITABLE_TRIGGER",
    )
    reopened = SwingDatabase(settings.database_path)
    reopened.initialize(settings.strategy_version)
    assert reopened.get_position_lifecycle(result.trade_id)["state"] == "PROFITABLE"
    assert reopened.get_trade_thesis(result.trade_id).ticker == "XYZ"


ILLEGAL_TRANSITIONS = [(old, new) for old in PositionLifecycleState for new in PositionLifecycleState if new != old and new not in LEGAL_TRANSITIONS[old]]


@pytest.mark.parametrize(("old_state", "new_state"), ILLEGAL_TRANSITIONS)
def test_every_illegal_lifecycle_transition_raises(settings, database, candidate, old_state, new_state):
    trade_id = _seed_trade(database, settings, candidate, state=old_state)
    with pytest.raises(IllegalLifecycleTransition):
        PositionLifecycleService(settings, database).transition(trade_id, new_state, trigger="illegal_test", reason_code="ILLEGAL")


def test_named_stop_policy_does_not_assume_breakeven_at_one_r(settings, candidate):
    policy = StopManagementPolicy(settings)
    decision = policy.evaluate(thesis=_thesis(candidate), current_stop=96, current_price=104, high_watermark=104)
    assert decision.state == PositionLifecycleState.PROFITABLE
    assert decision.action == "HOLD"
    assert settings.move_to_breakeven_at_r is None


def test_trailing_policy_activates_only_for_configured_setup_and_progress(settings, candidate):
    policy = StopManagementPolicy(settings)
    decision = policy.evaluate(thesis=_thesis(candidate), current_stop=96, current_price=108, high_watermark=109)
    assert decision.action == "TIGHTEN_STOP"
    assert decision.proposed_stop == pytest.approx(106)
    ineligible = _thesis(candidate).model_copy(update={"setup": SetupType.BREAKOUT_RETEST})
    assert policy.evaluate(thesis=ineligible, current_stop=96, current_price=108, high_watermark=109).action == "HOLD"


def test_time_stop_requires_exact_configured_criteria_and_defaults_to_review(settings):
    policy = TimeStopPolicy(settings)
    developing = TimeStopInputs(days_held=4, progress_r=0.1, expected_hold_days=4, max_hold_days=10, trend_degradation=False)
    assert policy.evaluate(developing).action == "HOLD"
    dead = replace(developing, trend_degradation=True)
    assert policy.evaluate(dead).action == "REVIEW"
    assert policy.evaluate(replace(developing, days_held=10)).action == "REVIEW"
    auto_exit = TimeStopPolicy(replace(settings, time_stop_mode="EXIT"))
    assert auto_exit.evaluate(dead).action == "EXIT"


@pytest.mark.parametrize(
    ("trigger_kwargs", "reason_code"),
    [
        ({"thesis_invalidated": True}, "EXIT_THESIS_INVALIDATED"),
        ({"technical_trend_failure": True}, "EXIT_TECHNICAL_TREND_FAILURE"),
        ({"event_risk_exit": True}, "EXIT_EVENT_RISK"),
        ({"manual_safety_exit": True}, "EXIT_MANUAL_SAFETY"),
    ],
)
def test_each_deterministic_managed_exit_trigger_enters_exit_pending(settings, database, candidate, trigger_kwargs, reason_code):
    trade_id = _seed_trade(database, settings, candidate, state=PositionLifecycleState.PROTECTED)
    decision = PositionLifecycleService(settings, database).evaluate_exit(trade_id, **trigger_kwargs)
    assert decision.action == "EXIT"
    assert decision.reason_code == reason_code
    assert database.get_position_lifecycle(trade_id)["state"] == "EXIT_PENDING"


def test_configured_automatic_time_stop_enters_exit_pending(settings, database, candidate):
    auto_settings = replace(settings, time_stop_mode="EXIT")
    trade_id = _seed_trade(database, auto_settings, candidate, state=PositionLifecycleState.PROTECTED)
    decision = PositionLifecycleService(auto_settings, database).evaluate_exit(
        trade_id,
        time_stop_inputs=TimeStopInputs(days_held=10, progress_r=0.2, expected_hold_days=4, max_hold_days=10, trend_degradation=False),
    )
    assert decision.reason_code == "EXIT_TIME_STOP"
    assert database.get_position_lifecycle(trade_id)["state"] == "EXIT_PENDING"


@pytest.mark.parametrize(
    ("mutation", "expected_rule"),
    [
        (lambda c, q, p, a: (c, q.model_copy(update={"retrieved_at": datetime.now(timezone.utc) - timedelta(seconds=301)}), p, a), "stale_quote"),
        (lambda c, q, p, a: (c, q.model_copy(update={"market_timestamp": datetime.now(timezone.utc) - timedelta(seconds=301)}), p, a), "stale_market_data"),
        (lambda c, q, p, a: (c, q.model_copy(update={"market_timestamp": None}), p, a), "market_data_missing"),
        (
            lambda c, q, p, a: (
                c.model_copy(update={"data": c.data.model_copy(update={"retrieved_at": datetime.now(timezone.utc) - timedelta(days=8), "market_timestamp": datetime.now(timezone.utc) - timedelta(days=8)})}),
                q,
                p,
                a,
            ),
            "stale_candidate",
        ),
        (lambda c, q, p, a: (c, q, p.model_copy(update={"account": p.account.model_copy(update={"retrieved_at": datetime.now(timezone.utc) - timedelta(seconds=301)})}), a), "stale_account"),
        (lambda c, q, p, a: (c.model_copy(update={"completed_daily_bar": False}), q, p, a), "incomplete_daily_bar"),
        (lambda c, q, p, a: (c.model_copy(update={"major_event_retrieved_at": None}), q, p, a), "stale_event_data"),
        (lambda c, q, p, a: (c.model_copy(update={"major_event_retrieved_at": datetime.now(timezone.utc) - timedelta(days=2)}), q, p, a), "stale_event_data"),
        (lambda c, q, p, a: (c.model_copy(update={"earnings_retrieved_at": None}), q, p, a), "stale_earnings_data"),
        (lambda c, q, p, a: (c.model_copy(update={"earnings_retrieved_at": datetime.now(timezone.utc) - timedelta(days=2)}), q, p, a), "stale_earnings_data"),
        (lambda c, q, p, a: (c, q.model_copy(update={"bid": None, "ask": None}), p, a), "spread_unavailable"),
        (lambda c, q, p, a: (c, q, p, a.model_copy(update={"retrieved_at": datetime.now(timezone.utc) - timedelta(seconds=61)})), "stale_asset_state"),
    ],
)
def test_each_safety_critical_freshness_condition_rejects_independently(settings, database, proposal, candidate, quote, portfolio, mutation, expected_rule):
    asset = BrokerAsset(symbol="XYZ", asset_class="us_equity", tradable=True, status="active")
    candidate, quote, portfolio, asset = mutation(candidate, quote, portfolio, asset)
    result = SwingRiskManager(settings, database).validate_entry(proposal, candidate, quote, portfolio, str(uuid4()), asset=asset)
    assert not result.approved
    assert result.rule == expected_rule


def test_market_clock_missing_or_closed_rejects_before_submission(settings, database, proposal, candidate, quote):
    class ClosedBroker(FakeBrokerProvider):
        def review_order(self, intent):
            review = super().review_order(intent)
            review["market_open"] = False
            return review

    enabled = replace(settings, trading_enabled=True)
    broker = ClosedBroker(quotes={"XYZ": quote})
    result = SwingExecutionService(enabled, database, broker).submit(proposal, candidate, dry_run=False)
    assert result.status == "REJECTED"
    assert result.message == "Broker reports that the market is closed"
    assert broker.place_calls == 0


def test_rejected_protective_leg_requests_emergency_exit_and_halts(settings, database, proposal, candidate, quote):
    enabled = replace(settings, trading_enabled=True)
    broker = FakeBrokerProvider(quotes={"XYZ": quote})
    broker.next_status = "partially_filled"
    broker.next_filled_quantity = 1
    broker.reject_stop_leg = True
    result = SwingExecutionService(enabled, database, broker).submit(proposal, candidate, dry_run=False)
    assert result.status == "PROTECTION_FAILURE_HALTED"
    assert database.get_state("reconciliation_halt") is True
    assert database.get_position_lifecycle(result.trade_id)["state"] == "EXIT_PENDING"
    assert broker.positions == []


def test_duplicate_submission_uses_stable_client_order_id_and_never_places_twice(settings, database, proposal, candidate, quote):
    enabled = replace(settings, trading_enabled=True)
    broker = FakeBrokerProvider(quotes={"XYZ": quote})
    service = SwingExecutionService(enabled, database, broker)
    first = service.submit(proposal, candidate, dry_run=False)
    second = service.submit(proposal, candidate, dry_run=False)
    assert first.intent_id == second.intent_id
    assert second.risk.rule == "duplicate_intent"
    assert broker.place_calls == 1


def _root_fill(result, trade, *, quantity=3):
    return {
        "id": trade["broker_order_id"],
        "client_order_id": result.intent_id[:48],
        "symbol": "XYZ",
        "side": "buy",
        "qty": str(quantity),
        "filled_qty": str(quantity),
        "filled_avg_price": "100",
        "status": "filled",
        "submitted_at": trade["entry_datetime"],
    }


def test_replaced_protective_leg_remains_reconcilable(settings, database, proposal, candidate, quote):
    enabled, broker, result = _filled_trade(settings, database, proposal, candidate, quote)
    trade = database.get_trade(result.trade_id)
    broker.history = [_root_fill(result, trade)]
    broker.open_orders = [
        {"id": "old-stop", "parent_order_id": trade["broker_order_id"], "symbol": "XYZ", "side": "sell", "type": "stop", "stop_price": "96", "status": "replaced", "replaced_by": "new-stop"},
        {"id": "new-stop", "parent_order_id": trade["broker_order_id"], "symbol": "XYZ", "side": "sell", "type": "stop", "stop_price": "98", "status": "new", "replaces": "old-stop"},
        {"id": "target", "parent_order_id": trade["broker_order_id"], "symbol": "XYZ", "side": "sell", "type": "limit", "limit_price": "110", "status": "new"},
    ]
    assert BrokerReconciler(enabled, database, broker).reconcile().reconciled


def test_cancelled_protective_leg_halts_and_requests_emergency_close(settings, database, proposal, candidate, quote):
    enabled, broker, result = _filled_trade(settings, database, proposal, candidate, quote)
    trade = database.get_trade(result.trade_id)
    broker.history = [_root_fill(result, trade)]
    broker.open_orders = [
        {"id": "dead-stop", "parent_order_id": trade["broker_order_id"], "symbol": "XYZ", "side": "sell", "type": "stop", "stop_price": "96", "status": "canceled"},
        {"id": "target", "parent_order_id": trade["broker_order_id"], "symbol": "XYZ", "side": "sell", "type": "limit", "limit_price": "110", "status": "new"},
    ]
    reconciled = BrokerReconciler(enabled, database, broker).reconcile()
    assert not reconciled.reconciled
    assert database.get_state("reconciliation_halt") is True
    assert database.get_position_lifecycle(result.trade_id)["state"] == "EXIT_PENDING"
    assert broker.positions == []


def test_explicit_corporate_action_adjusts_current_state_without_rewriting_thesis(settings, database, proposal, candidate, quote):
    enabled, broker, result = _filled_trade(settings, database, proposal, candidate, quote)
    trade = database.get_trade(result.trade_id)
    original_thesis = database.get_trade_thesis(result.trade_id)
    broker.positions = [Position(symbol="XYZ", quantity=6, average_entry_price=50, current_price=51, market_value=306, current_stop=48)]
    broker.history = [
        _root_fill(result, trade),
        {"event_type": "split", "symbol": "XYZ", "old_qty": 3, "new_qty": 6, "ratio": 2, "adjusted_stop": 48},
    ]
    broker.open_orders = [
        {"id": "split-stop", "parent_order_id": trade["broker_order_id"], "symbol": "XYZ", "side": "sell", "type": "stop", "stop_price": "48", "status": "new"},
        {"id": "split-target", "parent_order_id": trade["broker_order_id"], "symbol": "XYZ", "side": "sell", "type": "limit", "limit_price": "55", "status": "new"},
    ]
    reconciled = BrokerReconciler(enabled, database, broker).reconcile()
    assert reconciled.reconciled
    current = database.get_trade(result.trade_id)
    assert current["shares"] == 6
    assert current["entry_price"] == 50
    assert current["final_stop"] == 48
    assert database.get_trade_thesis(result.trade_id) == original_thesis


def test_unexplained_quantity_change_is_a_reconciliation_mismatch(settings, database, proposal, candidate, quote):
    enabled, broker, result = _filled_trade(settings, database, proposal, candidate, quote)
    trade = database.get_trade(result.trade_id)
    broker.positions = [Position(symbol="XYZ", quantity=6, average_entry_price=50, current_price=51, market_value=306, current_stop=48)]
    broker.history = [_root_fill(result, trade)]
    reconciled = BrokerReconciler(enabled, database, broker).reconcile()
    assert not reconciled.reconciled
    assert "quantity mismatch" in " ".join(reconciled.discrepancies)


def test_gap_through_stop_uses_actual_fill_and_closes_lifecycle(settings, database, proposal, candidate, quote):
    enabled, broker, result = _filled_trade(settings, database, proposal, candidate, quote)
    trade = database.get_trade(result.trade_id)
    now = datetime.now(timezone.utc).isoformat()
    broker.positions = []
    broker.open_orders = []
    broker.history = [
        _root_fill(result, trade),
        {
            "id": "gap-stop-fill",
            "parent_order_id": trade["broker_order_id"],
            "symbol": "XYZ",
            "side": "sell",
            "type": "stop",
            "stop_price": "96",
            "filled_qty": "3",
            "filled_avg_price": "94",
            "status": "filled",
            "filled_at": now,
        },
    ]
    reconciled = BrokerReconciler(enabled, database, broker).reconcile()
    assert reconciled.reconciled
    closed = database.get_trade(result.trade_id)
    assert closed["exit_price"] == 94
    assert closed["reason_exit"] == "protective stop gap-through fill"
    assert database.get_position_lifecycle(result.trade_id)["state"] == "CLOSED"
