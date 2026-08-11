"""Versioned SQLite system of record for adaptive swing trading.

Backed by a local SQLite file by default. If TURSO_DATABASE_URL is set,
connects to a hosted Turso (libSQL-over-HTTP) database instead -- this is
what lets cloud routines share live state without git: they have no
persistent disk and (it turns out) no working git-push credentials either,
so committing the database file back to the repo every run doesn't work,
regardless of file size. See docs/CRON_AGENTS.md.

turso_serverless is a DB-API 2.0 driver deliberately built to be
sqlite3-compatible (?-style params, Row supports both index and column-name
access via __getitem__, IntegrityError raised on constraint violations,
executescript() commits any pending transaction first same as sqlite3) --
verified by installing the package and reading its source directly rather
than trusting incomplete docs. PRAGMA statements below are SQLite-file
specific (WAL mode, busy_timeout) and are skipped against Turso.
"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any, Iterator
from uuid import uuid4

from .models import (
    PositionLifecycleState,
    PostmortemRecord,
    SwingCandidate,
    TradeThesis,
)
from .security import redact_sensitive, redact_text


def _turso_serverless():
    import turso_serverless

    return turso_serverless


SCHEMA_VERSION = 7


DDL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS strategy_versions (
    strategy_version TEXT PRIMARY KEY,
    status TEXT NOT NULL CHECK(status IN ('PRODUCTION','CANDIDATE','REJECTED','RETIRED')),
    introduced_at TEXT NOT NULL,
    changes_json TEXT NOT NULL DEFAULT '{}',
    supporting_evidence_json TEXT NOT NULL DEFAULT '{}',
    backtest_id TEXT,
    out_of_sample_results_json TEXT,
    paper_results_json TEXT,
    approved_by TEXT,
    approved_at TEXT
);
CREATE TABLE IF NOT EXISTS market_regimes (
    regime_id TEXT PRIMARY KEY,
    observed_at TEXT NOT NULL,
    regime TEXT NOT NULL,
    spy_trend TEXT,
    qqq_trend TEXT,
    volatility REAL,
    breadth REAL,
    inputs_json TEXT NOT NULL,
    source TEXT NOT NULL,
    retrieved_at TEXT NOT NULL,
    market_timestamp TEXT
);
CREATE TABLE IF NOT EXISTS candidate_scores (
    candidate_id TEXT PRIMARY KEY,
    evaluated_at TEXT NOT NULL,
    ticker TEXT NOT NULL,
    setup_type TEXT NOT NULL,
    score REAL NOT NULL,
    score_components_json TEXT NOT NULL,
    disposition TEXT NOT NULL DEFAULT 'EVALUATED',
    rejection_reason TEXT,
    market_regime TEXT NOT NULL,
    sector TEXT,
    sector_etf TEXT,
    context_json TEXT NOT NULL,
    data_source TEXT NOT NULL,
    retrieved_at TEXT NOT NULL,
    market_timestamp TEXT
);
CREATE TABLE IF NOT EXISTS trades (
    trade_id TEXT PRIMARY KEY,
    decision_id TEXT NOT NULL UNIQUE,
    intent_id TEXT UNIQUE,
    candidate_id TEXT REFERENCES candidate_scores(candidate_id),
    ticker TEXT NOT NULL,
    setup_type TEXT NOT NULL,
    status TEXT NOT NULL,
    strategy_version TEXT NOT NULL REFERENCES strategy_versions(strategy_version),
    entry_datetime TEXT,
    exit_datetime TEXT,
    entry_price REAL,
    exit_price REAL,
    initial_stop REAL NOT NULL,
    final_stop REAL NOT NULL,
    target REAL NOT NULL,
    shares REAL NOT NULL,
    position_value REAL,
    planned_dollar_risk REAL NOT NULL,
    planned_account_risk_pct REAL NOT NULL,
    planned_rr REAL NOT NULL,
    realized_pnl REAL,
    realized_r REAL,
    mfe REAL,
    mae REAL,
    mfe_r REAL,
    mae_r REAL,
    holding_period_days REAL,
    market_regime TEXT NOT NULL,
    spy_trend TEXT,
    qqq_trend TEXT,
    sector TEXT,
    sector_trend TEXT,
    sector_relative_strength REAL,
    stock_relative_strength REAL,
    stock_relative_strength_sector REAL,
    rsi REAL,
    macd REAL,
    atr REAL,
    adx REAL,
    ma20 REAL,
    ma50 REAL,
    ma200 REAL,
    volume_ratio REAL,
    support REAL,
    resistance REAL,
    candidate_score REAL NOT NULL,
    bull_thesis TEXT,
    bear_thesis TEXT,
    pm_reasoning TEXT,
    reason_entry TEXT,
    reason_exit TEXT,
    broker_provider TEXT,
    broker_order_id TEXT,
    order_type TEXT,
    slippage REAL,
    risk_validation_result TEXT NOT NULL,
    rule_violation_status TEXT NOT NULL DEFAULT 'NONE',
    postmortem_classification TEXT,
    mistake_type TEXT,
    hypothesis_ids_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS trade_snapshots (
    snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id TEXT NOT NULL REFERENCES trades(trade_id),
    observed_at TEXT NOT NULL,
    price REAL NOT NULL,
    stop REAL NOT NULL,
    unrealized_pnl REAL,
    mfe REAL,
    mae REAL,
    source TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS trade_theses (
    trade_id TEXT PRIMARY KEY REFERENCES trades(trade_id),
    thesis_json TEXT NOT NULL,
    thesis_sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS trade_theses_no_update
BEFORE UPDATE ON trade_theses BEGIN
    SELECT RAISE(ABORT, 'trade thesis is immutable');
END;
CREATE TRIGGER IF NOT EXISTS trade_theses_no_delete
BEFORE DELETE ON trade_theses BEGIN
    SELECT RAISE(ABORT, 'trade thesis is immutable');
END;
CREATE TABLE IF NOT EXISTS position_lifecycle (
    trade_id TEXT PRIMARY KEY REFERENCES trades(trade_id),
    state TEXT NOT NULL CHECK(state IN ('OPEN','PROTECTED','PROFITABLE','TRAILING','EXIT_PENDING','CLOSED')),
    current_stop REAL NOT NULL,
    high_watermark REAL,
    context_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS lifecycle_events (
    lifecycle_event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id TEXT NOT NULL REFERENCES trades(trade_id),
    occurred_at TEXT NOT NULL,
    from_state TEXT,
    to_state TEXT NOT NULL,
    trigger TEXT NOT NULL,
    reason_code TEXT NOT NULL,
    context_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS reconciliation_mismatches (
    mismatch_id INTEGER PRIMARY KEY AUTOINCREMENT,
    detected_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('OPEN','ACKNOWLEDGED')),
    discrepancies_json TEXT NOT NULL,
    acknowledged_at TEXT,
    acknowledged_by TEXT,
    acknowledgement_reason TEXT
);
CREATE TABLE IF NOT EXISTS operational_alerts (
    alert_id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    severity TEXT NOT NULL,
    code TEXT NOT NULL,
    message TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS live_ticket_approvals (
    decision_id TEXT PRIMARY KEY,
    intent_id TEXT NOT NULL UNIQUE,
    candidate_json TEXT NOT NULL,
    proposal_json TEXT NOT NULL,
    risk_json TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    consumed_at TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS agent_decisions (
    agent_decision_id TEXT PRIMARY KEY,
    candidate_id TEXT REFERENCES candidate_scores(candidate_id),
    trade_id TEXT REFERENCES trades(trade_id),
    role TEXT NOT NULL,
    model_name TEXT,
    schema_version TEXT NOT NULL,
    decision TEXT,
    payload_json TEXT NOT NULL,
    validation_status TEXT NOT NULL,
    validation_error TEXT,
    prompt_tokens INTEGER,
    completion_tokens INTEGER,
    estimated_cost REAL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS postmortems (
    postmortem_id TEXT PRIMARY KEY,
    trade_id TEXT NOT NULL UNIQUE REFERENCES trades(trade_id),
    classification TEXT NOT NULL,
    followed_strategy INTEGER NOT NULL,
    answers_json TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    model_name TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS lessons (
    lesson_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    description TEXT NOT NULL,
    supporting_trades_json TEXT NOT NULL,
    sample_size INTEGER NOT NULL,
    confidence REAL NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('OBSERVATION','PROVISIONAL','VALIDATED','REJECTED','RETIRED')),
    applicable_setup TEXT,
    applicable_regime TEXT,
    evidence_json TEXT NOT NULL,
    strategy_version TEXT NOT NULL,
    approved_by TEXT,
    approved_at TEXT
);
CREATE TABLE IF NOT EXISTS hypotheses (
    hypothesis_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    observation TEXT NOT NULL,
    proposed_rule TEXT NOT NULL,
    setup_type TEXT,
    market_regime TEXT,
    supporting_lesson_ids_json TEXT NOT NULL,
    supporting_sample_size INTEGER NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('PROPOSED','VALIDATING','VALIDATED','REJECTED','APPROVED')),
    validation_plan_json TEXT NOT NULL,
    validation_results_json TEXT,
    candidate_strategy_version TEXT,
    human_approved INTEGER NOT NULL DEFAULT 0,
    approved_by TEXT,
    approved_at TEXT
);
CREATE TABLE IF NOT EXISTS backtests (
    backtest_id TEXT PRIMARY KEY,
    hypothesis_id TEXT REFERENCES hypotheses(hypothesis_id),
    strategy_version TEXT NOT NULL,
    created_at TEXT NOT NULL,
    data_hash TEXT NOT NULL,
    config_hash TEXT NOT NULL,
    train_period TEXT NOT NULL,
    validation_period TEXT NOT NULL,
    out_of_sample_period TEXT NOT NULL,
    metrics_json TEXT NOT NULL,
    benchmark_json TEXT NOT NULL,
    regime_results_json TEXT NOT NULL,
    walk_forward_json TEXT,
    artifact_path TEXT,
    accepted INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS experiments (
    experiment_id TEXT PRIMARY KEY,
    hypothesis_id TEXT REFERENCES hypotheses(hypothesis_id),
    started_at TEXT NOT NULL,
    completed_at TEXT,
    status TEXT NOT NULL,
    baseline_backtest_id TEXT REFERENCES backtests(backtest_id),
    candidate_backtest_id TEXT REFERENCES backtests(backtest_id),
    fitness REAL,
    fitness_components_json TEXT,
    notes TEXT
);
CREATE TABLE IF NOT EXISTS rule_violations (
    violation_id INTEGER PRIMARY KEY AUTOINCREMENT,
    occurred_at TEXT NOT NULL,
    trade_id TEXT REFERENCES trades(trade_id),
    decision_id TEXT,
    intent_id TEXT,
    ticker TEXT,
    rule TEXT NOT NULL,
    severity TEXT NOT NULL,
    blocked INTEGER NOT NULL,
    details TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS execution_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    occurred_at TEXT NOT NULL,
    intent_id TEXT NOT NULL,
    decision_id TEXT,
    trade_id TEXT REFERENCES trades(trade_id),
    broker_order_id TEXT,
    ticker TEXT NOT NULL,
    side TEXT NOT NULL,
    quantity REAL NOT NULL,
    event_type TEXT NOT NULL,
    status TEXT NOT NULL,
    payload_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS entry_admissions (
    intent_id TEXT PRIMARY KEY,
    decision_id TEXT NOT NULL UNIQUE,
    ticker TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('PENDING','SUBMITTED','UNKNOWN','RECONCILED','CANCELED','REJECTED')),
    planned_dollar_risk REAL NOT NULL,
    account_equity REAL NOT NULL,
    trade_id TEXT REFERENCES trades(trade_id),
    broker_order_id TEXT,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS equity_snapshots (
    snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
    observed_at TEXT NOT NULL,
    equity REAL NOT NULL,
    cash REAL NOT NULL,
    buying_power REAL NOT NULL,
    realized_pnl REAL,
    unrealized_pnl REAL,
    drawdown_pct REAL NOT NULL,
    execution_mode TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS benchmark_snapshots (
    snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
    observed_at TEXT NOT NULL,
    symbol TEXT NOT NULL,
    price REAL NOT NULL,
    source TEXT NOT NULL,
    UNIQUE(observed_at, symbol)
);
CREATE TABLE IF NOT EXISTS model_costs (
    cost_id INTEGER PRIMARY KEY AUTOINCREMENT,
    occurred_at TEXT NOT NULL,
    role TEXT NOT NULL,
    model_name TEXT NOT NULL,
    prompt_tokens INTEGER,
    completion_tokens INTEGER,
    cost REAL NOT NULL,
    candidate_id TEXT,
    trade_id TEXT
);
CREATE TABLE IF NOT EXISTS system_state (
    key TEXT PRIMARY KEY,
    value_json TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    updated_by TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS earnings_cache (
    symbol TEXT PRIMARY KEY,
    trading_days INTEGER,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS risk_snapshots (
    snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
    observed_at TEXT NOT NULL,
    account_equity REAL NOT NULL,
    total_open_risk REAL NOT NULL,
    open_risk_percent REAL NOT NULL,
    risk_by_sector_json TEXT NOT NULL,
    risk_by_cluster_json TEXT NOT NULL,
    risk_by_position_json TEXT NOT NULL,
    source TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS risk_rejections (
    rejection_id INTEGER PRIMARY KEY AUTOINCREMENT,
    occurred_at TEXT NOT NULL,
    reason_code TEXT NOT NULL,
    rule TEXT NOT NULL,
    ticker TEXT,
    decision_id TEXT,
    intent_id TEXT,
    candidate_json TEXT NOT NULL,
    computed_values_json TEXT NOT NULL,
    reason TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS halt_events (
    halt_event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    occurred_at TEXT NOT NULL,
    halt_key TEXT NOT NULL,
    active INTEGER NOT NULL,
    reason TEXT NOT NULL,
    source TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_execution_submitted_intent
    ON execution_events(intent_id) WHERE event_type = 'ORDER_SUBMITTED';
CREATE UNIQUE INDEX IF NOT EXISTS uq_execution_reserved_intent
    ON execution_events(intent_id) WHERE event_type = 'ORDER_RESERVED';
CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status);
CREATE INDEX IF NOT EXISTS idx_trades_entry ON trades(entry_datetime);
CREATE INDEX IF NOT EXISTS idx_trades_context ON trades(setup_type, market_regime, sector);
CREATE INDEX IF NOT EXISTS idx_candidates_ticker_time ON candidate_scores(ticker, evaluated_at);
CREATE INDEX IF NOT EXISTS idx_events_intent ON execution_events(intent_id, occurred_at);
CREATE INDEX IF NOT EXISTS idx_admissions_status ON entry_admissions(status, created_at);
CREATE INDEX IF NOT EXISTS idx_lifecycle_events_trade ON lifecycle_events(trade_id, occurred_at);
CREATE INDEX IF NOT EXISTS idx_reconciliation_mismatches_status ON reconciliation_mismatches(status, detected_at);
"""


JOURNAL_DDL = """
CREATE TABLE IF NOT EXISTS decision_records (
    decision_record_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    subject_type TEXT NOT NULL CHECK(subject_type IN ('CANDIDATE','REJECTION','TRADE','LLM')),
    subject_id TEXT,
    ticker TEXT,
    reason_code TEXT NOT NULL,
    strategy_version TEXT NOT NULL,
    config_version TEXT NOT NULL,
    scanner_version TEXT NOT NULL,
    model_name TEXT,
    prompt_version TEXT,
    market_data_timestamp TEXT,
    feature_values_json TEXT NOT NULL,
    entry_score REAL,
    risk_settings_snapshot_json TEXT NOT NULL,
    decision_json TEXT NOT NULL,
    decision_fingerprint TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(run_id, subject_type, decision_fingerprint)
);
CREATE INDEX IF NOT EXISTS idx_decision_why_not_trade
    ON decision_records(ticker, created_at, reason_code);
CREATE INDEX IF NOT EXISTS idx_decision_fingerprint
    ON decision_records(decision_fingerprint);
CREATE TABLE IF NOT EXISTS llm_review_state (
    candidate_key TEXT NOT NULL,
    role TEXT NOT NULL,
    model_name TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    last_score REAL NOT NULL,
    material_context_hash TEXT NOT NULL,
    response_json TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(candidate_key, role, model_name, prompt_version)
);
CREATE TABLE IF NOT EXISTS llm_cycles (
    cycle_id TEXT PRIMARY KEY,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    actual_calls INTEGER NOT NULL DEFAULT 0,
    suppressed_calls INTEGER NOT NULL DEFAULT 0,
    reason_counts_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS notifications (
    notification_id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    event_type TEXT NOT NULL,
    severity TEXT NOT NULL,
    title TEXT NOT NULL,
    reason_code TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    dedupe_key TEXT UNIQUE,
    delivery_status TEXT NOT NULL DEFAULT 'PENDING'
);
CREATE TABLE IF NOT EXISTS structured_logs (
    log_id INTEGER PRIMARY KEY AUTOINCREMENT,
    occurred_at TEXT NOT NULL,
    level TEXT NOT NULL,
    event TEXT NOT NULL,
    reason_code TEXT NOT NULL,
    ticker TEXT,
    decision_id TEXT,
    trade_id TEXT,
    context_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_logs_why_not_trade
    ON structured_logs(ticker, occurred_at, reason_code);
CREATE TABLE IF NOT EXISTS persisted_reports (
    report_id INTEGER PRIMARY KEY AUTOINCREMENT,
    report_type TEXT NOT NULL,
    generated_at TEXT NOT NULL,
    strategy_version TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    UNIQUE(report_type, generated_at)
);
CREATE TABLE IF NOT EXISTS trade_fills (
    fill_id TEXT PRIMARY KEY,
    trade_id TEXT NOT NULL REFERENCES trades(trade_id),
    side TEXT NOT NULL CHECK(side IN ('ENTRY','EXIT')),
    quantity REAL NOT NULL CHECK(quantity > 0),
    price REAL NOT NULL CHECK(price > 0),
    filled_at TEXT NOT NULL,
    broker_fill_id TEXT UNIQUE,
    payload_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_trade_fills_trade ON trade_fills(trade_id,side,filled_at);
CREATE VIEW IF NOT EXISTS closed_trade_journal AS
SELECT
    ticker,
    setup_type,
    entry_datetime AS entry_timestamp,
    exit_datetime AS exit_timestamp,
    holding_period_days AS days_held,
    entry_price,
    exit_price,
    initial_stop,
    initial_target,
    candidate_score AS entry_score,
    market_regime,
    sector,
    risk_cluster,
    initial_risk_dollars AS initial_R_dollars,
    realized_r AS realized_R,
    mae_r AS MAE_R,
    mfe_r AS MFE_R,
    entry_slippage,
    exit_slippage,
    reason_exit AS exit_reason,
    earnings_distance_at_entry,
    technical_invalidation_reason,
    llm_verdict AS LLM_verdict,
    risk_rejection_history_json AS risk_rejection_history
FROM trades WHERE status='CLOSED';
"""


MIGRATION_6_COLUMNS: dict[str, dict[str, str]] = {
    "candidate_scores": {
        "strategy_version": "TEXT",
        "config_version": "TEXT",
        "scanner_version": "TEXT",
        "feature_values_json": "TEXT NOT NULL DEFAULT '{}'",
        "decision_fingerprint": "TEXT",
    },
    "trades": {
        "initial_target": "REAL",
        "initial_risk_dollars": "REAL",
        "unrealized_r": "REAL",
        "entry_slippage": "REAL",
        "exit_slippage": "REAL",
        "earnings_distance_at_entry": "INTEGER",
        "technical_invalidation_reason": "TEXT",
        "llm_verdict": "TEXT",
        "risk_rejection_history_json": "TEXT NOT NULL DEFAULT '[]'",
        "config_version": "TEXT",
        "scanner_version": "TEXT",
        "model_name": "TEXT",
        "prompt_version": "TEXT",
        "market_data_timestamp": "TEXT",
        "feature_values_json": "TEXT NOT NULL DEFAULT '{}'",
        "risk_settings_snapshot_json": "TEXT NOT NULL DEFAULT '{}'",
        "risk_cluster": "TEXT",
        "volatility_bucket": "TEXT",
        "process_quality_score": "REAL",
        "outcome": "TEXT",
    },
    "trade_snapshots": {
        "high": "REAL",
        "low": "REAL",
        "unrealized_r": "REAL",
        "mfe_r": "REAL",
        "mae_r": "REAL",
    },
    "agent_decisions": {
        "strategy_version": "TEXT",
        "config_version": "TEXT",
        "scanner_version": "TEXT",
        "prompt_version": "TEXT",
        "input_json": "TEXT NOT NULL DEFAULT '{}'",
        "input_fingerprint": "TEXT",
        "cycle_id": "TEXT",
        "call_disposition": "TEXT NOT NULL DEFAULT 'CALLED'",
    },
    "postmortems": {
        "process_quality_score": "REAL",
        "outcome": "TEXT",
        "mechanical_answers_json": "TEXT NOT NULL DEFAULT '{}'",
        "narrative": "TEXT",
    },
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value: Any) -> str:
    return json.dumps(redact_sensitive(value), default=str, sort_keys=True)


def _safe(value: Any) -> str:
    return redact_text(value)


class SwingDatabase:
    """Small explicit repository layer; callers never depend on ORM state."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.turso_url = os.getenv("TURSO_DATABASE_URL") or None
        self.turso_auth_token = os.getenv("TURSO_AUTH_TOKEN") or None
        self.integrity_errors: tuple[type[Exception], ...] = (sqlite3.IntegrityError,)
        if self.turso_url:
            self.integrity_errors = (sqlite3.IntegrityError, _turso_serverless().IntegrityError)
        self.strategy_version = "SWING_V1.0"

    @contextmanager
    def connect(self) -> Iterator[Any]:
        if self.turso_url:
            connection = _turso_serverless().connect(self.turso_url, auth_token=self.turso_auth_token)
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(self.path)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA busy_timeout = 5000")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self, strategy_version: str = "SWING_V1.0") -> None:
        self.strategy_version = strategy_version
        with self.connect() as connection:
            connection.executescript(DDL)
            existing = connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
            if existing is None:
                # DDL above is the historical v5 baseline. Fresh databases start
                # there and then exercise the same forward migration as populated ones.
                connection.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES(5, ?)",
                    (_now(),),
                )
                existing = 5
            if int(existing) < 6:
                self._migrate_v6(connection)
                connection.execute(
                    "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES(6, ?)",
                    (_now(),),
                )
            connection.executescript(JOURNAL_DDL)
            connection.execute(
                "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES(?, ?)",
                (SCHEMA_VERSION, _now()),
            )
            connection.execute(
                """INSERT OR IGNORE INTO strategy_versions
                   (strategy_version, status, introduced_at, changes_json, supporting_evidence_json)
                   VALUES(?, 'PRODUCTION', ?, ?, ?)""",
                (strategy_version, _now(), _json({"baseline": "conservative swing-only rules"}), _json({"status": "paper validation required"})),
            )
            connection.execute(
                "INSERT OR IGNORE INTO system_state(key, value_json, updated_at, updated_by) VALUES('kill_switch', 'false', ?, 'system')",
                (_now(),),
            )
            connection.execute(
                "INSERT OR IGNORE INTO system_state(key, value_json, updated_at, updated_by) VALUES('drawdown_halt', 'false', ?, 'system')",
                (_now(),),
            )
            connection.execute(
                "INSERT OR IGNORE INTO system_state(key, value_json, updated_at, updated_by) VALUES('loss_streak_halt', 'false', ?, 'system')",
                (_now(),),
            )
            connection.execute(
                "INSERT OR IGNORE INTO system_state(key, value_json, updated_at, updated_by) VALUES('reconciliation_halt', 'false', ?, 'system')",
                (_now(),),
            )
            connection.execute(
                "INSERT OR IGNORE INTO system_state(key, value_json, updated_at, updated_by) VALUES('weekly_drawdown_halt', 'false', ?, 'system')",
                (_now(),),
            )

    @staticmethod
    def _table_columns(connection: Any, table: str) -> set[str]:
        cursor = connection.execute(f"SELECT * FROM {table} LIMIT 0")
        return {str(column[0]) for column in (cursor.description or ())}

    def _migrate_v6(self, connection: Any) -> None:
        """Forward-only journal/measurement migration; safe on populated v5 DBs."""

        for table, additions in MIGRATION_6_COLUMNS.items():
            present = self._table_columns(connection, table)
            for column, declaration in additions.items():
                if column not in present:
                    connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")
        connection.executescript(JOURNAL_DDL)

    def set_state(self, key: str, value: Any, updated_by: str) -> None:
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO system_state(key, value_json, updated_at, updated_by) VALUES(?, ?, ?, ?)
                   ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json,
                   updated_at=excluded.updated_at, updated_by=excluded.updated_by""",
                (key, _json(value), _now(), updated_by),
            )

    def activate_halt(self, key: str, reason: str, source: str) -> None:
        if key not in {"kill_switch", "drawdown_halt", "weekly_drawdown_halt", "loss_streak_halt", "reconciliation_halt"}:
            raise ValueError(f"Unsupported halt key: {key}")
        now = _now()
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO system_state(key,value_json,updated_at,updated_by) VALUES(?, 'true', ?, ?)
                   ON CONFLICT(key) DO UPDATE SET value_json='true',updated_at=excluded.updated_at,updated_by=excluded.updated_by""",
                (key, now, source),
            )
            connection.execute(
                "INSERT INTO halt_events(occurred_at,halt_key,active,reason,source) VALUES(?,?,1,?,?)",
                (now, key, _safe(reason), source),
            )
            if key == "kill_switch":
                connection.execute(
                    """INSERT OR IGNORE INTO notifications
                       (created_at,event_type,severity,title,reason_code,payload_json,dedupe_key)
                       VALUES(?,'KILL_SWITCH_ACTIVATED','CRITICAL','Kill switch activated','KILL_SWITCH_ACTIVATED',?,?)""",
                    (now, _json({"reason": reason, "source": source}), f"kill-switch:{now}"),
                )

    def get_state(self, key: str, default: Any = None) -> Any:
        with self.connect() as connection:
            row = connection.execute("SELECT value_json FROM system_state WHERE key=?", (key,)).fetchone()
        return json.loads(row["value_json"]) if row else default

    def get_earnings_cache_many(self, symbols: list[str], ttl_seconds: int) -> dict[str, int | None]:
        """Bulk-read fresh earnings-lookup cache entries, hosted so they survive across routine runs.

        One round trip for the whole batch rather than one per symbol -- this
        table exists specifically so a cloud routine's fresh-sandbox restart
        doesn't re-pay the same slow yfinance lookups every run. See
        docs/CRON_AGENTS.md and data_feed.fetch_earnings_trading_days_batch.
        """
        if not symbols:
            return {}
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=ttl_seconds)).isoformat()
        placeholders = ",".join("?" for _ in symbols)
        with self.connect() as connection:
            rows = connection.execute(
                f"SELECT symbol, trading_days FROM earnings_cache WHERE updated_at >= ? AND symbol IN ({placeholders})",
                (cutoff, *symbols),
            ).fetchall()
        return {row["symbol"]: (int(row["trading_days"]) if row["trading_days"] is not None else None) for row in rows}

    def set_earnings_cache_many(self, entries: list[tuple[str, int | None]]) -> None:
        if not entries:
            return
        now = _now()
        with self.connect() as connection:
            for symbol, trading_days in entries:
                connection.execute(
                    """INSERT INTO earnings_cache(symbol, trading_days, updated_at) VALUES(?,?,?)
                       ON CONFLICT(symbol) DO UPDATE SET trading_days=excluded.trading_days, updated_at=excluded.updated_at""",
                    (symbol, trading_days, now),
                )

    def record_candidate(
        self,
        candidate: SwingCandidate,
        disposition: str = "EVALUATED",
        rejection_reason: str | None = None,
        *,
        strategy_version: str | None = None,
        config_version: str | None = None,
        scanner_version: str | None = None,
    ) -> None:
        payload = candidate.model_dump(mode="json")
        decision_fingerprint = sha256(
            _json(
                {
                    "ticker": candidate.ticker,
                    "setup_type": candidate.setup_type.value,
                    "score": candidate.score,
                    "features": candidate.validator_features,
                    "strategy_version": strategy_version or self.strategy_version,
                    "config_version": config_version,
                    "scanner_version": scanner_version,
                    "disposition": disposition,
                    "rejection_reason": rejection_reason,
                }
            ).encode("utf-8")
        ).hexdigest()
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO candidate_scores
                   (candidate_id,evaluated_at,ticker,setup_type,score,score_components_json,disposition,
                    rejection_reason,market_regime,sector,sector_etf,context_json,data_source,retrieved_at,market_timestamp,
                    strategy_version,config_version,scanner_version,feature_values_json,decision_fingerprint)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(candidate_id) DO UPDATE SET
                     evaluated_at=excluded.evaluated_at, score=excluded.score,
                     score_components_json=excluded.score_components_json, disposition=excluded.disposition,
                     rejection_reason=excluded.rejection_reason, context_json=excluded.context_json,
                     data_source=excluded.data_source, retrieved_at=excluded.retrieved_at,
                     market_timestamp=excluded.market_timestamp,strategy_version=excluded.strategy_version,
                     config_version=excluded.config_version,scanner_version=excluded.scanner_version,
                     feature_values_json=excluded.feature_values_json,decision_fingerprint=excluded.decision_fingerprint""",
                (
                    candidate.candidate_id,
                    _now(),
                    candidate.ticker,
                    candidate.setup_type.value,
                    candidate.score,
                    _json(candidate.score_components),
                    disposition,
                    rejection_reason,
                    candidate.market_regime.value,
                    candidate.sector,
                    candidate.sector_etf,
                    _json(payload),
                    candidate.data.source,
                    candidate.data.retrieved_at.isoformat(),
                    candidate.data.market_timestamp.isoformat() if candidate.data.market_timestamp else None,
                    strategy_version or self.strategy_version,
                    config_version,
                    scanner_version,
                    _json({**candidate.validator_features, "score_components": candidate.score_components}),
                    decision_fingerprint,
                ),
            )

    def record_decision(
        self,
        *,
        run_id: str,
        subject_type: str,
        subject_id: str | None,
        ticker: str | None,
        reason_code: str,
        strategy_version: str,
        config_version: str,
        scanner_version: str,
        model_name: str | None,
        prompt_version: str | None,
        market_data_timestamp: str | None,
        feature_values: dict[str, Any],
        entry_score: float | None,
        risk_settings_snapshot: dict[str, Any],
        decision: dict[str, Any],
        decision_fingerprint: str,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """INSERT OR IGNORE INTO decision_records
                   (run_id,subject_type,subject_id,ticker,reason_code,strategy_version,config_version,
                    scanner_version,model_name,prompt_version,market_data_timestamp,feature_values_json,
                    entry_score,risk_settings_snapshot_json,decision_json,decision_fingerprint,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    run_id,
                    subject_type,
                    subject_id,
                    ticker,
                    reason_code,
                    strategy_version,
                    config_version,
                    scanner_version,
                    model_name,
                    prompt_version,
                    market_data_timestamp,
                    _json(feature_values),
                    entry_score,
                    _json(risk_settings_snapshot),
                    _json(decision),
                    decision_fingerprint,
                    _now(),
                ),
            )

    def why_not_trade(self, ticker: str, date_prefix: str) -> list[dict[str, Any]]:
        """Answer the operational question without re-running a scan."""

        return self.rows(
            """SELECT created_at,subject_type,reason_code,feature_values_json,decision_json
               FROM decision_records WHERE ticker=? AND created_at LIKE ?
               UNION ALL
               SELECT occurred_at AS created_at,'RISK' AS subject_type,reason_code,
                      context_json AS feature_values_json,'{}' AS decision_json
               FROM structured_logs WHERE ticker=? AND occurred_at LIKE ?
               ORDER BY created_at""",
            (ticker.upper(), f"{date_prefix}%", ticker.upper(), f"{date_prefix}%"),
        )

    def record_agent_decision(
        self,
        agent_decision_id: str,
        role: str,
        schema_version: str,
        payload: dict[str, Any],
        validation_status: str,
        *,
        candidate_id: str | None = None,
        trade_id: str | None = None,
        model_name: str | None = None,
        decision: str | None = None,
        validation_error: str | None = None,
        prompt_tokens: int | None = None,
        completion_tokens: int | None = None,
        estimated_cost: float | None = None,
        strategy_version: str | None = None,
        config_version: str | None = None,
        scanner_version: str | None = None,
        prompt_version: str | None = None,
        input_payload: dict[str, Any] | None = None,
        input_fingerprint: str | None = None,
        cycle_id: str | None = None,
        call_disposition: str = "CALLED",
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO agent_decisions
                   (agent_decision_id,candidate_id,trade_id,role,model_name,schema_version,decision,payload_json,
                    validation_status,validation_error,prompt_tokens,completion_tokens,estimated_cost,created_at,
                    strategy_version,config_version,scanner_version,prompt_version,input_json,input_fingerprint,
                    cycle_id,call_disposition)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    agent_decision_id,
                    candidate_id,
                    trade_id,
                    role,
                    model_name,
                    schema_version,
                    decision,
                    _json(payload),
                    validation_status,
                    _safe(validation_error) if validation_error else None,
                    prompt_tokens,
                    completion_tokens,
                    estimated_cost,
                    _now(),
                    strategy_version or self.strategy_version,
                    config_version,
                    scanner_version,
                    prompt_version,
                    _json(input_payload or {}),
                    input_fingerprint,
                    cycle_id,
                    call_disposition,
                ),
            )

    def get_llm_review_state(self, candidate_key: str, role: str, model_name: str, prompt_version: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                """SELECT * FROM llm_review_state
                   WHERE candidate_key=? AND role=? AND model_name=? AND prompt_version=?""",
                (candidate_key, role, model_name, prompt_version),
            ).fetchone()
        if not row:
            return None
        result = dict(row)
        result["response"] = json.loads(result.pop("response_json"))
        return result

    def save_llm_review_state(
        self,
        *,
        candidate_key: str,
        role: str,
        model_name: str,
        prompt_version: str,
        score: float,
        material_context_hash: str,
        response: dict[str, Any],
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO llm_review_state
                   (candidate_key,role,model_name,prompt_version,last_score,material_context_hash,response_json,updated_at)
                   VALUES(?,?,?,?,?,?,?,?)
                   ON CONFLICT(candidate_key,role,model_name,prompt_version) DO UPDATE SET
                     last_score=excluded.last_score,material_context_hash=excluded.material_context_hash,
                     response_json=excluded.response_json,updated_at=excluded.updated_at""",
                (
                    candidate_key,
                    role,
                    model_name,
                    prompt_version,
                    score,
                    material_context_hash,
                    _json(response),
                    _now(),
                ),
            )

    def start_llm_cycle(self, cycle_id: str) -> None:
        with self.connect() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO llm_cycles(cycle_id,started_at) VALUES(?,?)",
                (cycle_id, _now()),
            )

    def finish_llm_cycle(
        self,
        cycle_id: str,
        *,
        actual_calls: int,
        suppressed_calls: int,
        reason_counts: dict[str, int],
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """UPDATE llm_cycles SET completed_at=?,actual_calls=?,suppressed_calls=?,reason_counts_json=?
                   WHERE cycle_id=?""",
                (_now(), actual_calls, suppressed_calls, _json(reason_counts), cycle_id),
            )

    def record_violation(
        self,
        rule: str,
        details: str,
        *,
        ticker: str | None = None,
        decision_id: str | None = None,
        intent_id: str | None = None,
        trade_id: str | None = None,
        severity: str = "MAJOR",
        blocked: bool = True,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO rule_violations
                   (occurred_at,trade_id,decision_id,intent_id,ticker,rule,severity,blocked,details)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (_now(), trade_id, decision_id, intent_id, ticker, rule, severity, int(blocked), details),
            )

    def record_risk_rejection(
        self,
        *,
        reason_code: str,
        rule: str,
        reason: str,
        ticker: str | None,
        decision_id: str | None,
        intent_id: str | None,
        candidate: dict[str, Any] | None,
        computed_values: dict[str, Any] | None,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO risk_rejections
                   (occurred_at,reason_code,rule,ticker,decision_id,intent_id,candidate_json,computed_values_json,reason)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    _now(),
                    reason_code,
                    rule,
                    ticker,
                    decision_id,
                    intent_id,
                    _json(candidate or {}),
                    _json(computed_values or {}),
                    _safe(reason),
                ),
            )
            connection.execute(
                """INSERT INTO structured_logs
                   (occurred_at,level,event,reason_code,ticker,decision_id,context_json)
                   VALUES(?,'INFO','TRADE_REJECTED_BY_RISK',?,?,?,?)""",
                (_now(), reason_code, ticker, decision_id, _json({"rule": rule, "computed_values": computed_values or {}})),
            )
            connection.execute(
                """INSERT INTO notifications
                   (created_at,event_type,severity,title,reason_code,payload_json,dedupe_key)
                   VALUES(?,'TRADE_REJECTED_BY_RISK','INFO',?,?,?,NULL)""",
                (_now(), f"Risk rejected {ticker or 'candidate'}", reason_code, _json({"ticker": ticker, "rule": rule})),
            )

    def record_risk_snapshot(
        self,
        *,
        account_equity: float,
        total_open_risk: float,
        risk_by_sector: dict[str, float],
        risk_by_cluster: dict[str, float],
        risk_by_position: dict[str, float],
        source: str = "risk_engine",
    ) -> None:
        open_risk_percent = total_open_risk / account_equity if account_equity > 0 else 0.0
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO risk_snapshots
                   (observed_at,account_equity,total_open_risk,open_risk_percent,risk_by_sector_json,
                    risk_by_cluster_json,risk_by_position_json,source)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (
                    _now(),
                    account_equity,
                    total_open_risk,
                    open_risk_percent,
                    _json(risk_by_sector),
                    _json(risk_by_cluster),
                    _json(risk_by_position),
                    source,
                ),
            )

    def sizing_rejection_summary(self, since_iso: str) -> dict[str, Any]:
        rows = self.rows(
            """SELECT reason_code,ticker,computed_values_json FROM risk_rejections
               WHERE occurred_at >= ? AND reason_code IN ('REJECT_QTY_ZERO','REJECT_INSUFFICIENT_CASH')
               ORDER BY occurred_at""",
            (since_iso,),
        )
        grouped: dict[str, list[dict[str, Any]]] = {"REJECT_QTY_ZERO": [], "REJECT_INSUFFICIENT_CASH": []}
        for row in rows:
            values = json.loads(row["computed_values_json"])
            grouped[row["reason_code"]].append({"ticker": row["ticker"], "price": values.get("price"), "risk_per_share": values.get("risk_per_share")})
        return {
            "quantity_rounding_to_zero": {"count": len(grouped["REJECT_QTY_ZERO"]), "candidates": grouped["REJECT_QTY_ZERO"]},
            "insufficient_cash": {"count": len(grouped["REJECT_INSUFFICIENT_CASH"]), "candidates": grouped["REJECT_INSUFFICIENT_CASH"]},
        }

    def risk_rejection_history_for(self, ticker: str, limit: int = 20) -> list[dict[str, Any]]:
        rows = self.rows(
            """SELECT occurred_at,reason_code,rule,computed_values_json,reason
               FROM risk_rejections WHERE ticker=? ORDER BY occurred_at DESC LIMIT ?""",
            (ticker.upper(), limit),
        )
        for row in rows:
            row["computed_values"] = json.loads(row.pop("computed_values_json"))
        return rows

    def record_execution_event(
        self,
        *,
        intent_id: str,
        decision_id: str | None,
        trade_id: str | None,
        broker_order_id: str | None,
        ticker: str,
        side: str,
        quantity: float,
        event_type: str,
        status: str,
        payload: dict[str, Any],
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO execution_events
                   (occurred_at,intent_id,decision_id,trade_id,broker_order_id,ticker,side,quantity,event_type,status,payload_json)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (_now(), intent_id, decision_id, trade_id, broker_order_id, ticker, side, quantity, event_type, status, _json(payload)),
            )

    def intent_was_submitted(self, intent_id: str) -> bool:
        with self.connect() as connection:
            row = connection.execute("SELECT 1 FROM execution_events WHERE intent_id=? AND event_type IN ('ORDER_RESERVED','ORDER_SUBMITTED') LIMIT 1", (intent_id,)).fetchone()
        return row is not None

    def reserve_intent(
        self,
        *,
        intent_id: str,
        decision_id: str,
        ticker: str,
        side: str,
        quantity: float,
        payload: dict[str, Any],
    ) -> bool:
        """Atomically reserve an intent before the network call; retries fail closed."""
        try:
            self.record_execution_event(
                intent_id=intent_id,
                decision_id=decision_id,
                trade_id=None,
                broker_order_id=None,
                ticker=ticker,
                side=side,
                quantity=quantity,
                event_type="ORDER_RESERVED",
                status="PENDING",
                payload=payload,
            )
            return True
        except self.integrity_errors:
            return False

    def reserve_entry_admission(
        self,
        *,
        intent_id: str,
        decision_id: str,
        ticker: str,
        planned_dollar_risk: float,
        account_equity: float,
        payload: dict[str, Any],
        day_start_iso: str,
        week_start_iso: str,
        broker_position_symbols: set[str],
        max_positions: int,
        max_day: int,
        max_week: int,
        max_open_risk_pct: float,
        sector: str,
        cluster: str,
        cluster_by_ticker: dict[str, str],
        max_sector_risk_pct: float,
        max_cluster_risk_pct: float,
    ) -> tuple[bool, str | None, str]:
        """Serialize final admission under a SQLite write lock.

        Active unlinked reservations count toward activity, position, and open-risk
        limits so concurrent decisions cannot each pass on the same stale state.
        """
        now = _now()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute("SELECT 1 FROM entry_admissions WHERE intent_id=?", (intent_id,)).fetchone():
                return False, "duplicate_intent", "Intent already has an admission record"
            active = connection.execute(
                """SELECT ticker, planned_dollar_risk, created_at, payload_json FROM entry_admissions
                   WHERE trade_id IS NULL AND status IN ('PENDING','UNKNOWN')"""
            ).fetchall()
            today_trades = int(
                connection.execute(
                    "SELECT COUNT(*) FROM trades WHERE entry_datetime >= ? AND status NOT IN ('REJECTED','CANCELED')",
                    (day_start_iso,),
                ).fetchone()[0]
            )
            week_trades = int(
                connection.execute(
                    "SELECT COUNT(*) FROM trades WHERE entry_datetime >= ? AND status NOT IN ('REJECTED','CANCELED')",
                    (week_start_iso,),
                ).fetchone()[0]
            )
            pending_today = sum(1 for row in active if row["created_at"] >= day_start_iso)
            pending_week = sum(1 for row in active if row["created_at"] >= week_start_iso)
            if today_trades + pending_today >= max_day:
                return False, "daily_entry_limit", "Serialized daily entry limit reached"
            if week_trades + pending_week >= max_week:
                return False, "weekly_entry_limit", "Serialized weekly entry limit reached"
            open_rows = connection.execute("SELECT ticker, planned_dollar_risk FROM trades WHERE status IN ('SUBMITTED','OPEN','PARTIALLY_FILLED')").fetchall()
            estimated_symbols = set(broker_position_symbols)
            estimated_symbols.update(str(row["ticker"]).upper() for row in open_rows)
            estimated_symbols.update(str(row["ticker"]).upper() for row in active)
            if ticker.upper() not in estimated_symbols and len(estimated_symbols) >= max_positions:
                return False, "max_positions", "Serialized maximum-position limit reached"
            existing_open_risk = sum(float(row["planned_dollar_risk"] or 0) for row in open_rows)
            reserved_risk = sum(float(row["planned_dollar_risk"] or 0) for row in active)
            if existing_open_risk + reserved_risk + planned_dollar_risk > account_equity * max_open_risk_pct + 1e-9:
                return False, "max_combined_open_risk", "Serialized combined open-risk limit reached"
            open_context = connection.execute("SELECT ticker,sector,planned_dollar_risk FROM trades WHERE status IN ('SUBMITTED','OPEN','PARTIALLY_FILLED')").fetchall()
            existing_sector_risk = sum(float(row["planned_dollar_risk"] or 0) for row in open_context if str(row["sector"] or "UNKNOWN") == sector)
            existing_cluster_risk = 0.0
            for row in open_context:
                if cluster_by_ticker.get(str(row["ticker"]).upper()) == cluster:
                    existing_cluster_risk += float(row["planned_dollar_risk"] or 0)
            for row in active:
                try:
                    active_payload = json.loads(row["payload_json"]) if "payload_json" in row.keys() else {}
                except (TypeError, ValueError):
                    return False, "broker_mismatch", "Malformed active-admission risk context"
                active_candidate = active_payload.get("candidate", {})
                active_risk = float(row["planned_dollar_risk"] or 0)
                if str(active_candidate.get("sector") or "UNKNOWN") == sector:
                    existing_sector_risk += active_risk
                if active_payload.get("cluster") == cluster:
                    existing_cluster_risk += active_risk
            if existing_sector_risk + planned_dollar_risk > account_equity * max_sector_risk_pct + 1e-9:
                return False, "sector_risk", "Serialized sector open-risk limit reached"
            if existing_cluster_risk + planned_dollar_risk > account_equity * max_cluster_risk_pct + 1e-9:
                return False, "cluster_risk", "Serialized cluster open-risk limit reached"
            connection.execute(
                """INSERT INTO entry_admissions
                   (intent_id,decision_id,ticker,status,planned_dollar_risk,account_equity,payload_json,created_at,updated_at)
                   VALUES(?,?,?,'PENDING',?,?,?,?,?)""",
                (intent_id, decision_id, ticker.upper(), planned_dollar_risk, account_equity, _json(payload), now, now),
            )
            connection.execute(
                """INSERT INTO execution_events
                   (occurred_at,intent_id,decision_id,trade_id,broker_order_id,ticker,side,quantity,event_type,status,payload_json)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (now, intent_id, decision_id, None, None, ticker.upper(), "buy", payload.get("intent", {}).get("quantity", 0), "ORDER_RESERVED", "PENDING", _json(payload)),
            )
        return True, None, "Entry admission reserved"

    def update_admission(
        self,
        intent_id: str,
        status: str,
        *,
        trade_id: str | None = None,
        broker_order_id: str | None = None,
    ) -> None:
        if status not in {"PENDING", "SUBMITTED", "UNKNOWN", "RECONCILED", "CANCELED", "REJECTED"}:
            raise ValueError("Invalid admission status")
        with self.connect() as connection:
            connection.execute(
                """UPDATE entry_admissions SET status=?, trade_id=COALESCE(?,trade_id),
                   broker_order_id=COALESCE(?,broker_order_id), updated_at=? WHERE intent_id=?""",
                (status, trade_id, broker_order_id, _now(), intent_id),
            )

    def active_admissions(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute("SELECT * FROM entry_admissions WHERE status IN ('PENDING','SUBMITTED','UNKNOWN') ORDER BY created_at").fetchall()
        return [dict(row) for row in rows]

    def add_trade(self, values: dict[str, Any]) -> None:
        required = {
            "trade_id",
            "decision_id",
            "ticker",
            "setup_type",
            "status",
            "strategy_version",
            "initial_stop",
            "final_stop",
            "target",
            "shares",
            "planned_dollar_risk",
            "planned_account_risk_pct",
            "planned_rr",
            "market_regime",
            "candidate_score",
            "risk_validation_result",
        }
        missing = sorted(required - values.keys())
        if missing:
            raise ValueError(f"Missing trade fields: {', '.join(missing)}")
        record = dict(values)
        record.setdefault("initial_target", record.get("target"))
        record.setdefault("initial_risk_dollars", record.get("planned_dollar_risk"))
        record.setdefault("risk_rejection_history_json", "[]")
        record.setdefault("feature_values_json", "{}")
        record.setdefault("risk_settings_snapshot_json", "{}")
        record.setdefault("created_at", _now())
        record.setdefault("updated_at", record["created_at"])
        columns = list(record)
        placeholders = ",".join("?" for _ in columns)
        with self.connect() as connection:
            connection.execute(
                f"INSERT INTO trades ({','.join(columns)}) VALUES ({placeholders})",
                tuple(_json(v) if isinstance(v, (dict, list)) else v for v in record.values()),
            )
            if record.get("status") in {"SUBMITTED", "OPEN", "PARTIALLY_FILLED"}:
                connection.execute(
                    """INSERT OR IGNORE INTO notifications
                       (created_at,event_type,severity,title,reason_code,payload_json,dedupe_key)
                       VALUES(?,'PAPER_TRADE_OPENED','INFO',?,'TRADE_OPENED',?,?)""",
                    (
                        _now(),
                        f"Paper trade opened: {record['ticker']}",
                        _json({"trade_id": record["trade_id"], "ticker": record["ticker"]}),
                        f"trade-open:{record['trade_id']}",
                    ),
                )

    def add_trade_with_thesis(self, values: dict[str, Any], thesis: TradeThesis) -> None:
        """Atomically persist the mutable journal row and immutable original thesis."""
        if values.get("trade_id") is None or values.get("trade_id") == "":
            raise ValueError("trade_id is required")
        required = {
            "trade_id",
            "decision_id",
            "ticker",
            "setup_type",
            "status",
            "strategy_version",
            "initial_stop",
            "final_stop",
            "target",
            "shares",
            "planned_dollar_risk",
            "planned_account_risk_pct",
            "planned_rr",
            "market_regime",
            "candidate_score",
            "risk_validation_result",
        }
        missing = sorted(required - values.keys())
        if missing:
            raise ValueError(f"Missing trade fields: {', '.join(missing)}")
        if str(values["ticker"]).upper() != thesis.ticker or str(values["setup_type"]) != thesis.setup.value:
            raise ValueError("Trade row and immutable thesis identity do not match")
        record = dict(values)
        record.setdefault("initial_target", record.get("target"))
        record.setdefault("initial_risk_dollars", record.get("planned_dollar_risk"))
        record.setdefault("risk_rejection_history_json", "[]")
        record.setdefault("feature_values_json", "{}")
        record.setdefault("risk_settings_snapshot_json", "{}")
        record.setdefault("created_at", _now())
        record.setdefault("updated_at", record["created_at"])
        columns = list(record)
        thesis_json = thesis.model_dump_json()
        with self.connect() as connection:
            connection.execute(
                f"INSERT INTO trades ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
                tuple(_json(v) if isinstance(v, (dict, list)) else v for v in record.values()),
            )
            connection.execute(
                "INSERT INTO trade_theses(trade_id,thesis_json,thesis_sha256,created_at) VALUES(?,?,?,?)",
                (record["trade_id"], thesis_json, sha256(thesis_json.encode("utf-8")).hexdigest(), _now()),
            )
            if record.get("status") in {"SUBMITTED", "OPEN", "PARTIALLY_FILLED"}:
                connection.execute(
                    """INSERT OR IGNORE INTO notifications
                       (created_at,event_type,severity,title,reason_code,payload_json,dedupe_key)
                       VALUES(?,'PAPER_TRADE_OPENED','INFO',?,'TRADE_OPENED',?,?)""",
                    (
                        _now(),
                        f"Paper trade opened: {record['ticker']}",
                        _json({"trade_id": record["trade_id"], "ticker": record["ticker"]}),
                        f"trade-open:{record['trade_id']}",
                    ),
                )

    def get_trade_thesis(self, trade_id: str) -> TradeThesis | None:
        with self.connect() as connection:
            row = connection.execute("SELECT thesis_json FROM trade_theses WHERE trade_id=?", (trade_id,)).fetchone()
        return TradeThesis.model_validate_json(row["thesis_json"]) if row else None

    def overwrite_trade_thesis(self, trade_id: str, thesis: TradeThesis) -> None:
        """Explicitly exposed so callers/tests receive the storage-layer immutable failure."""
        thesis_json = thesis.model_dump_json()
        with self.connect() as connection:
            connection.execute(
                "UPDATE trade_theses SET thesis_json=?,thesis_sha256=? WHERE trade_id=?",
                (thesis_json, sha256(thesis_json.encode("utf-8")).hexdigest(), trade_id),
            )

    def update_trade(self, trade_id: str, **values: Any) -> None:
        if not values:
            return
        immutable = {
            "decision_id",
            "intent_id",
            "candidate_id",
            "ticker",
            "setup_type",
            "strategy_version",
            "initial_stop",
            "target",
            "initial_target",
            "initial_risk_dollars",
            "planned_dollar_risk",
            "planned_account_risk_pct",
            "planned_rr",
            "market_regime",
            "candidate_score",
            "bull_thesis",
            "bear_thesis",
            "pm_reasoning",
            "reason_entry",
            "config_version",
            "scanner_version",
            "model_name",
            "prompt_version",
            "market_data_timestamp",
            "feature_values_json",
            "risk_settings_snapshot_json",
            "risk_cluster",
            "volatility_bucket",
            "earnings_distance_at_entry",
            "technical_invalidation_reason",
            "llm_verdict",
            "risk_rejection_history_json",
        }
        attempted = sorted(immutable.intersection(values))
        if attempted:
            raise ValueError(f"Immutable original trade fields cannot be updated: {', '.join(attempted)}")
        values["updated_at"] = _now()
        assignments = ",".join(f"{key}=?" for key in values)
        params = [(_json(value) if isinstance(value, (dict, list)) else value) for value in values.values()]
        with self.connect() as connection:
            connection.execute(f"UPDATE trades SET {assignments} WHERE trade_id=?", (*params, trade_id))

    def create_position_lifecycle(
        self,
        trade_id: str,
        state: PositionLifecycleState,
        *,
        current_stop: float,
        trigger: str,
    ) -> None:
        now = _now()
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO position_lifecycle(trade_id,state,current_stop,context_json,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                (trade_id, state.value, current_stop, "{}", now, now),
            )
            connection.execute(
                "INSERT INTO lifecycle_events(trade_id,occurred_at,from_state,to_state,trigger,reason_code,context_json) VALUES(?,?,?,?,?,?,?)",
                (trade_id, now, None, state.value, trigger, "POSITION_OPENED", "{}"),
            )

    def get_position_lifecycle(self, trade_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM position_lifecycle WHERE trade_id=?", (trade_id,)).fetchone()
        return dict(row) if row else None

    def transition_position_lifecycle(
        self,
        trade_id: str,
        *,
        expected_state: PositionLifecycleState,
        new_state: PositionLifecycleState,
        trigger: str,
        reason_code: str,
        context: dict[str, Any],
    ) -> bool:
        now = _now()
        with self.connect() as connection:
            cursor = connection.execute(
                "UPDATE position_lifecycle SET state=?,context_json=?,updated_at=? WHERE trade_id=? AND state=?",
                (new_state.value, _json(context), now, trade_id, expected_state.value),
            )
            if cursor.rowcount != 1:
                return False
            connection.execute(
                "INSERT INTO lifecycle_events(trade_id,occurred_at,from_state,to_state,trigger,reason_code,context_json) VALUES(?,?,?,?,?,?,?)",
                (trade_id, now, expected_state.value, new_state.value, trigger, reason_code, _json(context)),
            )
        return True

    def update_position_runtime(self, trade_id: str, *, current_stop: float | None = None, high_watermark: float | None = None, context: dict[str, Any] | None = None) -> None:
        updates: dict[str, Any] = {"updated_at": _now()}
        if current_stop is not None:
            updates["current_stop"] = current_stop
        if high_watermark is not None:
            updates["high_watermark"] = high_watermark
        if context is not None:
            updates["context_json"] = _json(context)
        assignments = ",".join(f"{key}=?" for key in updates)
        with self.connect() as connection:
            connection.execute(f"UPDATE position_lifecycle SET {assignments} WHERE trade_id=?", (*updates.values(), trade_id))

    def record_reconciliation_mismatch(self, discrepancies: list[str]) -> None:
        now = _now()
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO reconciliation_mismatches(detected_at,status,discrepancies_json) VALUES(?,'OPEN',?)",
                (now, _json(discrepancies)),
            )
            connection.execute(
                "INSERT INTO operational_alerts(created_at,severity,code,message,payload_json) VALUES(?,'CRITICAL','RECONCILIATION_MISMATCH',?,?)",
                (now, _safe("; ".join(discrepancies)), _json({"discrepancies": discrepancies})),
            )
            connection.execute(
                """INSERT INTO notifications
                   (created_at,event_type,severity,title,reason_code,payload_json,dedupe_key)
                   VALUES(?,'BROKER_DATABASE_MISMATCH','CRITICAL','Broker/database mismatch','RECONCILIATION_MISMATCH',?,?)""",
                (now, _json({"discrepancies": discrepancies}), f"reconciliation:{now}"),
            )

    def acknowledge_reconciliation_halt(self, *, acknowledged_by: str, reason: str) -> None:
        if not acknowledged_by.strip() or not reason.strip():
            raise ValueError("Reconciliation acknowledgement requires actor and reason")
        last = self.get_state("last_reconciliation", {})
        if not isinstance(last, dict) or not last.get("reconciled"):
            raise ValueError("A clean broker reconciliation is required before acknowledgement")
        now = _now()
        with self.connect() as connection:
            connection.execute(
                "UPDATE reconciliation_mismatches SET status='ACKNOWLEDGED',acknowledged_at=?,acknowledged_by=?,acknowledgement_reason=? WHERE status='OPEN'",
                (now, acknowledged_by, reason),
            )
        self.set_state("reconciliation_halt", False, f"human:{acknowledged_by}")

    def record_live_ticket_approval(
        self,
        *,
        decision_id: str,
        intent_id: str,
        candidate: dict[str, Any],
        proposal: dict[str, Any],
        risk: dict[str, Any],
        expires_at: str,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO live_ticket_approvals(decision_id,intent_id,candidate_json,proposal_json,risk_json,expires_at,created_at) VALUES(?,?,?,?,?,?,?)",
                (decision_id, intent_id, _json(candidate), _json(proposal), _json(risk), expires_at, _now()),
            )

    def consume_live_ticket_approval(self, decision_id: str) -> dict[str, Any]:
        now = _now()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM live_ticket_approvals WHERE decision_id=? AND consumed_at IS NULL",
                (decision_id,),
            ).fetchone()
            if row is None:
                raise ValueError("No unused deterministic live-ticket approval exists for this decision")
            if str(row["expires_at"]) < now:
                raise ValueError("Live-ticket approval expired; re-run every deterministic gate")
            connection.execute(
                "UPDATE live_ticket_approvals SET consumed_at=? WHERE decision_id=? AND consumed_at IS NULL",
                (now, decision_id),
            )
        result = dict(row)
        for field in ("candidate_json", "proposal_json", "risk_json"):
            result[field] = json.loads(result[field])
        return result

    def record_entry_fill(
        self,
        trade_id: str,
        *,
        quantity: float,
        price: float,
        filled_at: str,
        broker_fill_id: str | None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        """Add one entry fill and recompute actual fixed-R basis from fill lots."""

        if quantity <= 0 or price <= 0:
            raise ValueError("Entry fill quantity and price must be positive")
        with self.connect() as connection:
            trade = connection.execute("SELECT status,initial_stop FROM trades WHERE trade_id=?", (trade_id,)).fetchone()
            if not trade:
                raise KeyError(trade_id)
            if trade["status"] == "CLOSED":
                raise ValueError("Cannot add an entry fill to a closed trade")
            exit_count = connection.execute("SELECT COUNT(*) FROM trade_fills WHERE trade_id=? AND side='EXIT'", (trade_id,)).fetchone()[0]
            if exit_count:
                raise ValueError("Cannot add an entry fill after an exit fill")
            connection.execute(
                """INSERT OR IGNORE INTO trade_fills
                   (fill_id,trade_id,side,quantity,price,filled_at,broker_fill_id,payload_json)
                   VALUES(?,?,'ENTRY',?,?,?,?,?)""",
                (
                    str(uuid4()),
                    trade_id,
                    quantity,
                    price,
                    filled_at,
                    broker_fill_id,
                    _json(payload or {}),
                ),
            )
            totals = connection.execute(
                """SELECT SUM(quantity) AS quantity,SUM(quantity*price) AS cost
                   FROM trade_fills WHERE trade_id=? AND side='ENTRY'""",
                (trade_id,),
            ).fetchone()
            total_quantity = float(totals["quantity"] or 0)
            total_cost = float(totals["cost"] or 0)
            average_entry = total_cost / total_quantity
            actual_initial_risk = total_cost - total_quantity * float(trade["initial_stop"])
            if actual_initial_risk <= 0:
                raise ValueError("Entry fills do not produce positive initial risk")
            connection.execute(
                """UPDATE trades SET shares=?,entry_price=?,position_value=?,initial_risk_dollars=?,updated_at=?
                   WHERE trade_id=?""",
                (
                    total_quantity,
                    average_entry,
                    total_cost,
                    actual_initial_risk,
                    _now(),
                    trade_id,
                ),
            )

    def sync_cumulative_entry_fill(
        self,
        trade_id: str,
        *,
        cumulative_quantity: float,
        cumulative_average_price: float,
        filled_at: str,
        broker_fill_id: str,
    ) -> None:
        """Convert a broker cumulative fill update into one incremental lot."""

        rows = self.rows(
            """SELECT COALESCE(SUM(quantity),0) AS quantity,COALESCE(SUM(quantity*price),0) AS cost
               FROM trade_fills WHERE trade_id=? AND side='ENTRY'""",
            (trade_id,),
        )
        prior_quantity = float(rows[0]["quantity"])
        prior_cost = float(rows[0]["cost"])
        delta_quantity = cumulative_quantity - prior_quantity
        if delta_quantity <= 1e-9:
            return
        delta_cost = cumulative_quantity * cumulative_average_price - prior_cost
        delta_price = delta_cost / delta_quantity
        self.record_entry_fill(
            trade_id,
            quantity=delta_quantity,
            price=delta_price,
            filled_at=filled_at,
            broker_fill_id=f"{broker_fill_id}:{cumulative_quantity:g}",
            payload={"source": "broker_cumulative_fill"},
        )

    def record_trade_snapshot(
        self,
        trade_id: str,
        *,
        price: float,
        stop: float,
        source: str,
        unrealized_pnl: float | None = None,
        mfe: float | None = None,
        mae: float | None = None,
        high: float | None = None,
        low: float | None = None,
        unrealized_r: float | None = None,
        mfe_r: float | None = None,
        mae_r: float | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO trade_snapshots
                   (trade_id,observed_at,price,stop,unrealized_pnl,mfe,mae,source,payload_json,
                    high,low,unrealized_r,mfe_r,mae_r)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    trade_id,
                    _now(),
                    price,
                    stop,
                    unrealized_pnl,
                    mfe,
                    mae,
                    source,
                    _json(payload or {}),
                    high,
                    low,
                    unrealized_r,
                    mfe_r,
                    mae_r,
                ),
            )

    def trade_price_extremes(self, trade_id: str, fallback: float) -> tuple[float, float]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT MAX(COALESCE(high,price)) AS high, MIN(COALESCE(low,price)) AS low FROM trade_snapshots WHERE trade_id=?",
                (trade_id,),
            ).fetchone()
        return (
            float(row["high"]) if row and row["high"] is not None else fallback,
            float(row["low"]) if row and row["low"] is not None else fallback,
        )

    def get_trade(self, trade_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM trades WHERE trade_id=?", (trade_id,)).fetchone()
        return dict(row) if row else None

    def open_trades(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute("SELECT * FROM trades WHERE status IN ('SUBMITTED','OPEN','PARTIALLY_FILLED')").fetchall()
        return [dict(row) for row in rows]

    def closed_trades(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute("SELECT * FROM trades WHERE status='CLOSED' ORDER BY exit_datetime").fetchall()
        return [dict(row) for row in rows]

    def new_position_count(self, start_iso: str) -> int:
        with self.connect() as connection:
            row = connection.execute("SELECT COUNT(*) AS n FROM trades WHERE entry_datetime >= ? AND status NOT IN ('REJECTED','CANCELED')", (start_iso,)).fetchone()
        return int(row["n"])

    def consecutive_losses(self) -> int:
        with self.connect() as connection:
            rows = connection.execute("SELECT realized_pnl FROM trades WHERE status='CLOSED' AND realized_pnl IS NOT NULL ORDER BY exit_datetime DESC LIMIT 100").fetchall()
        count = 0
        for row in rows:
            if float(row["realized_pnl"]) < 0:
                count += 1
            else:
                break
        return count

    def last_losing_trade(self, ticker: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                """SELECT * FROM trades WHERE ticker=? AND status='CLOSED' AND realized_pnl < 0
                   ORDER BY exit_datetime DESC LIMIT 1""",
                (ticker.upper(),),
            ).fetchone()
        return dict(row) if row else None

    def record_equity_snapshot(self, **values: Any) -> None:
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO equity_snapshots
                   (observed_at,equity,cash,buying_power,realized_pnl,unrealized_pnl,drawdown_pct,execution_mode)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (
                    values.get("observed_at", _now()),
                    values["equity"],
                    values["cash"],
                    values["buying_power"],
                    values.get("realized_pnl"),
                    values.get("unrealized_pnl"),
                    values.get("drawdown_pct", 0),
                    values.get("execution_mode", "paper"),
                ),
            )

    def equity_high(self, fallback: float) -> float:
        with self.connect() as connection:
            row = connection.execute("SELECT MAX(equity) AS high FROM equity_snapshots").fetchone()
        return float(row["high"]) if row and row["high"] is not None else fallback

    def equity_high_since(self, start_iso: str, fallback: float) -> float:
        with self.connect() as connection:
            row = connection.execute("SELECT MAX(equity) AS high FROM equity_snapshots WHERE observed_at >= ?", (start_iso,)).fetchone()
        return float(row["high"]) if row and row["high"] is not None else fallback

    def record_postmortem(self, postmortem_id: str, record: PostmortemRecord, model_name: str | None = None) -> None:
        answers = record.model_dump(mode="json", exclude={"evidence"})
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO postmortems
                   (postmortem_id,trade_id,classification,followed_strategy,answers_json,evidence_json,model_name,created_at,
                    process_quality_score,outcome,mechanical_answers_json,narrative)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    postmortem_id,
                    record.trade_id,
                    record.classification.value,
                    int(record.followed_strategy),
                    _json(answers),
                    _json(record.evidence),
                    model_name,
                    _now(),
                    record.process_quality_score,
                    record.outcome,
                    _json(record.mechanical_answers),
                    _safe(record.narrative) if record.narrative else None,
                ),
            )
            connection.execute(
                """UPDATE trades SET postmortem_classification=?, mistake_type=?, process_quality_score=?,
                   outcome=?, updated_at=? WHERE trade_id=?""",
                (
                    record.classification.value,
                    record.mistake_type,
                    record.process_quality_score,
                    record.outcome,
                    _now(),
                    record.trade_id,
                ),
            )

    def record_notification(
        self,
        *,
        event_type: str,
        severity: str,
        title: str,
        reason_code: str,
        payload: dict[str, Any],
        dedupe_key: str | None = None,
    ) -> bool:
        try:
            with self.connect() as connection:
                connection.execute(
                    """INSERT INTO notifications
                       (created_at,event_type,severity,title,reason_code,payload_json,dedupe_key)
                       VALUES(?,?,?,?,?,?,?)""",
                    (
                        _now(),
                        event_type,
                        severity,
                        _safe(title),
                        reason_code,
                        _json(payload),
                        dedupe_key,
                    ),
                )
            return True
        except self.integrity_errors:
            return False

    def mark_notification_delivery(self, notification_id: int, status: str) -> None:
        if status not in {"SENT", "FAILED"}:
            raise ValueError("status must be SENT or FAILED")
        with self.connect() as connection:
            connection.execute(
                "UPDATE notifications SET delivery_status=? WHERE notification_id=?",
                (status, notification_id),
            )

    def record_log(
        self,
        *,
        level: str,
        event: str,
        reason_code: str,
        context: dict[str, Any],
        ticker: str | None = None,
        decision_id: str | None = None,
        trade_id: str | None = None,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO structured_logs
                   (occurred_at,level,event,reason_code,ticker,decision_id,trade_id,context_json)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (
                    _now(),
                    level,
                    event,
                    reason_code,
                    ticker,
                    decision_id,
                    trade_id,
                    _json(context),
                ),
            )

    def persist_report(self, report_type: str, generated_at: str, strategy_version: str, payload: dict[str, Any]) -> None:
        with self.connect() as connection:
            connection.execute(
                """INSERT OR REPLACE INTO persisted_reports
                   (report_type,generated_at,strategy_version,payload_json) VALUES(?,?,?,?)""",
                (report_type, generated_at, strategy_version, _json(payload)),
            )

    def record_model_cost(
        self,
        *,
        role: str,
        model_name: str,
        prompt_tokens: int | None,
        completion_tokens: int | None,
        cost: float,
        candidate_id: str | None = None,
        trade_id: str | None = None,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO model_costs
                   (occurred_at,role,model_name,prompt_tokens,completion_tokens,cost,candidate_id,trade_id)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (_now(), role, model_name, prompt_tokens, completion_tokens, cost, candidate_id, trade_id),
            )

    def validated_lessons_for(self, setup_type: str | None, market_regime: str | None) -> list[dict[str, Any]]:
        """Read-only lookup of VALIDATED lessons applicable to a setup/regime.

        A lesson with no ``applicable_setup``/``applicable_regime`` filter is
        treated as applying broadly. Only VALIDATED lessons are ever returned;
        OBSERVATION/PROVISIONAL rows must never influence production decisions.
        """
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT * FROM lessons WHERE status='VALIDATED'
                   AND (applicable_setup IS NULL OR applicable_setup=?)
                   AND (applicable_regime IS NULL OR applicable_regime=?)
                   ORDER BY created_at DESC""",
                (setup_type, market_regime),
            ).fetchall()
        return [dict(row) for row in rows]

    def create_observation(
        self,
        lesson_id: str,
        description: str,
        trade_id: str,
        confidence: float,
        strategy_version: str,
        applicable_setup: str | None,
        applicable_regime: str | None,
        evidence: dict[str, Any],
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO lessons
                   (lesson_id,created_at,description,supporting_trades_json,sample_size,confidence,status,
                    applicable_setup,applicable_regime,evidence_json,strategy_version)
                   VALUES(?,?,?,?,1,?,'OBSERVATION',?,?,?,?)""",
                (
                    lesson_id,
                    _now(),
                    description,
                    _json([trade_id]),
                    confidence,
                    applicable_setup,
                    applicable_regime,
                    _json(evidence),
                    strategy_version,
                ),
            )

    def transition_lesson(self, lesson_id: str, new_status: str, *, approved_by: str | None = None) -> None:
        allowed = {
            "OBSERVATION": {"PROVISIONAL", "REJECTED", "RETIRED"},
            "PROVISIONAL": {"VALIDATED", "REJECTED", "RETIRED"},
            "VALIDATED": {"RETIRED"},
            "REJECTED": set(),
            "RETIRED": set(),
        }
        with self.connect() as connection:
            row = connection.execute("SELECT status,sample_size FROM lessons WHERE lesson_id=?", (lesson_id,)).fetchone()
            if not row:
                raise KeyError(lesson_id)
            if new_status not in allowed[row["status"]]:
                raise ValueError(f"Invalid lesson transition {row['status']} -> {new_status}")
            if new_status == "VALIDATED" and (not approved_by or int(row["sample_size"]) < 2):
                raise ValueError("Validation requires multi-trade evidence and explicit human approval")
            connection.execute(
                "UPDATE lessons SET status=?, approved_by=?, approved_at=? WHERE lesson_id=?",
                (new_status, approved_by, _now() if approved_by else None, lesson_id),
            )

    def add_hypothesis(self, values: dict[str, Any]) -> None:
        required = {
            "hypothesis_id",
            "observation",
            "proposed_rule",
            "supporting_lesson_ids",
            "supporting_sample_size",
            "validation_plan",
        }
        missing = required - values.keys()
        if missing:
            raise ValueError(f"Missing hypothesis fields: {sorted(missing)}")
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO hypotheses
                   (hypothesis_id,created_at,observation,proposed_rule,setup_type,market_regime,
                    supporting_lesson_ids_json,supporting_sample_size,status,validation_plan_json,
                    validation_results_json,candidate_strategy_version,human_approved,approved_by,approved_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    values["hypothesis_id"],
                    _now(),
                    values["observation"],
                    values["proposed_rule"],
                    values.get("setup_type"),
                    values.get("market_regime"),
                    _json(values["supporting_lesson_ids"]),
                    values["supporting_sample_size"],
                    values.get("status", "PROPOSED"),
                    _json(values["validation_plan"]),
                    _json(values["validation_results"]) if values.get("validation_results") is not None else None,
                    values.get("candidate_strategy_version"),
                    int(values.get("human_approved", False)),
                    values.get("approved_by"),
                    values.get("approved_at"),
                ),
            )

    def get_hypothesis(self, hypothesis_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM hypotheses WHERE hypothesis_id=?", (hypothesis_id,)).fetchone()
        return dict(row) if row else None

    def update_hypothesis_validation(self, hypothesis_id: str, *, status: str, results: dict[str, Any]) -> None:
        if status not in {"VALIDATING", "VALIDATED", "REJECTED"}:
            raise ValueError("Invalid validation status")
        with self.connect() as connection:
            connection.execute(
                "UPDATE hypotheses SET status=?, validation_results_json=? WHERE hypothesis_id=?",
                (status, _json(results), hypothesis_id),
            )

    def add_backtest(self, values: dict[str, Any]) -> None:
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO backtests
                   (backtest_id,hypothesis_id,strategy_version,created_at,data_hash,config_hash,train_period,
                    validation_period,out_of_sample_period,metrics_json,benchmark_json,regime_results_json,
                    walk_forward_json,artifact_path,accepted)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    values["backtest_id"],
                    values.get("hypothesis_id"),
                    values["strategy_version"],
                    _now(),
                    values["data_hash"],
                    values["config_hash"],
                    values["train_period"],
                    values["validation_period"],
                    values["out_of_sample_period"],
                    _json(values["metrics"]),
                    _json(values["benchmark"]),
                    _json(values["regime_results"]),
                    _json(values.get("walk_forward")) if values.get("walk_forward") else None,
                    values.get("artifact_path"),
                    int(values.get("accepted", False)),
                ),
            )

    def approve_candidate_strategy(self, hypothesis_id: str, approved_by: str) -> str:
        """Human-only promotion; requires an accepted OOS backtest and adequate sample."""
        if not approved_by.strip():
            raise ValueError("approved_by is required")
        with self.connect() as connection:
            hypothesis = connection.execute("SELECT * FROM hypotheses WHERE hypothesis_id=?", (hypothesis_id,)).fetchone()
            if not hypothesis:
                raise KeyError(hypothesis_id)
            if int(hypothesis["supporting_sample_size"]) < 30:
                raise ValueError("At least 30 supporting observations are required for production approval")
            backtest = connection.execute("SELECT * FROM backtests WHERE hypothesis_id=? AND accepted=1 ORDER BY created_at DESC LIMIT 1", (hypothesis_id,)).fetchone()
            if not backtest:
                raise ValueError("No accepted out-of-sample backtest supports this hypothesis")
            version = hypothesis["candidate_strategy_version"]
            if not version:
                raise ValueError("Hypothesis has no candidate strategy version")
            connection.execute(
                """INSERT OR IGNORE INTO strategy_versions
                   (strategy_version,status,introduced_at,changes_json,supporting_evidence_json,backtest_id,approved_by,approved_at)
                   VALUES(?, 'CANDIDATE', ?, ?, ?, ?, ?, ?)""",
                (version, _now(), _json({"hypothesis_id": hypothesis_id}), _json({"backtest_id": backtest["backtest_id"]}), backtest["backtest_id"], approved_by, _now()),
            )
            connection.execute("UPDATE strategy_versions SET status='RETIRED' WHERE status='PRODUCTION'")
            connection.execute(
                "UPDATE strategy_versions SET status='PRODUCTION', approved_by=?, approved_at=? WHERE strategy_version=?",
                (approved_by, _now(), version),
            )
            connection.execute(
                "UPDATE hypotheses SET status='APPROVED', human_approved=1, approved_by=?, approved_at=? WHERE hypothesis_id=?",
                (approved_by, _now(), hypothesis_id),
            )
        return str(version)

    def rows(self, query: str, params: tuple = ()) -> list[dict[str, Any]]:
        """Read-only helper used by reporting modules with fixed internal SQL."""
        if not query.lstrip().upper().startswith("SELECT"):
            raise ValueError("rows() accepts SELECT statements only")
        with self.connect() as connection:
            result = connection.execute(query, params).fetchall()
        return [dict(row) for row in result]

    def table_counts(self) -> dict[str, int]:
        tables = [
            "trades",
            "trade_snapshots",
            "trade_fills",
            "market_regimes",
            "candidate_scores",
            "agent_decisions",
            "postmortems",
            "lessons",
            "hypotheses",
            "strategy_versions",
            "backtests",
            "experiments",
            "rule_violations",
            "execution_events",
            "entry_admissions",
            "risk_snapshots",
            "risk_rejections",
            "halt_events",
            "decision_records",
            "llm_cycles",
            "notifications",
            "structured_logs",
            "persisted_reports",
        ]
        with self.connect() as connection:
            return {table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]) for table in tables}
