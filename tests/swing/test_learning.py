from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from src.autoresearch_swing.fitness import FitnessMetrics
from src.autoresearch_swing.research import SwingResearchEngine
from src.swing.postmortem import PostmortemEngine


def add_open_trade(database, settings, proposal, candidate):
    database.record_candidate(candidate)
    trade_id = str(uuid4())
    database.add_trade(
        {
            "trade_id": trade_id,
            "decision_id": str(uuid4()),
            "candidate_id": candidate.candidate_id,
            "ticker": "XYZ",
            "setup_type": candidate.setup_type.value,
            "status": "OPEN",
            "strategy_version": settings.strategy_version,
            "entry_datetime": (datetime.now(timezone.utc) - timedelta(days=5)).isoformat(),
            "entry_price": 100,
            "initial_stop": 96,
            "final_stop": 96,
            "target": 108,
            "shares": 2,
            "planned_dollar_risk": 8,
            "planned_account_risk_pct": 0.004,
            "planned_rr": 2,
            "market_regime": candidate.market_regime.value,
            "candidate_score": candidate.score,
            "risk_validation_result": "approved",
        }
    )
    return trade_id


def test_closed_trade_creates_postmortem_and_observation(database, settings, proposal, candidate):
    trade_id = add_open_trade(database, settings, proposal, candidate)
    record = PostmortemEngine(database).close_and_review(
        trade_id,
        exit_price=96,
        high_during_trade=102,
        low_during_trade=95.5,
        reason_exit="technical stop",
    )
    assert record.classification.value == "VALID_LOSS"
    assert database.get_trade(trade_id)["realized_r"] == pytest.approx(-1)
    counts = database.table_counts()
    assert counts["postmortems"] == 1 and counts["lessons"] == 1


def test_close_and_review_without_backend_matches_default_behavior(database, settings, proposal, candidate):
    trade_id = add_open_trade(database, settings, proposal, candidate)
    record = PostmortemEngine(database).close_and_review(
        trade_id,
        exit_price=96,
        high_during_trade=102,
        low_during_trade=95.5,
        reason_exit="technical stop",
        backend=None,
    )
    assert record.classification.value == "VALID_LOSS"


class _StubPostmortemBackend:
    def __init__(self, classification: str = "VALID_WIN"):
        self.classification = classification
        self.calls = 0

    def complete(self, *, role, model_name, payload, schema):
        assert role == "postmortem"
        self.calls += 1
        return schema(
            classification=self.classification,
            followed_strategy=True,
            entry_valid=True,
            regime_correct=True,
            sector_favorable=True,
            relative_strength_favorable=True,
            stop_sensible=True,
            target_realistic=True,
            execution_hurt=False,
            unexpected_event=False,
            rule_rationalization=False,
            normal_variance=False,
            repeats_known_lesson=False,
            mistake_type=None,
            reasoning="clean trend continuation win",
        )


def test_close_and_review_uses_llm_backend_for_valid_win(database, settings, proposal, candidate):
    trade_id = add_open_trade(database, settings, proposal, candidate)
    backend = _StubPostmortemBackend()
    record = PostmortemEngine(database).close_and_review(
        trade_id,
        exit_price=108,
        high_during_trade=109,
        low_during_trade=99,
        reason_exit="target hit",
        backend=backend,
        model_name="claude-opus-5",
    )
    assert record.classification.value == "VALID_WIN"
    assert backend.calls == 1


def test_close_and_review_backend_failure_falls_back_to_deterministic_default(database, settings, proposal, candidate):
    class _BrokenBackend:
        def complete(self, **kwargs):
            raise RuntimeError("model unavailable")

    trade_id = add_open_trade(database, settings, proposal, candidate)
    record = PostmortemEngine(database).close_and_review(
        trade_id,
        exit_price=96,
        high_during_trade=102,
        low_during_trade=95.5,
        reason_exit="technical stop",
        backend=_BrokenBackend(),
        model_name="claude-opus-5",
    )
    assert record.classification.value == "VALID_LOSS"


def test_sqlite_journal_survives_new_repository_instance(database, settings, proposal, candidate):
    trade_id = add_open_trade(database, settings, proposal, candidate)
    from src.swing.database import SwingDatabase

    reopened = SwingDatabase(settings.database_path)
    reopened.initialize(settings.strategy_version)
    stored = reopened.get_trade(trade_id)
    assert stored is not None
    assert stored["ticker"] == "XYZ"


def test_invalid_lesson_transition_and_single_trade_validation_blocked(database):
    lesson_id = str(uuid4())
    database.create_observation(lesson_id, "one trade", "t1", 0.1, "SWING_V1.0", None, None, {})
    with pytest.raises(ValueError):
        database.transition_lesson(lesson_id, "VALIDATED", approved_by="human")


def test_autoresearch_requires_multiple_observations(database, settings, tmp_path):
    engine = SwingResearchEngine(database, settings, tmp_path / "reports")
    with pytest.raises(ValueError):
        engine.propose(
            observation="one loss",
            proposed_rule="ban ticker",
            supporting_lesson_ids=["one"],
            supporting_sample_size=1,
            setup_type=None,
            market_regime=None,
            candidate_strategy_version="SWING_V1.1_CANDIDATE",
        )


def test_validation_never_auto_promotes(database, settings, tmp_path):
    engine = SwingResearchEngine(database, settings, tmp_path / "reports")
    hypothesis_id = engine.propose(
        observation="breakouts weak in declining QQQ",
        proposed_rule="require rising QQQ 20MA",
        supporting_lesson_ids=["a", "b", "c", "d", "e"],
        supporting_sample_size=30,
        setup_type="BREAKOUT_RETEST",
        market_regime="NEUTRAL",
        candidate_strategy_version="SWING_V1.1_CANDIDATE",
    )
    baseline = FitnessMetrics(expectancy_r=0.1, profit_factor=1.2, sharpe=0.5, sortino=0.7, max_drawdown=0.06, total_return=0.08, trade_count=50, turnover=2, regime_stability=0.5, performance_concentration=0.4, strategy_complexity=3)
    candidate = baseline.model_copy(update={"expectancy_r": 0.25, "profit_factor": 1.6, "sharpe": 0.9, "sortino": 1.2, "max_drawdown": 0.05, "regime_stability": 0.7})
    result = engine.record_comparison(
        hypothesis_id,
        baseline=baseline,
        candidate=candidate,
        data_hash="data",
        config_hash="config",
        train_period="2018-2021",
        validation_period="2022-2023",
        out_of_sample_period="2024-2025",
        regime_results={"BULL": {"expectancy_r": 0.3}, "CHOPPY": {"expectancy_r": 0.1}},
        benchmark={"SPY": 0.1, "QQQ": 0.12},
        walk_forward={"positive_out_of_sample": True},
    )
    assert result["production_changed"] is False
    production = database.rows("SELECT strategy_version FROM strategy_versions WHERE status='PRODUCTION'")
    assert production == [{"strategy_version": "SWING_V1.0"}]
