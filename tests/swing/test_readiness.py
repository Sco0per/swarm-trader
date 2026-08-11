from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.backend.database.models import Base
from app.backend.models.schemas import BaseHedgeFundRequest, PortfolioPosition
from app.backend.repositories.flow_repository import FlowRepository
from app.backend.services.backtest_service import BacktestService
from app.backend.services.portfolio import create_portfolio
from src.swing import cli as cli_module
from src.swing.config import load_settings
from src.swing.models import BrokerAsset, OrderIntent
from src.swing.risk import SwingRiskManager


def test_supported_cli_help_is_one_swing_workflow(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["swing-trader", "--help"])
    with pytest.raises(SystemExit) as raised:
        cli_module.main()
    assert raised.value.code == 0
    help_text = capsys.readouterr().out
    for command in (
        "init-db",
        "status",
        "scan",
        "run",
        "positions",
        "reconcile",
        "analytics",
        "report",
        "paper-review",
        "tighten-stop",
        "close",
        "live-ticket",
        "record-live-fill",
        "record-live-exit",
    ):
        assert command in help_text
    assert "backtester" not in help_text


def test_positions_is_database_only(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("SWING_DATABASE_PATH", str(tmp_path / "positions.db"))
    monkeypatch.setattr("sys.argv", ["swing-trader", "positions"])
    assert cli_module.main() == 0
    assert json.loads(capsys.readouterr().out) == {"positions": [], "count": 0}


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("TRADING_STYLE", "day"),
        ("EXECUTION_MODE", "backtest"),
        ("BROKER_ASSET_FRESHNESS_SECONDS", "61"),
        ("MARKET_CLOCK_FRESHNESS_SECONDS", "61"),
        ("CORPORATE_EVENT_FRESHNESS_SECONDS", "86401"),
        ("EARNINGS_FRESHNESS_SECONDS", "86401"),
        ("MAXIMUM_HOLD_DAYS", "11"),
        ("TRAILING_DISTANCE_R", "0.76"),
        ("TRADING_ENABLED", "sometimes"),
        ("NORMAL_RISK_PCT", "not-a-number"),
    ],
)
def test_environment_cannot_weaken_named_safety_bound(monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError, match=name):
        load_settings()


def test_legacy_research_simulation_rejects_margin_shorts_and_negative_holdings():
    with pytest.raises(ValueError, match="Margin"):
        create_portfolio(2_000, 0.5, ["XYZ"])
    assert "margin_requirement" not in BaseHedgeFundRequest.model_fields

    negative = PortfolioPosition(ticker="XYZ", quantity=-1, trade_price=100.0)
    with pytest.raises(ValueError, match="Short positions"):
        create_portfolio(2_000, 0.0, ["XYZ"], [negative])

    service = object.__new__(BacktestService)
    service.portfolio = create_portfolio(2_000, 0.0, ["XYZ"])
    with pytest.raises(ValueError, match="Short selling"):
        service.execute_trade("XYZ", "short", 1, 100)


@pytest.mark.parametrize("unsupported", [{"api_keys": {"OPENAI_API_KEY": "secret"}}, {"margin_requirement": 0.5}])
def test_legacy_http_schema_rejects_secret_and_margin_fields(unsupported):
    with pytest.raises(ValidationError):
        BaseHedgeFundRequest(tickers=["XYZ"], graph_nodes=[], graph_edges=[], **unsupported)


def test_legacy_database_has_no_key_table_and_redacts_arbitrary_flow_data():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    assert "api_keys" not in Base.metadata.tables

    with sessionmaker(bind=engine)() as session:
        flow = FlowRepository(session).create_flow(
            "test",
            nodes=[{"id": "node", "api_key": "should-not-persist"}],
            edges=[],
            data={"password": "should-not-persist"},
        )
        assert flow.nodes[0]["api_key"] == "[REDACTED]"
        assert flow.data["password"] == "[REDACTED]"


@pytest.mark.parametrize("asset_class", ["option", "crypto"])
def test_non_equity_asset_classes_are_behaviorally_rejected(settings, database, proposal, candidate, quote, portfolio, asset_class):
    asset = BrokerAsset(symbol="XYZ", asset_class=asset_class, tradable=True, status="active")
    result = SwingRiskManager(settings, database).validate_entry(
        proposal,
        candidate,
        quote,
        portfolio,
        f"unsupported-{asset_class}",
        asset=asset,
    )
    assert not result.approved
    assert result.rule == "asset_not_tradable"


def test_fractional_order_quantity_is_schema_impossible():
    with pytest.raises(ValidationError):
        OrderIntent(
            intent_id="intent",
            decision_id="decision",
            ticker="XYZ",
            quantity=1.5,
            limit_price=100,
            stop_price=96,
            target_price=108,
            strategy_version="SWING_V1.0",
        )


def test_autoresearch_has_no_production_import_or_automatic_config_bridge():
    root = Path(__file__).resolve().parents[2]
    for path in (root / "src" / "swing").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "import autoresearch" not in text
        assert "from autoresearch" not in text
    quarantine = (root / "autoresearch" / "README.md").read_text(encoding="utf-8").lower()
    for phrase in ("intraday-derived", "not swing-validated", "human review"):
        assert phrase in quarantine


def test_sensitive_file_classes_are_gitignored():
    gitignore = (Path(__file__).resolve().parents[2] / ".gitignore").read_text(encoding="utf-8")
    for pattern in (".env.*", "!.env.example", "*.pem", "*.key", "credentials*.json", "secrets*.json", "*.log"):
        assert pattern in gitignore
