from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from src.swing.agents import AgentPipeline
from src.swing.analytics import ExperienceEngine, INSUFFICIENT_SAMPLE
from src.swing.config import ModelSettings
from src.swing.database import DDL, SCHEMA_VERSION, SwingDatabase
from src.swing.decision_versioning import (
    decision_envelope,
    DecisionVersions,
    persist_scan_decisions,
    risk_settings_snapshot,
)
from src.swing.lessons_review import review_observations
from src.swing.market import FunnelRejection
from src.swing.measurement import (
    calculate_r_multiples,
    excursion_from_daily_bars,
    Fill,
    TradeMeasurementService,
)
from src.swing.notifications import NotificationService
from src.swing.postmortem import PostmortemEngine
from src.swing.scheduling import JOBS


def _add_trade(database, settings, candidate, realized_r: float, index: int, **overrides):
    entry = datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(days=index)
    values = {
        "trade_id": str(uuid4()),
        "decision_id": str(uuid4()),
        "candidate_id": candidate.candidate_id,
        "ticker": overrides.pop("ticker", f"T{index:03d}"),
        "setup_type": overrides.pop("setup_type", candidate.setup_type.value),
        "status": overrides.pop("status", "CLOSED"),
        "strategy_version": settings.strategy_version,
        "entry_datetime": entry.isoformat(),
        "exit_datetime": (entry + timedelta(days=4)).isoformat(),
        "entry_price": 100,
        "exit_price": 100 + realized_r * 4,
        "initial_stop": 96,
        "final_stop": 96,
        "target": 108,
        "shares": 2,
        "position_value": 200,
        "planned_dollar_risk": 8,
        "initial_risk_dollars": 8,
        "planned_account_risk_pct": 0.004,
        "planned_rr": 2,
        "realized_pnl": realized_r * 8,
        "realized_r": realized_r,
        "mfe_r": max(0.2, realized_r + 0.3),
        "mae_r": 0.4 if realized_r > 0 else 0.9,
        "holding_period_days": 4,
        "market_regime": overrides.pop("market_regime", candidate.market_regime.value),
        "sector": overrides.pop("sector", candidate.sector),
        "candidate_score": overrides.pop("candidate_score", candidate.score),
        "atr": overrides.pop("atr", 3),
        "risk_validation_result": json.dumps({"approved": True}),
    }
    values.update(overrides)
    database.add_trade(values)
    return values["trade_id"]


@pytest.mark.parametrize(
    ("exit_price", "expected"),
    [(108, 2.0), (96, -1.0), (100, 0.0)],
)
def test_fixed_r_winner_loser_and_breakeven(exit_price, expected):
    result = calculate_r_multiples(
        entry_fills=[Fill(2, 100)],
        exit_fills=[Fill(2, exit_price)],
        initial_stop=96,
        current_price=exit_price,
        current_stop=96,
        high_during_trade=max(100, exit_price),
        low_during_trade=min(100, exit_price),
    )
    assert result.realized_r == pytest.approx(expected)
    assert result.unrealized_r == 0


def test_partial_fills_and_stop_above_cost_keep_original_denominator():
    result = calculate_r_multiples(
        entry_fills=[Fill(2, 100), Fill(2, 102)],
        exit_fills=[Fill(1, 106)],
        initial_stop=96,
        current_price=103,
        current_stop=103,
        high_during_trade=108,
        low_during_trade=95,
    )
    assert result.initial_risk_dollars == 20
    assert result.realized_r == pytest.approx(0.25)
    assert result.unrealized_r == pytest.approx(0.30)
    assert result.mfe_r == pytest.approx(1.40)
    assert result.mae_r == pytest.approx(1.20)
    assert result.stop_locked_r == pytest.approx(0.30)


def test_mae_mfe_from_daily_bar_fixture():
    mfe, mae = excursion_from_daily_bars(
        entry_price=100,
        quantity=2,
        initial_risk_dollars_value=8,
        bars=[{"high": 104, "low": 98}, {"high": 110, "low": 95}],
    )
    assert mfe == pytest.approx(2.5)
    assert mae == pytest.approx(1.25)


def test_daily_bar_tracking_persists_unrealized_r_and_excursions(database, settings, candidate):
    database.record_candidate(candidate)
    trade_id = _add_trade(database, settings, candidate, 0, 1, status="OPEN", exit_datetime=None)
    snapshot = TradeMeasurementService(database).record_daily_bar(trade_id, high=110, low=95, close=104, source="fixture")
    stored = database.get_trade(trade_id)
    assert snapshot.unrealized_r == pytest.approx(1.0)
    assert stored["mfe_r"] == pytest.approx(2.5)
    assert stored["mae_r"] == pytest.approx(1.25)


def test_expectancy_and_all_required_segments_on_hand_computed_fixture(database, settings, candidate):
    database.record_candidate(candidate)
    for index in range(30):
        _add_trade(
            database,
            settings,
            candidate,
            2.0 if index < 15 else -1.0,
            index,
            setup_type="TREND_PULLBACK" if index % 2 == 0 else "BREAKOUT_RETEST",
            market_regime="BULL" if index % 3 else "NEUTRAL",
            sector="Technology" if index % 2 else "Industrials",
            candidate_score=82 if index % 2 else 91,
            holding_period_days=2 if index % 2 else 8,
            atr=1 if index % 2 else 5,
        )
    result = ExperienceEngine(database, settings).calculate()
    overall = result["overall"]
    assert overall["trade_count"] == 30
    assert overall["win_rate"] == pytest.approx(0.5)
    assert overall["average_winner_r"] == pytest.approx(2.0)
    assert overall["average_loser_r"] == pytest.approx(1.0)
    assert overall["expectancy_r"] == pytest.approx(0.5)
    assert set(result["breakdowns"]) == {
        "setup",
        "market_regime",
        "sector",
        "entry_score_bucket",
        "holding_period",
        "volatility_bucket",
    }


def test_small_sample_never_exposes_bare_statistics(database, settings, candidate):
    database.record_candidate(candidate)
    for index in range(3):
        _add_trade(database, settings, candidate, 2.0, index)
    result = ExperienceEngine(database, settings).calculate()["overall"]
    assert result["trade_count"] == 3
    for name in ("win_rate", "average_winner_r", "expectancy_r", "profit_factor", "average_hold_duration_days"):
        assert result[name]["status"] == INSUFFICIENT_SAMPLE
        assert "value" not in result[name]


def test_journal_round_trip_includes_measurement_and_version_fields(database, settings, candidate):
    database.record_candidate(candidate)
    trade_id = _add_trade(
        database,
        settings,
        candidate,
        1.5,
        1,
        initial_target=110,
        entry_slippage=0.05,
        exit_slippage=-0.10,
        earnings_distance_at_entry=12,
        technical_invalidation_reason="close below support",
        llm_verdict="APPROVE",
        risk_rejection_history_json=[{"reason_code": "REJECT_QTY_ZERO"}],
        config_version=settings.config_version,
        scanner_version=settings.scanner_version,
        model_name="mock-model",
        prompt_version="portfolio-manager-v2",
        market_data_timestamp="2026-01-01T20:00:00+00:00",
        feature_values_json={"atr_pct": 0.03},
        risk_settings_snapshot_json={"normal_risk_pct": 0.0075},
        risk_cluster="SEMICONDUCTORS",
        volatility_bucket="MEDIUM",
    )
    stored = database.get_trade(trade_id)
    assert stored["initial_risk_dollars"] == 8
    assert stored["initial_target"] == 110
    assert stored["earnings_distance_at_entry"] == 12
    assert json.loads(stored["risk_rejection_history_json"])[0]["reason_code"] == "REJECT_QTY_ZERO"
    assert stored["config_version"] == settings.config_version


def test_forward_migration_preserves_populated_v5_database(tmp_path):
    path = tmp_path / "populated-v5.db"
    connection = sqlite3.connect(path)
    connection.executescript(DDL)
    connection.execute("INSERT INTO schema_migrations(version,applied_at) VALUES(5,'2026-01-01')")
    connection.execute("INSERT INTO strategy_versions(strategy_version,status,introduced_at) VALUES('SWING_V1.0','PRODUCTION','2026-01-01')")
    connection.execute(
        """INSERT INTO market_regimes
           (regime_id,observed_at,regime,inputs_json,source,retrieved_at)
           VALUES('old-row','2026-01-01','BULL','{}','fixture','2026-01-01')"""
    )
    connection.commit()
    connection.close()
    migrated = SwingDatabase(path)
    migrated.initialize("SWING_V1.0")
    assert migrated.rows("SELECT regime_id FROM market_regimes") == [{"regime_id": "old-row"}]
    assert migrated.rows("SELECT MAX(version) AS version FROM schema_migrations")[0]["version"] == SCHEMA_VERSION
    columns = {row[1] for row in sqlite3.connect(path).execute("PRAGMA table_info(trades)")}
    assert {"initial_risk_dollars", "unrealized_r", "process_quality_score"} <= columns
    assert migrated.rows("SELECT COUNT(*) AS n FROM closed_trade_journal")[0]["n"] == 0


def test_identical_versioned_inputs_have_identical_persisted_decisions(database, settings):
    versions = DecisionVersions.from_settings(settings)
    kwargs = {
        "versions": versions,
        "market_data_timestamp": "2026-01-01T20:00:00+00:00",
        "feature_values": {"trend": 1.0, "volume": 1.2},
        "entry_score": 88,
        "risk_settings_snapshot": risk_settings_snapshot(settings),
        "decision": {"disposition": "strong"},
    }
    first = decision_envelope(**kwargs)
    second = decision_envelope(**kwargs)
    assert first == second
    for run_id, envelope in (("run-1", first), ("run-2", second)):
        database.record_decision(
            run_id=run_id,
            subject_type="CANDIDATE",
            subject_id=run_id,
            ticker="XYZ",
            reason_code="CANDIDATE_ACCEPTED",
            **versions.__dict__,
            market_data_timestamp=envelope["market_data_timestamp"],
            feature_values=envelope["feature_values"],
            entry_score=envelope["entry_score"],
            risk_settings_snapshot=envelope["risk_settings_snapshot"],
            decision=envelope["decision"],
            decision_fingerprint=envelope["decision_fingerprint"],
        )
    rows = database.rows("SELECT decision_fingerprint FROM decision_records ORDER BY run_id")
    assert len(rows) == 2 and rows[0] == rows[1]


def test_rejected_candidates_are_persisted_as_control_group(database, settings):
    report = SimpleNamespace(rejections=[FunnelRejection("XYZ", "trend_rs", "trend_rs_ineligible", ("roc=-0.05",))])
    persist_scan_decisions(database, settings, run_id="scan-1", candidates=[], funnel_report=report)
    row = database.why_not_trade("XYZ", datetime.now(timezone.utc).date().isoformat())[0]
    assert row["reason_code"] == "trend_rs_ineligible"
    assert "roc=-0.05" in row["feature_values_json"]


def test_postmortem_process_score_is_independent_of_outcome(database, settings, candidate):
    database.record_candidate(candidate)
    first = _add_trade(database, settings, candidate, 0, 1, status="OPEN", exit_datetime=None)
    second = _add_trade(database, settings, candidate, 0, 2, status="OPEN", exit_datetime=None)
    process = {
        "followed_strategy": True,
        "entry_valid": True,
        "entry_timing_good": True,
        "thesis_valid": True,
        "exit_good": True,
        "would_take_again": True,
        "result_driver": "PROCESS",
    }
    winner = PostmortemEngine(database, settings=settings).close_and_review(
        first,
        exit_price=108,
        high_during_trade=109,
        low_during_trade=98,
        reason_exit="target",
        assessment={**process, "classification": "VALID_WIN"},
    )
    loser = PostmortemEngine(database, settings=settings).close_and_review(
        second,
        exit_price=96,
        high_during_trade=102,
        low_during_trade=95,
        reason_exit="stop",
        assessment={**process, "classification": "VALID_LOSS"},
    )
    assert winner.outcome == "WIN" and loser.outcome == "LOSS"
    assert winner.process_quality_score == loser.process_quality_score


class _Backend:
    def __init__(self):
        self.calls = 0

    def complete(self, *, role, model_name, payload, schema):
        self.calls += 1
        if role == "technical":
            return schema(trend_structure="up", setup_valid=True, support_resistance="valid", volume_analysis="valid", invalidation="support")
        if role == "fundamental":
            return schema(earnings_trading_days=10, has_blocking_event_risk=False, material_events=[], fundamental_context="clear", data_gaps=[])
        if role == "bear":
            return schema(kill_trade=False, is_extended_from_support=False, is_chasing_price=False, regime_is_weak=False, sector_is_weakening=False, volume_is_unconvincing=False, support_is_weak=False, target_is_unrealistic=False, earnings_too_close=False, repeats_known_bad_pattern=False, fatal_reasons=[], weaknesses=[], thesis="valid", confidence=80)
        return schema(ticker="XYZ", decision="APPROVE", setup_type="TREND_PULLBACK", confidence_score=80, market_regime="BULL", bull_case="valid", bear_case="risk", invalidation="support", event_risk="clear", reasoning="approve")


def test_unchanged_candidate_suppresses_repeat_llm_calls(database, settings, candidate):
    database.record_candidate(candidate)
    backend = _Backend()
    configured = replace(settings, models=ModelSettings(analyst_model="mock", portfolio_manager_model="mock"))
    pipeline = AgentPipeline(configured, database, backend)
    assert len(pipeline.analyze([candidate])) == 1
    assert backend.calls == 4
    assert len(pipeline.analyze([candidate])) == 1
    assert backend.calls == 4
    assert pipeline.last_cycle_metrics["actual_calls"] == 0
    assert pipeline.last_cycle_metrics["suppressed_calls"] == 4


def test_lessons_aggregation_cannot_write_production_configuration(database, settings, tmp_path):
    config_file = Path(__file__).resolve().parents[2] / "config" / "do_not_trade.yaml"
    before = hashlib.sha256(config_file.read_bytes()).hexdigest()
    review_observations(database, settings, tmp_path / "reports")
    after = hashlib.sha256(config_file.read_bytes()).hexdigest()
    assert before == after
    assert database.rows("SELECT COUNT(*) AS n FROM hypotheses")[0]["n"] == 0


def test_secrets_are_redacted_from_logs_notifications_and_rows(database):
    token = "sk-ant-supersecrettoken123456789"
    database.record_risk_rejection(
        reason_code="REJECT_TEST",
        rule="test",
        reason=f"provider failure {token}",
        ticker="XYZ",
        decision_id="d1",
        intent_id=None,
        candidate={"api_key": token},
        computed_values={"auth_token": token},
    )
    database.record_notification(
        event_type="DAILY_SUMMARY",
        severity="INFO",
        title=f"summary {token}",
        reason_code="NO_TRADE",
        payload={"password": token},
    )
    text = json.dumps(
        {
            "rejections": database.rows("SELECT * FROM risk_rejections"),
            "logs": database.rows("SELECT * FROM structured_logs"),
            "notifications": database.rows("SELECT * FROM notifications"),
        }
    )
    assert token not in text
    assert "[REDACTED]" in text


def test_no_trade_daily_summary_is_calm_and_deduplicated(database):
    service = NotificationService(database)
    assert service.daily_summary(date="2026-01-02", candidates=0, opened=0, closed=0)
    assert not service.daily_summary(date="2026-01-02", candidates=0, opened=0, closed=0)
    row = database.rows("SELECT severity,reason_code FROM notifications")[0]
    assert row == {"severity": "INFO", "reason_code": "NO_TRADE"}


def test_each_scheduled_agent_has_one_allowlisted_subcommand():
    assert [job.default_et for job in JOBS[:6]] == ["09:35", "10:15", "12:30", "14:30", "15:45", "16:15"]
    for job in JOBS:
        assert job.command in job.agent_prompt
        assert "Every other shell command" in job.agent_prompt
        assert "forbidden" in job.agent_prompt
