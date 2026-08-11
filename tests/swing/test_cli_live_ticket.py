from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from src.swing import cli as cli_module
from src.swing.database import SwingDatabase


def _snapshot():
    now = datetime.now(timezone.utc).isoformat()
    return {
        "account": {
            "account_id": "robinhood-agentic-1",
            "equity": 2000.0,
            "cash": 2000.0,
            "buying_power": 2000.0,
            "is_paper": False,
            "is_margin_enabled": False,
            "dedicated_agentic_account": True,
            "retrieved_at": now,
        },
        "positions": [],
        "open_orders": [],
        "asset": {"symbol": "XYZ", "asset_class": "us_equity", "tradable": True, "status": "active"},
        "market": {"is_open": True, "retrieved_at": now},
        "quote": {"symbol": "XYZ", "last": 100.0, "bid": 99.95, "ask": 100.05, "retrieved_at": now, "market_timestamp": now},
        "trade_history": [],
    }


def _write_payload(tmp_path, candidate, proposal):
    payload = {
        "candidate": candidate.model_dump(mode="json"),
        "proposal": proposal.model_dump(mode="json"),
        "broker_snapshot": _snapshot(),
    }
    path = tmp_path / "live_input.json"
    path.write_text(json.dumps(payload, default=str), encoding="utf-8")
    return path


def test_live_ticket_blocked_outside_live_mode(monkeypatch, tmp_path, candidate, proposal):
    monkeypatch.setenv("SWING_DATABASE_PATH", str(tmp_path / "swing.db"))
    monkeypatch.delenv("EXECUTION_MODE", raising=False)
    path = _write_payload(tmp_path, candidate, proposal)
    monkeypatch.setattr("sys.argv", ["swing-trader", "live-ticket", str(path)])
    with pytest.raises(ValueError, match="EXECUTION_MODE=live"):
        cli_module.main()


def test_live_ticket_produces_approved_ticket_in_live_mode(monkeypatch, tmp_path, capsys, candidate, proposal):
    monkeypatch.setenv("SWING_DATABASE_PATH", str(tmp_path / "swing.db"))
    monkeypatch.setenv("EXECUTION_MODE", "live")
    monkeypatch.setenv("TRADING_ENABLED", "true")
    monkeypatch.setenv("LIVE_TRADING_ACK", "I_ACKNOWLEDGE_LIVE_RISK")
    path = _write_payload(tmp_path, candidate, proposal)
    monkeypatch.setattr("sys.argv", ["swing-trader", "live-ticket", str(path)])
    cli_module.main()
    out = capsys.readouterr().out
    assert "DRY_RUN_APPROVED" in out
    assert "APPROVED LIVE ORDER TICKET" in out
    assert '"quantity": 3' in out


def test_record_live_fill_requires_and_consumes_deterministic_ticket_with_protection(monkeypatch, tmp_path, capsys, candidate, proposal):
    database_path = tmp_path / "swing.db"
    monkeypatch.setenv("SWING_DATABASE_PATH", str(database_path))
    monkeypatch.setenv("EXECUTION_MODE", "live")
    monkeypatch.setenv("TRADING_ENABLED", "true")
    monkeypatch.setenv("LIVE_TRADING_ACK", "I_ACKNOWLEDGE_LIVE_RISK")
    ticket_path = _write_payload(tmp_path, candidate, proposal)
    monkeypatch.setattr("sys.argv", ["swing-trader", "live-ticket", str(ticket_path)])
    cli_module.main()
    capsys.readouterr()

    fill_path = tmp_path / "live_fill.json"
    fill_path.write_text(
        json.dumps(
            {
                "proposal": {"decision_id": proposal.decision_id},
                "fill": {
                    "entry_datetime": datetime.now(timezone.utc).isoformat(),
                    "entry_price": 100,
                    "shares": 3,
                    "broker_order_id": "human-entry",
                    "protective_status": "active",
                    "protective_stop_order_id": "human-stop",
                    "target_order_id": "human-target",
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("sys.argv", ["swing-trader", "record-live-fill", str(fill_path)])
    cli_module.main()
    assert '"status": "recorded"' in capsys.readouterr().out

    database = SwingDatabase(database_path)
    trades = database.open_trades()
    assert len(trades) == 1
    thesis = database.get_trade_thesis(trades[0]["trade_id"])
    assert thesis is not None
    assert thesis.ticker == "XYZ"
    persisted_lifecycle = database.get_position_lifecycle(trades[0]["trade_id"])
    assert persisted_lifecycle is not None
    assert persisted_lifecycle["state"] == "PROTECTED"
    with pytest.raises(ValueError, match="No unused deterministic live-ticket approval"):
        cli_module.main()
