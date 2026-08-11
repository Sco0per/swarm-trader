from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import numpy as np
import pandas as pd
import pytest

from portfolio_monitor import check_hard_stop, check_trailing_stop, run_monitor
from src.swing.brokers.alpaca import AlpacaPaperProvider
from src.swing.brokers.fake import FakeBrokerProvider
from src.swing.config import SwingSettings
from src.swing.database import SwingDatabase
from src.swing.execution import SwingExecutionService
from src.swing.market import DeterministicSwingScanner, UniverseAsset
from src.swing.models import BrokerAsset, BrokerOrder, Position
from src.swing.postmortem import PostmortemEngine
from src.swing.reconciliation import BrokerReconciler
from src.swing.risk import SwingRiskManager
from src.swing.stops import ProtectedStopService


def _add_open_trade(database, settings, candidate, *, ticker="XYZ", broker_order_id="root-1"):
    database.record_candidate(candidate)
    trade_id = str(uuid4())
    database.add_trade({
        "trade_id": trade_id,
        "decision_id": str(uuid4()),
        "intent_id": str(uuid4()),
        "candidate_id": candidate.candidate_id,
        "ticker": ticker,
        "setup_type": candidate.setup_type.value,
        "status": "OPEN",
        "strategy_version": settings.strategy_version,
        "entry_datetime": (datetime.now(timezone.utc) - timedelta(days=2)).isoformat(),
        "entry_price": 100,
        "initial_stop": 96,
        "final_stop": 96,
        "target": 108,
        "shares": 2,
        "position_value": 200,
        "planned_dollar_risk": 8,
        "planned_account_risk_pct": 0.004,
        "planned_rr": 2,
        "market_regime": candidate.market_regime.value,
        "sector": candidate.sector,
        "candidate_score": candidate.score,
        "broker_provider": "fake-paper",
        "broker_order_id": broker_order_id,
        "risk_validation_result": "approved",
    })
    return trade_id


def test_legacy_monitor_is_disabled_and_zero_thresholds_never_trigger():
    flat = {"avg_entry_price": "100", "current_price": "100"}
    profitable = {"avg_entry_price": "100", "current_price": "105"}
    assert check_hard_stop(flat, 0) == (False, "")
    assert check_trailing_stop(profitable, 110, 0) == (False, "")
    result = run_monitor(dry_run=False, mode="swing")
    assert result["status"] == "DISABLED_FAIL_CLOSED"
    assert result["actions"] == []


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"normal_risk_pct": 0.0051}, "0.50%"),
        ({"a_plus_risk_pct": 0.0076}, "0.75%"),
        ({"absolute_max_risk_pct": 0.0101}, "1.00%"),
        ({"max_combined_open_risk_pct": 0.0201}, "2.00%"),
        ({"buy_score_threshold": 79}, "below 80"),
        ({"max_open_positions": 4}, "one and three"),
        ({"max_new_positions_day": 2}, "one per day"),
        ({"quote_freshness_seconds": 301}, "between 1 and 300"),
    ],
)
def test_hard_settings_cannot_be_weakened(settings, changes, message):
    with pytest.raises(ValueError, match=message):
        replace(settings, **changes)


def test_a_plus_request_is_capped_at_point_seven_five_percent(
    settings, database, proposal, candidate, quote, portfolio,
):
    candidate = candidate.model_copy(update={"score": 90})
    proposal = proposal.model_copy(update={
        "candidate_score": 90,
        "confidence_score": 90,
        "requested_risk_pct": 0.01,
    })
    result = SwingRiskManager(settings, database).validate_entry(proposal, candidate, quote, portfolio, "a-plus")
    assert result.approved
    assert result.applied_risk_pct == pytest.approx(0.0075)
    assert result.allowed_dollar_risk == 15
    assert result.planned_account_risk_pct <= settings.absolute_max_risk_pct


def test_future_market_timestamp_is_rejected(settings, database, proposal, candidate, quote, portfolio):
    future = datetime.now(timezone.utc) + timedelta(minutes=2)
    quote = quote.model_copy(update={"market_timestamp": future})
    result = SwingRiskManager(settings, database).validate_entry(proposal, candidate, quote, portfolio, "future")
    assert not result.approved
    assert result.rule == "future_market_data"


def test_alpaca_swing_bracket_uses_gtc():
    provider = AlpacaPaperProvider("key", "secret")
    captured = {}

    def request(method, url, **kwargs):
        captured.update(kwargs["json"])
        return {
            "id": "order-1",
            "symbol": "XYZ",
            "side": "buy",
            "qty": "2",
            "status": "accepted",
            "filled_qty": "0",
        }

    provider._request = request
    from src.swing.models import OrderIntent
    provider.place_order(OrderIntent(
        intent_id="intent", decision_id="decision", ticker="XYZ", quantity=2,
        limit_price=100, stop_price=96, target_price=108, strategy_version="SWING_V1.0",
    ))
    assert captured["time_in_force"] == "gtc"
    assert captured["order_class"] == "bracket"


def test_alpaca_account_uses_broker_multiplier_for_margin_flag():
    provider = AlpacaPaperProvider("key", "secret")
    provider._request = lambda *args, **kwargs: {
        "id": "paper", "equity": "2000", "cash": "2000", "buying_power": "8000", "multiplier": "4",
    }
    assert provider.get_account().is_margin_enabled is True


def test_margin_enabled_live_account_is_rejected(settings, database, proposal, candidate, quote, portfolio):
    live = replace(
        settings,
        execution_mode="live",
        trading_enabled=True,
        live_acknowledgement="I_ACKNOWLEDGE_LIVE_RISK",
    )
    state = portfolio.model_copy(update={
        "account": portfolio.account.model_copy(update={
            "is_paper": False, "is_margin_enabled": True, "dedicated_agentic_account": True,
        }),
    })
    result = SwingRiskManager(live, database).validate_entry(proposal, candidate, quote, state, "margin")
    assert not result.approved and result.rule == "margin_forbidden"


def test_broker_asset_classification_can_veto_candidate(settings, database, proposal, candidate, quote):
    broker = FakeBrokerProvider(
        quotes={"XYZ": quote},
        assets={"XYZ": BrokerAsset(symbol="XYZ", asset_class="us_equity", tradable=False, status="inactive")},
    )
    result = SwingExecutionService(settings, database, broker).submit(proposal, candidate)
    assert result.status == "REJECTED"
    assert result.risk.rule == "asset_not_tradable"


def test_stop_widening_is_blocked_at_provider_and_service(settings, database, candidate):
    trade_id = _add_open_trade(database, settings, candidate)
    broker = FakeBrokerProvider()
    broker.orders["stop-leg"] = BrokerOrder(
        broker_order_id="stop-leg", intent_id="protective", symbol="XYZ", side="sell", quantity=2, status="accepted",
        raw={"stop_price": 96},
    )
    with pytest.raises(ValueError, match="widen"):
        broker.replace_stop("stop-leg", 95)
    service = ProtectedStopService(database, broker, SwingRiskManager(settings, database))
    service.tighten(trade_id, "stop-leg", 97)
    assert database.get_trade(trade_id)["final_stop"] == 97
    with pytest.raises(ValueError, match="widen"):
        service.tighten(trade_id, "stop-leg", 96)


def test_simultaneous_candidates_serialize_to_one_daily_admission(settings, database, proposal, candidate, quote):
    enabled = replace(settings, trading_enabled=True)
    candidate_two = candidate.model_copy(update={"candidate_id": str(uuid4()), "ticker": "ABC"})
    proposal_two = proposal.model_copy(update={"decision_id": str(uuid4()), "ticker": "ABC"})
    quote_two = quote.model_copy(update={"symbol": "ABC"})
    broker = FakeBrokerProvider(quotes={"XYZ": quote, "ABC": quote_two})
    service = SwingExecutionService(enabled, database, broker)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(
            lambda values: service.submit(values[0], values[1], dry_run=False),
            [(proposal, candidate), (proposal_two, candidate_two)],
        ))

    assert sum(result.status == "SUBMITTED" for result in results) == 1
    assert sum(result.risk.rule == "daily_entry_limit" for result in results) == 1
    assert broker.place_calls == 1


def test_terminal_broker_rejection_releases_admission_without_trade(settings, database, proposal, candidate, quote):
    enabled = replace(settings, trading_enabled=True)
    broker = FakeBrokerProvider(quotes={"XYZ": quote})
    broker.next_status = "rejected"
    result = SwingExecutionService(enabled, database, broker).submit(proposal, candidate, dry_run=False)
    assert result.status == "REJECTED"
    assert database.open_trades() == []
    assert database.active_admissions() == []


def test_reconciliation_rejects_short_or_manual_position(settings, database):
    broker = FakeBrokerProvider(positions=[Position(
        symbol="XYZ", quantity=-2, average_entry_price=100, current_price=99, market_value=-198, side="short",
    )])
    result = BrokerReconciler(settings, database, broker).reconcile()
    assert not result.reconciled
    assert any("Unsupported broker position side" in item for item in result.discrepancies)
    assert database.get_state("reconciliation_halt") is True


def test_unknown_outcome_halts_then_reconciles_after_restart(settings, database, proposal, candidate, quote):
    enabled = replace(settings, trading_enabled=True)
    broker = FakeBrokerProvider(quotes={"XYZ": quote})
    broker.raise_on_place = TimeoutError("unknown outcome")
    unknown = SwingExecutionService(enabled, database, broker).submit(proposal, candidate, dry_run=False)
    assert unknown.status == "UNKNOWN_REQUIRES_RECONCILIATION"
    assert database.get_state("reconciliation_halt") is True

    broker.raise_on_place = None
    broker.positions = [Position(
        symbol="XYZ", quantity=2, average_entry_price=100, current_price=101, market_value=202,
    )]
    broker.history = [{
        "id": "recovered-order",
        "client_order_id": unknown.intent_id[:48],
        "symbol": "XYZ",
        "side": "buy",
        "qty": "2",
        "filled_qty": "2",
        "filled_avg_price": "100",
        "status": "filled",
        "submitted_at": datetime.now(timezone.utc).isoformat(),
    }]
    reopened = SwingDatabase(settings.database_path)
    reopened.initialize(settings.strategy_version)
    result = BrokerReconciler(enabled, reopened, broker).reconcile()
    assert result.reconciled
    assert result.recovered_admissions == 1
    assert reopened.get_state("reconciliation_halt") is False
    assert reopened.open_trades()[0]["broker_order_id"] == "recovered-order"


def test_reconciliation_of_broker_exit_runs_postmortem(settings, database, proposal, candidate, quote):
    enabled = replace(settings, trading_enabled=True)
    broker = FakeBrokerProvider(quotes={"XYZ": quote})
    submitted = SwingExecutionService(enabled, database, broker).submit(proposal, candidate, dry_run=False)
    trade = database.get_trade(submitted.trade_id)
    broker.history = [
        {
            "id": trade["broker_order_id"], "client_order_id": submitted.intent_id[:48], "symbol": "XYZ",
            "side": "buy", "filled_qty": "2", "filled_avg_price": "100", "status": "filled",
        },
        {
            "id": "sell-leg", "parent_order_id": trade["broker_order_id"], "symbol": "XYZ", "side": "sell",
            "filled_qty": "2", "filled_avg_price": "96", "status": "filled",
            "filled_at": datetime.now(timezone.utc).isoformat(),
        },
    ]
    result = BrokerReconciler(enabled, database, broker).reconcile()
    assert result.reconciled
    assert result.closed_trades == 1
    assert database.get_trade(submitted.trade_id)["status"] == "CLOSED"
    assert database.table_counts()["postmortems"] == 1


def test_partial_exit_preserves_initial_shares_and_uses_weighted_exit(settings, database, proposal, candidate, quote):
    enabled = replace(settings, trading_enabled=True)
    broker = FakeBrokerProvider(quotes={"XYZ": quote})
    submitted = SwingExecutionService(enabled, database, broker).submit(proposal, candidate, dry_run=False)
    trade = database.get_trade(submitted.trade_id)
    now = datetime.now(timezone.utc)
    buy = {
        "id": trade["broker_order_id"], "client_order_id": submitted.intent_id[:48], "symbol": "XYZ",
        "side": "buy", "filled_qty": "2", "filled_avg_price": "100", "status": "filled",
        "filled_at": now.isoformat(),
    }
    first_exit = {
        "id": "profit-leg", "parent_order_id": trade["broker_order_id"], "symbol": "XYZ", "side": "sell",
        "filled_qty": "1", "filled_avg_price": "104", "status": "filled", "filled_at": now.isoformat(),
    }
    broker.history = [buy, first_exit]
    broker.positions = [Position(
        symbol="XYZ", quantity=1, average_entry_price=100, current_price=102, market_value=102,
    )]
    partial = BrokerReconciler(enabled, database, broker).reconcile()
    assert partial.reconciled
    assert database.get_trade(submitted.trade_id)["shares"] == 2

    broker.positions = []
    broker.history.append({
        "id": "stop-leg", "parent_order_id": trade["broker_order_id"], "symbol": "XYZ", "side": "sell",
        "filled_qty": "1", "filled_avg_price": "96", "status": "filled",
        "filled_at": (now + timedelta(minutes=1)).isoformat(),
    })
    closed = BrokerReconciler(enabled, database, broker).reconcile()
    stored = database.get_trade(submitted.trade_id)
    assert closed.reconciled and closed.closed_trades == 1
    assert stored["exit_price"] == pytest.approx(100)
    assert stored["realized_pnl"] == pytest.approx(0)


def test_third_automatic_loss_latches_human_review(settings, database, candidate, tmp_path):
    engine = PostmortemEngine(database, consecutive_loss_halt=3, report_dir=tmp_path)
    for index in range(3):
        trade_id = _add_open_trade(database, settings, candidate, broker_order_id=f"root-{index}")
        engine.close_and_review(
            trade_id,
            exit_price=96,
            exit_datetime=datetime.now(timezone.utc) + timedelta(seconds=index),
            high_during_trade=101,
            low_during_trade=95,
            reason_exit="technical stop",
        )
    assert database.get_state("loss_streak_halt") is True
    assert list(tmp_path.glob("LOSS_STREAK_REVIEW_*.md"))


def test_scanner_scores_viable_asset_and_excludes_banned(settings):
    index = pd.date_range(end=datetime.now(timezone.utc), periods=220, freq="B")
    close = np.linspace(80, 100, len(index))
    frame = pd.DataFrame({
        "open": close - 0.25,
        "high": close + 1,
        "low": close - 1,
        "close": close,
        "volume": np.full(len(index), 2_000_000),
    }, index=index)
    assets = [
        UniverseAsset("GOOD", sector="Technology", earnings_trading_days=10, bid=99.95, ask=100.05),
        UniverseAsset("TQQQ", is_etf=True, bid=99.95, ask=100.05),
        UniverseAsset("HALT", is_halted=True, bid=99.95, ask=100.05),
    ]
    results = DeterministicSwingScanner(settings).scan(
        assets,
        {"GOOD": frame, "TQQQ": frame, "HALT": frame},
        spy_bars=frame,
        qqq_bars=frame,
        source="test-feed",
    )
    assert [candidate.ticker for candidate in results] == ["GOOD"]
    assert sum(results[0].score_components.values()) == pytest.approx(results[0].score)
