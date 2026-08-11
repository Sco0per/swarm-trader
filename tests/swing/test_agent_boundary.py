from __future__ import annotations

import ast
from dataclasses import replace
from pathlib import Path

import pytest

from src.swing.agents import AgentPipeline
from src.swing.config import ModelSettings
from src.swing.models import Decision, SetupType


class BoundaryBackend:
    def __init__(self, pm_override=None, fail_role=None):
        self.pm_override = pm_override or {}
        self.fail_role = fail_role
        self.payloads = []

    def complete(self, *, role, model_name, payload, schema):
        self.payloads.append((role, payload))
        if role == self.fail_role:
            raise TimeoutError("mock model timeout")
        if role == "technical":
            return schema(
                trend_structure="uptrend",
                setup_valid=True,
                support_resistance="support intact",
                volume_analysis="normal",
                invalidation="close below support",
            )
        if role == "fundamental":
            return schema(
                earnings_trading_days=10,
                has_blocking_event_risk=False,
                material_events=[],
                fundamental_context="no known blocker",
                data_gaps=[],
            )
        if role == "bear":
            return schema(
                kill_trade=False,
                is_extended_from_support=False,
                is_chasing_price=False,
                regime_is_weak=False,
                sector_is_weakening=False,
                volume_is_unconvincing=False,
                support_is_weak=False,
                target_is_unrealistic=False,
                earnings_too_close=False,
                repeats_known_bad_pattern=False,
                fatal_reasons=[],
                weaknesses=[],
                thesis="no fatal contradiction",
                confidence=70,
            )
        if role == "portfolio_manager":
            values = {
                "ticker": payload["candidate"]["ticker"],
                "decision": "APPROVE",
                "setup_type": payload["candidate"]["setup_type"],
                "confidence_score": 80,
                "market_regime": payload["candidate"]["market_regime"],
                "bull_case": "trend",
                "bear_case": "known risks",
                "invalidation": "close below support",
                "event_risk": "none known",
                "reasoning": "advisory approval",
                **self.pm_override,
            }
            return schema(**values)
        raise AssertionError(role)


def _pipeline(settings, database, backend):
    configured = replace(
        settings,
        models=ModelSettings(analyst_model="mock-analyst", portfolio_manager_model="mock-reviewer"),
    )
    return AgentPipeline(configured, database, backend)


@pytest.mark.parametrize("malicious_field", ["quantity", "stop", "risk_limit", "position_size", "override_earnings_gate"])
def test_malicious_llm_risk_or_sizing_override_is_schema_rejected(settings, database, candidate, malicious_field):
    database.record_candidate(candidate)
    backend = BoundaryBackend(pm_override={malicious_field: 999})
    assert _pipeline(settings, database, backend).analyze([candidate]) == []


@pytest.mark.parametrize(
    "override",
    [
        {"ticker": "UNKNOWN"},
        {"setup_type": "INVALID_SETUP"},
        {"setup_type": "BREAKOUT_RETEST"},
    ],
)
def test_unknown_ticker_or_invalid_mismatched_setup_is_no_trade(settings, database, candidate, override):
    database.record_candidate(candidate)
    assert _pipeline(settings, database, BoundaryBackend(pm_override=override)).analyze([candidate]) == []


@pytest.mark.parametrize("role", ["technical", "fundamental", "bear", "portfolio_manager"])
def test_timeout_at_any_required_role_is_no_trade(settings, database, candidate, role):
    database.record_candidate(candidate)
    assert _pipeline(settings, database, BoundaryBackend(fail_role=role)).analyze([candidate]) == []


def test_unconfigured_model_role_is_no_trade(settings, database, candidate):
    database.record_candidate(candidate)
    assert AgentPipeline(settings, database, BoundaryBackend()).analyze([candidate]) == []


@pytest.mark.parametrize("decision", ["REJECT", "WATCH"])
def test_non_approve_model_verdict_is_no_trade(settings, database, candidate, decision):
    database.record_candidate(candidate)
    assert _pipeline(settings, database, BoundaryBackend(pm_override={"decision": decision})).analyze([candidate]) == []


def test_deterministically_failed_candidate_never_reaches_any_model(settings, database, candidate):
    backend = BoundaryBackend()
    failed = candidate.model_copy(update={"validator_failures": ["hard_setup_failure"]})
    assert _pipeline(settings, database, backend).analyze([failed]) == []
    assert backend.payloads == []


def test_model_prompts_are_minimal_and_never_include_secrets_or_account_identifiers(settings, database, candidate):
    database.record_candidate(candidate)
    backend = BoundaryBackend()
    proposals = _pipeline(settings, database, backend).analyze([candidate])
    assert len(proposals) == 1
    serialized = repr(backend.payloads).lower()
    for forbidden in ("api_key", "api_secret", "credential", "account_id", "buying_power", "client_order_id"):
        assert forbidden not in serialized
    assert [role for role, _ in backend.payloads] == ["technical", "fundamental", "bear", "portfolio_manager"]


def test_legacy_investor_personality_modules_are_not_imported_by_production_swing_path():
    swing_root = Path(__file__).resolve().parents[2] / "src" / "swing"
    forbidden_import_prefixes = ("src.agents", "app", "execute_trades", "run_hedge_fund")
    for path in swing_root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                assert all(not alias.name.startswith(forbidden_import_prefixes) for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                assert not node.module.startswith(forbidden_import_prefixes)


def test_legacy_broker_mutation_modules_are_removed_and_only_paper_provider_has_order_endpoint():
    root = Path(__file__).resolve().parents[2]
    for relative in ("execute_trades.py", "rebalance.py", "run_hedge_fund.py", "run_analysis.py", "src/alpaca_integration.py"):
        assert not (root / relative).exists()
    endpoint_files = []
    for path in root.rglob("*.py"):
        if any(part in {".git", ".venv", "tests"} for part in path.parts):
            continue
        text = path.read_text(encoding="utf-8")
        if "/orders" in text and any(token in text for token in ('"POST"', '"PATCH"', '"DELETE"', "requests.post", "requests.patch", "requests.delete")):
            endpoint_files.append(path.relative_to(root).as_posix())
    assert endpoint_files == ["src/swing/brokers/alpaca.py"]


def test_production_decision_and_setup_schemas_have_no_day_short_or_derivative_modes():
    assert {item.value for item in SetupType} == {
        "TREND_PULLBACK",
        "BREAKOUT_RETEST",
        "RELATIVE_STRENGTH_CONTINUATION",
    }
    assert {item.value for item in Decision} == {"BUY", "WATCH", "HOLD", "EXIT", "NO_TRADE"}
    for prohibited in ("DAY_TRADE", "SHORT", "OPTION", "CRYPTO", "LEVERAGED_ETF", "MARGIN"):
        with pytest.raises(ValueError):
            Decision(prohibited)
