from __future__ import annotations

import json

from src.swing import cli as cli_module
from src.swing.notifications import NotificationService, NotificationType


def _emit_run_failed(database, error: str) -> None:
    NotificationService(database).emit(
        NotificationType.RUN_FAILED,
        severity="CRITICAL",
        title="Swing CLI run failed",
        reason_code="RUN_EXCEPTION",
        payload={"error": error},
    )


def _check(monkeypatch, database_path, capsys) -> dict:
    monkeypatch.setenv("SWING_DATABASE_PATH", str(database_path))
    monkeypatch.setattr("sys.argv", ["swing-trader", "watchdog-check"])
    cli_module.main()
    return json.loads(capsys.readouterr().out)


def test_watchdog_check_is_empty_with_no_run_failures(monkeypatch, tmp_path, capsys):
    result = _check(monkeypatch, tmp_path / "swing.db", capsys)
    assert result == {"since_id": 0, "count": 0, "failures": []}


def test_watchdog_check_lists_run_failed_notifications_only(monkeypatch, tmp_path, capsys, database):
    _emit_run_failed(database, "boom: tuple indices must be integers or slices, not str")
    NotificationService(database).emit(
        NotificationType.KILL_SWITCH_ACTIVATED,
        severity="CRITICAL",
        title="Kill switch activated",
        reason_code="KILL_SWITCH_ACTIVATED",
        payload={},
    )
    result = _check(monkeypatch, database.path, capsys)
    assert result["count"] == 1
    assert result["failures"][0]["event_type"] == "RUN_FAILED"
    assert "tuple indices" in result["failures"][0]["payload_json"]


def test_watchdog_resolve_advances_watermark_and_emits_result(monkeypatch, tmp_path, capsys, database):
    _emit_run_failed(database, "boom")
    incident_id = database.unresolved_run_failures()[0]["notification_id"]

    monkeypatch.setenv("SWING_DATABASE_PATH", str(database.path))
    monkeypatch.setattr(
        "sys.argv",
        ["swing-trader", "watchdog-resolve", str(incident_id), "--outcome", "auto_fixed", "--summary", "fixed via _row_to_dict"],
    )
    cli_module.main()
    capsys.readouterr()

    watermark = database.get_state("watchdog_last_notification_id", 0)
    assert watermark == incident_id
    assert database.unresolved_run_failures(watermark) == []
    results = database.rows("SELECT * FROM notifications WHERE event_type='AUTO_FIX_RESULT'")
    assert len(results) == 1
    assert results[0]["reason_code"] == "AUTO_FIXED"
    assert json.loads(results[0]["payload_json"])["notification_id"] == incident_id


def test_watchdog_check_only_sees_failures_after_the_watermark(monkeypatch, tmp_path, capsys, database):
    _emit_run_failed(database, "first")
    first_id = database.unresolved_run_failures()[0]["notification_id"]
    database.set_state("watchdog_last_notification_id", first_id, "crash-watchdog-agent")
    _emit_run_failed(database, "second")

    result = _check(monkeypatch, database.path, capsys)
    assert result["since_id"] == first_id
    assert result["count"] == 1
    assert "second" in result["failures"][0]["payload_json"]
