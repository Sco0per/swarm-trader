from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from pydantic import ValidationError

from src.swing.config import SwingSettings
from src.swing.database import SwingDatabase
from src.swing.models import Position, TradeProposal
from src.swing.risk import SwingRiskManager


def review(settings, database, proposal, candidate, quote, portfolio, intent="intent-1"):
    return SwingRiskManager(settings, database).validate_entry(proposal, candidate, quote, portfolio, intent)


def test_position_size_uses_technical_risk_and_rounds_down(settings, database, proposal, candidate, quote, portfolio):
    result = review(settings, database, proposal, candidate, quote, portfolio)
    assert result.approved
    assert result.allowed_dollar_risk == 15
    assert result.quantity == 3
    assert result.planned_dollar_risk == 12
    assert result.planned_account_risk_pct == pytest.approx(0.006)


def test_position_size_never_increases_risk_to_fit_one_share(settings, database, proposal, candidate, quote, portfolio):
    tight = candidate.model_copy(update={"atr": 1, "structural_invalidation": 99})
    result = review(settings, database, proposal, tight, quote, portfolio)
    assert result.approved
    assert result.risk_constraints["risk"] == 15
    assert result.risk_constraints["position_allocation"] == 7
    assert result.quantity == 7


@pytest.mark.parametrize(
    ("field", "value", "rule"),
    [("new_positions_today", 1, "daily_entry_limit"), ("new_positions_this_week", 3, "weekly_entry_limit"), ("consecutive_losses", 3, "consecutive_loss_halt")],
)
def test_activity_halts(settings, database, proposal, candidate, quote, portfolio, field, value, rule):
    result = review(settings, database, proposal, candidate, quote, portfolio.model_copy(update={field: value}))
    assert not result.approved and result.rule == rule


def test_max_three_positions(settings, database, proposal, candidate, quote, portfolio):
    positions = [Position(symbol=f"T{i}", quantity=1, average_entry_price=10, current_price=10, market_value=10) for i in range(3)]
    result = review(settings, database, proposal, candidate, quote, portfolio.model_copy(update={"positions": positions}))
    assert not result.approved and result.rule == "max_positions"


def test_never_adds_or_averages_down(settings, database, proposal, candidate, quote, portfolio):
    position = Position(symbol="XYZ", quantity=1, average_entry_price=105, current_price=100, market_value=100)
    result = review(settings, database, proposal, candidate, quote, portfolio.model_copy(update={"positions": [position]}))
    assert not result.approved and result.rule == "no_adding"


def test_combined_open_risk(settings, database, proposal, candidate, quote, portfolio):
    database.add_trade(
        {
            "trade_id": str(uuid4()),
            "decision_id": str(uuid4()),
            "ticker": "AAA",
            "setup_type": "TREND_PULLBACK",
            "status": "SUBMITTED",
            "strategy_version": settings.strategy_version,
            "initial_stop": 61,
            "final_stop": 61,
            "target": 108,
            "shares": 0,
            "planned_dollar_risk": 39,
            "planned_account_risk_pct": 0.0195,
            "planned_rr": 2,
            "market_regime": "BULL",
            "sector": "Technology",
            "candidate_score": 88,
            "risk_validation_result": "approved",
        }
    )
    result = review(settings, database, proposal, candidate, quote, portfolio)
    assert not result.approved and result.rule == "max_combined_open_risk"


def test_five_percent_drawdown_reduces_risk(settings, database, proposal, candidate, quote, portfolio):
    state = portfolio.model_copy(update={"equity_high": 2_000, "drawdown_pct": 0.05})
    result = review(settings, database, proposal, candidate, quote, state)
    assert result.approved
    assert result.applied_risk_pct == 0.003


def test_eight_percent_drawdown_latches_halt(settings, database, proposal, candidate, quote, portfolio):
    result = review(settings, database, proposal, candidate, quote, portfolio.model_copy(update={"drawdown_pct": 0.08}))
    assert not result.approved and result.rule == "drawdown_halt"
    assert database.get_state("drawdown_halt") is True


def test_stop_cannot_widen(settings, database):
    manager = SwingRiskManager(settings, database)
    assert manager.validate_stop_change("XYZ", 96, 97).approved
    assert not manager.validate_stop_change("XYZ", 96, 95).approved


def test_minimum_reward_risk(settings, database, proposal, candidate, quote, portfolio):
    bad_candidate = candidate.model_copy(update={"resistance": 107})
    result = review(settings, database, proposal, bad_candidate, quote, portfolio)
    assert not result.approved and result.rule == "minimum_rr"
    exceptional = proposal.model_copy(update={"exceptional_rr_evidence": True})
    assert not review(settings, database, exceptional, bad_candidate, quote, portfolio, "intent-2").approved


@pytest.mark.parametrize(("days", "rule"), [(5, "earnings_exclusion"), (None, "earnings_unknown")])
def test_earnings_exclusion(settings, database, proposal, candidate, quote, portfolio, days, rule):
    result = review(settings, database, proposal, candidate.model_copy(update={"earnings_trading_days": days}), quote, portfolio)
    assert not result.approved and result.rule == rule


def test_long_term_blacklist(tmp_path, proposal, candidate, quote, portfolio):
    path = tmp_path / "blacklist.yaml"
    path.write_text("symbols:\n  - XYZ\nsubstantially_identical: []\n", encoding="utf-8")
    settings = SwingSettings(database_path=tmp_path / "db.sqlite", do_not_trade_path=path)
    database = SwingDatabase(settings.database_path)
    database.initialize()
    result = review(settings, database, proposal, candidate, quote, portfolio)
    assert not result.approved and result.rule == "long_term_blacklist"


def test_leveraged_etf_rejected(settings, database, proposal, candidate, quote, portfolio):
    candidate = candidate.model_copy(update={"ticker": "TQQQ", "is_etf": True, "is_leveraged_or_inverse": True})
    proposal = proposal.model_copy(update={"ticker": "TQQQ"})
    quote = quote.model_copy(update={"symbol": "TQQQ"})
    result = review(settings, database, proposal, candidate, quote, portfolio)
    assert not result.approved and result.rule == "banned_instrument"


def test_short_decision_is_not_in_schema(proposal):
    with pytest.raises(ValidationError):
        TradeProposal.model_validate({**proposal.model_dump(mode="json"), "decision": "SHORT"})


def test_stale_quote_rejected(settings, database, proposal, candidate, quote, portfolio):
    stale = datetime.now(timezone.utc) - timedelta(minutes=10)
    quote = quote.model_copy(update={"retrieved_at": stale, "market_timestamp": stale})
    result = review(settings, database, proposal, candidate, quote, portfolio)
    assert not result.approved and result.rule == "stale_quote"


def test_duplicate_intent_rejected(settings, database, proposal, candidate, quote, portfolio):
    assert database.reserve_intent(intent_id="same", decision_id=proposal.decision_id, ticker="XYZ", side="buy", quantity=2, payload={})
    result = review(settings, database, proposal, candidate, quote, portfolio, "same")
    assert not result.approved and result.rule == "duplicate_intent"


def _add_closed_loss(database, settings, candidate, exit_time):
    database.record_candidate(candidate)
    database.add_trade(
        {
            "trade_id": str(uuid4()),
            "decision_id": str(uuid4()),
            "candidate_id": candidate.candidate_id,
            "ticker": "XYZ",
            "setup_type": candidate.setup_type.value,
            "status": "CLOSED",
            "strategy_version": settings.strategy_version,
            "entry_datetime": (exit_time - timedelta(days=5)).isoformat(),
            "exit_datetime": exit_time.isoformat(),
            "entry_price": 100,
            "exit_price": 96,
            "initial_stop": 96,
            "final_stop": 96,
            "target": 108,
            "shares": 2,
            "planned_dollar_risk": 8,
            "planned_account_risk_pct": 0.004,
            "planned_rr": 2,
            "realized_pnl": -8,
            "realized_r": -1,
            "market_regime": candidate.market_regime.value,
            "candidate_score": candidate.score,
            "risk_validation_result": "approved",
        }
    )


def test_post_loss_cooldown(settings, database, proposal, candidate, quote, portfolio):
    _add_closed_loss(database, settings, candidate, datetime.now(timezone.utc))
    result = review(settings, database, proposal, candidate, quote, portfolio)
    assert not result.approved and result.rule == "loss_cooldown"


def test_post_cooldown_reentry_requires_explicit_answers(settings, database, proposal, candidate, quote, portfolio):
    _add_closed_loss(database, settings, candidate, datetime.now(timezone.utc) - timedelta(days=30))
    missing = review(settings, database, proposal, candidate, quote, portfolio)
    assert not missing.approved and missing.rule == "reentry_review_missing"
    context = {"what_failed": "normal variance", "what_changed": "new base", "failure_resolved": "support recovered", "new_setup": "yes"}
    approved = review(settings, database, proposal.model_copy(update={"reentry_context": context}), candidate, quote, portfolio, "new-intent")
    assert approved.approved


def test_malformed_proposal_and_missing_fields_rejected(proposal):
    data = proposal.model_dump(mode="json")
    data.pop("stop")
    with pytest.raises(ValidationError):
        TradeProposal.model_validate(data)
    with pytest.raises(ValidationError):
        TradeProposal.model_validate({**proposal.model_dump(mode="json"), "stop": 101})
