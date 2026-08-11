from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from src.swing import cli as cli_module


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
