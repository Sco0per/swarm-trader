from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from pydantic import ValidationError

from src.swing.agents import PMStructuredDecision, TechnicalSwingAnalysis
from src.swing.models import BrokerAsset, Position
from src.swing.risk import EXPOSURE_CLUSTERS, SwingRiskManager, TICKER_CLUSTERS


def _review(settings, database, proposal, candidate, quote, portfolio, intent="risk-engine"):
    return SwingRiskManager(settings, database).validate_entry(proposal, candidate, quote, portfolio, intent)


def _geometry(candidate, proposal, quote, *, symbol="XYZ", price=100.0, stop=96.0, target=108.0, atr=4.0):
    candidate = candidate.model_copy(
        update={
            "ticker": symbol,
            "price": price,
            "support": stop,
            "structural_invalidation": stop,
            "resistance": target,
            "atr": atr,
        }
    )
    proposal = proposal.model_copy(update={"ticker": symbol, "entry": price, "stop": stop, "target": target})
    quote = quote.model_copy(update={"symbol": symbol, "last": price, "bid": price * 0.9995, "ask": price * 1.0005})
    return candidate, proposal, quote


def _add_trade(database, settings, *, symbol, risk, sector="Technology", status="SUBMITTED", shares=0, entry=100, stop=96):
    database.add_trade(
        {
            "trade_id": str(uuid4()),
            "decision_id": str(uuid4()),
            "ticker": symbol,
            "setup_type": "TREND_PULLBACK",
            "status": status,
            "strategy_version": settings.strategy_version,
            "entry_datetime": (datetime.now(timezone.utc) - timedelta(days=30)).isoformat(),
            "entry_price": entry,
            "initial_stop": stop,
            "final_stop": stop,
            "target": entry + 8,
            "shares": shares,
            "planned_dollar_risk": risk,
            "planned_account_risk_pct": risk / 2_000,
            "planned_rr": 2,
            "market_regime": "BULL",
            "sector": sector,
            "candidate_score": 88,
            "risk_validation_result": "approved",
        }
    )


@pytest.mark.parametrize(
    ("price", "stop", "target", "expected"),
    [(100, 96, 108, 3), (50, 48, 54, 7), (20, 19, 22, 15)],
)
def test_risk_sizing_uses_point_seven_five_percent_and_whole_shares(settings, database, proposal, candidate, quote, portfolio, price, stop, target, expected):
    candidate, proposal, quote = _geometry(candidate, proposal, quote, price=price, stop=stop, target=target, atr=price - stop)
    result = _review(settings, database, proposal, candidate, quote, portfolio)
    assert result.approved
    assert result.allowed_dollar_risk == 15
    assert result.risk_constraints["risk"] == expected
    assert result.quantity == expected
    assert isinstance(result.quantity, int)


def test_expensive_stock_risk_quantity_floors_to_zero_and_is_persisted(settings, database, proposal, candidate, quote, portfolio):
    candidate, proposal, quote = _geometry(candidate, proposal, quote, price=1_000, stop=980, target=1_040, atr=10)
    result = _review(settings, database, proposal, candidate, quote, portfolio)
    assert not result.approved
    assert result.reason_code == "REJECT_QTY_ZERO"
    summary = database.sizing_rejection_summary((datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat())
    assert summary["quantity_rounding_to_zero"] == {
        "count": 1,
        "candidates": [{"ticker": "XYZ", "price": 1000.0, "risk_per_share": 20.0}],
    }


def test_insufficient_cash_has_distinct_reason_and_persisted_price(settings, database, proposal, candidate, quote, portfolio):
    account = portfolio.account.model_copy(update={"cash": 50, "buying_power": 50})
    result = _review(settings, database, proposal, candidate, quote, portfolio.model_copy(update={"account": account}))
    assert not result.approved
    assert result.reason_code == "REJECT_INSUFFICIENT_CASH"
    stored = database.rows("SELECT ticker,computed_values_json FROM risk_rejections")
    assert stored[0]["ticker"] == "XYZ"
    assert '"price": 100.0' in stored[0]["computed_values_json"]


def test_tight_valid_stop_cannot_create_oversized_position(settings, database, proposal, candidate, quote, portfolio):
    candidate, proposal, quote = _geometry(candidate, proposal, quote, stop=99, target=103, atr=1)
    result = _review(settings, database, proposal, candidate, quote, portfolio)
    assert result.approved
    assert result.risk_constraints["risk"] == 15
    assert result.risk_constraints["position_allocation"] == 7
    assert result.quantity == 7


def test_broker_minimum_is_exactly_one_whole_share(settings):
    assert settings.broker_min_quantity == 1
    with pytest.raises(ValueError, match="immutable at one whole share"):
        replace(settings, broker_min_quantity=2)


def test_liquidity_cap_can_only_reduce_and_reject(settings, database, proposal, candidate, quote, portfolio):
    strict = replace(settings, maximum_adv_fraction=1e-7)
    result = _review(strict, database, proposal, candidate, quote, portfolio)
    assert not result.approved
    assert result.reason_code == "REJECT_LIQUIDITY"


def test_open_risk_is_rebuilt_from_durable_trade_state(settings, database, proposal, candidate, quote, portfolio):
    _add_trade(database, settings, symbol="AAA", risk=39)
    result = _review(settings, database, proposal, candidate, quote, portfolio)
    assert not result.approved
    assert result.reason_code == "REJECT_OPEN_RISK"
    snapshot = database.rows("SELECT * FROM risk_snapshots ORDER BY snapshot_id DESC LIMIT 1")[0]
    assert snapshot["total_open_risk"] == 39
    assert snapshot["open_risk_percent"] == pytest.approx(0.0195)


def test_stop_above_cost_counts_as_zero_not_negative_risk(settings, database, portfolio):
    _add_trade(database, settings, symbol="AAA", risk=8, shares=2, entry=100, stop=105)
    state = portfolio.model_copy(update={"positions": [Position(symbol="AAA", quantity=2, average_entry_price=100, current_price=110, market_value=220, current_stop=105)]})
    snapshot, error = SwingRiskManager(settings, database)._open_risk(state)
    assert error is None
    assert snapshot is not None
    assert snapshot["risk_by_position"]["AAA"] == 0
    assert snapshot["total_open_risk"] == 0


def test_sector_limit_blocks_marginal_trade(settings, database, proposal, candidate, quote, portfolio):
    _add_trade(database, settings, symbol="AAPL", risk=30, sector="Technology")
    result = _review(settings, database, proposal, candidate, quote, portfolio)
    assert not result.approved
    assert result.reason_code == "REJECT_SECTOR_RISK"


def test_semiconductor_cluster_treats_nvda_amd_avgo_tsm_smci_as_one_risk(settings, database, proposal, candidate, quote, portfolio):
    names = {"NVDA", "AMD", "AVGO", "TSM", "SMCI"}
    assert {TICKER_CLUSTERS[name] for name in names} == {"Semiconductors"}
    _add_trade(database, settings, symbol="NVDA", risk=10)
    _add_trade(database, settings, symbol="AMD", risk=10)
    candidate, proposal, quote = _geometry(candidate, proposal, quote, symbol="SMCI")
    result = _review(settings, database, proposal, candidate, quote, portfolio)
    assert not result.approved
    assert result.reason_code == "REJECT_CLUSTER_RISK"


def test_all_required_clusters_exist():
    assert set(EXPOSURE_CLUSTERS) == {
        "Semiconductors",
        "Mega-cap technology",
        "High-beta growth",
        "Financials",
        "Healthcare",
        "Defensives",
        "Industrials",
        "Consumer",
        "Energy",
    }


def test_unmapped_ticker_fails_closed(settings, database, proposal, candidate, quote, portfolio):
    candidate, proposal, quote = _geometry(candidate, proposal, quote, symbol="UNMAPPED")
    result = _review(settings, database, proposal, candidate, quote, portfolio)
    assert result.reason_code == "REJECT_NO_CLUSTER_MAPPING"


def test_weekly_drawdown_halt_is_durable(settings, database, proposal, candidate, quote, portfolio):
    database.record_equity_snapshot(equity=2_000, cash=2_000, buying_power=2_000, drawdown_pct=0, execution_mode="paper")
    weekly_account = portfolio.account.model_copy(update={"equity": 1_900})
    weekly = _review(settings, database, proposal, candidate, quote, portfolio.model_copy(update={"account": weekly_account}), "weekly")
    assert weekly.reason_code == "REJECT_DRAWDOWN_HALT"
    assert database.get_state("weekly_drawdown_halt") is True
    event = database.rows("SELECT * FROM halt_events WHERE halt_key='weekly_drawdown_halt'")[0]
    assert event["reason"].startswith("Weekly drawdown reached")


def test_portfolio_drawdown_halt_is_durable(settings, database, proposal, candidate, quote, portfolio):
    database.record_equity_snapshot(
        observed_at=(datetime.now(timezone.utc) - timedelta(days=30)).isoformat(),
        equity=2_000,
        cash=2_000,
        buying_power=2_000,
        drawdown_pct=0,
        execution_mode="paper",
    )
    portfolio_account = portfolio.account.model_copy(update={"equity": 1_840})
    halted = _review(settings, database, proposal, candidate, quote, portfolio.model_copy(update={"account": portfolio_account}), "portfolio")
    assert halted.rule == "drawdown_halt"
    assert database.get_state("drawdown_halt") is True


def test_kill_switch_and_loss_streak_are_reread_from_database(settings, database, proposal, candidate, quote, portfolio):
    database.set_state("kill_switch", True, "test")
    assert _review(settings, database, proposal, candidate, quote, portfolio).reason_code == "REJECT_KILL_SWITCH"
    database.set_state("kill_switch", False, "test")
    database.set_state("loss_streak_halt", True, "test")
    assert _review(settings, database, proposal, candidate, quote, portfolio, "loss").reason_code == "REJECT_LOSS_STREAK"


@pytest.mark.parametrize("status", ["unknown", "stale", "conflicting", "malformed", "blocked"])
def test_major_event_data_fails_closed(settings, database, proposal, candidate, quote, portfolio, status):
    result = _review(settings, database, proposal, candidate.model_copy(update={"major_event_status": status}), quote, portfolio)
    assert result.reason_code == "REJECT_EARNINGS_WINDOW"


def test_earnings_unavailable_and_inside_window_fail_closed(settings, database, proposal, candidate, quote, portfolio):
    unavailable = _review(settings, database, proposal, candidate.model_copy(update={"earnings_trading_days": None, "earnings_data_status": "unavailable"}), quote, portfolio)
    inside = _review(settings, database, proposal, candidate.model_copy(update={"earnings_trading_days": 2}), quote, portfolio, "earnings")
    assert unavailable.reason_code == inside.reason_code == "REJECT_EARNINGS_WINDOW"


@pytest.mark.parametrize("status", ["stale", "conflicting", "malformed"])
def test_earnings_status_fails_closed(settings, database, proposal, candidate, quote, portfolio, status):
    result = _review(settings, database, proposal, candidate.model_copy(update={"earnings_data_status": status}), quote, portfolio)
    assert result.reason_code == "REJECT_EARNINGS_WINDOW"


def test_existing_position_event_policy_exits_before_binary_event(settings, database, candidate):
    manager = SwingRiskManager(settings, database)
    assert manager.existing_position_event_action(candidate.model_copy(update={"earnings_trading_days": 2})) == "EXIT_BEFORE_EARNINGS"
    assert manager.existing_position_event_action(candidate.model_copy(update={"major_event_status": "stale"})) == "EXIT_REVIEW_REQUIRED_EVENT_DATA_UNSAFE"


def test_halted_restricted_stale_and_wide_spread_are_rejected(settings, database, proposal, candidate, quote, portfolio):
    halted = _review(settings, database, proposal, candidate.model_copy(update={"is_halted": True}), quote, portfolio, "halted")
    restricted_asset = BrokerAsset(symbol="XYZ", asset_class="us_equity", tradable=False, status="inactive")
    restricted = SwingRiskManager(settings, database).validate_entry(proposal, candidate, quote, portfolio, "restricted", asset=restricted_asset)
    stale_time = datetime.now(timezone.utc) - timedelta(minutes=10)
    stale = _review(settings, database, proposal, candidate, quote.model_copy(update={"retrieved_at": stale_time}), portfolio, "stale")
    wide = _review(settings, database, proposal, candidate, quote.model_copy(update={"bid": 99, "ask": 101}), portfolio, "wide")
    assert halted.rule == "trading_halt"
    assert restricted.rule == "asset_not_tradable"
    assert stale.reason_code == "REJECT_STALE_DATA"
    assert wide.reason_code == "REJECT_WIDE_SPREAD"


@pytest.mark.parametrize(("stop", "atr", "rule"), [(99.5, 2, "stop_too_tight"), (85, 3, "stop_too_wide")])
def test_atr_sanity_bounds_reject_implausible_stops(settings, database, proposal, candidate, quote, portfolio, stop, atr, rule):
    candidate, proposal, quote = _geometry(candidate, proposal, quote, stop=stop, target=140, atr=atr)
    result = _review(settings, database, proposal, candidate, quote, portfolio)
    assert result.rule == rule
    assert result.reason_code == "REJECT_SETUP_INVALID"


@pytest.mark.parametrize(
    "change",
    [
        {"normal_risk_pct": 0.0101},
        {"max_combined_open_risk_pct": 0.0201},
        {"max_sector_open_risk_pct": 0.0151},
        {"max_cluster_open_risk_pct": 0.0101},
        {"weekly_drawdown_halt_pct": 0.0501},
        {"max_position_exposure_pct": 0.3501},
    ],
)
def test_startup_rejects_weakened_immutable_limits(settings, change):
    with pytest.raises(ValueError):
        replace(settings, **change)


def test_llm_schemas_have_no_stop_quantity_or_limit_authority(candidate, proposal, quote, settings, database, portfolio):
    prohibited = {"entry", "stop", "target", "quantity", "risk_per_trade", "limit", "max_open_risk"}
    assert prohibited.isdisjoint(TechnicalSwingAnalysis.model_fields)
    assert prohibited.isdisjoint(PMStructuredDecision.model_fields)
    with pytest.raises(ValidationError):
        PMStructuredDecision.model_validate(
            {
                "ticker": "XYZ",
                "decision": "BUY",
                "setup_type": "TREND_PULLBACK",
                "confidence_score": 90,
                "market_regime": "BULL",
                "bull_case": "x",
                "bear_case": "y",
                "invalidation": "z",
                "event_risk": "none",
                "reasoning": "x",
                "stop": 1,
            }
        )
    manipulated = proposal.model_copy(update={"stop": 1, "target": 10_000})
    result = _review(settings, database, manipulated, candidate, quote, portfolio)
    assert result.authoritative_stop == candidate.structural_invalidation
    assert result.quantity <= result.risk_constraints["risk"]
