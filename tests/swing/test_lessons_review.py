from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

from src.swing.lessons_review import review_observations
from src.swing.postmortem import PostmortemEngine


def _closed_trade(database, settings, candidate, *, exit_price: float, index: int) -> str:
    trade_id = str(uuid4())
    database.add_trade({
        "trade_id": trade_id, "decision_id": str(uuid4()), "candidate_id": candidate.candidate_id,
        "ticker": candidate.ticker, "setup_type": candidate.setup_type.value, "status": "OPEN",
        "strategy_version": settings.strategy_version,
        "entry_datetime": (datetime.now(timezone.utc) - timedelta(days=5)).isoformat(), "entry_price": 100,
        "initial_stop": 96, "final_stop": 96, "target": 108, "shares": 2,
        "planned_dollar_risk": 8, "planned_account_risk_pct": 0.004, "planned_rr": 2,
        "market_regime": candidate.market_regime.value, "candidate_score": candidate.score,
        "risk_validation_result": "approved",
    })
    PostmortemEngine(database).close_and_review(
        trade_id, exit_price=exit_price, exit_datetime=datetime.now(timezone.utc) + timedelta(seconds=index),
        high_during_trade=max(exit_price, 102), low_during_trade=min(exit_price, 95),
        reason_exit="test exit",
    )
    return trade_id


def test_review_observations_requires_statistical_sample(database, settings, candidate, tmp_path):
    database.record_candidate(candidate)
    reports_dir = tmp_path / "reports"

    for index in range(29):
        _closed_trade(database, settings, candidate, exit_price=104, index=index)
    assert review_observations(database, settings, reports_dir) == []

    _closed_trade(database, settings, candidate, exit_price=104, index=29)
    results = review_observations(database, settings, reports_dir)
    assert len(results) >= 1
    matching = [row for row in results if row["setup_type"] == candidate.setup_type.value and row["market_regime"] is None]
    assert matching and matching[0]["sample_size"] >= 30
    assert matching[0]["newly_proposed"] is True

    # Proposing a hypothesis must never validate or promote anything on its own.
    production = database.rows("SELECT strategy_version FROM strategy_versions WHERE status='PRODUCTION'")
    assert production == [{"strategy_version": settings.strategy_version}]
    hypotheses = database.rows("SELECT status, human_approved FROM hypotheses")
    assert all(row["status"] == "PROPOSED" and row["human_approved"] == 0 for row in hypotheses)

    again = review_observations(database, settings, reports_dir)
    again_matching = [row for row in again if row["setup_type"] == candidate.setup_type.value and row["market_regime"] is None]
    assert again_matching[0]["newly_proposed"] is False
    assert again_matching[0]["hypothesis_id"] == matching[0]["hypothesis_id"]
