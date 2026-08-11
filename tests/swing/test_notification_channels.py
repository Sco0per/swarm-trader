from __future__ import annotations

from src.swing import cli as cli_module
from src.swing.config import SwingSettings, TelegramSettings
from src.swing.notification_channels import (
    FakeNotificationChannel,
    TelegramChannel,
    drain_pending_notifications,
)
from src.swing.notifications import NotificationService, NotificationType


def _emit(database, message: str = "No trade found (0 candidates)") -> None:
    NotificationService(database).emit(
        NotificationType.RUN_CYCLE_SUMMARY,
        severity="INFO",
        title=message,
        reason_code="NO_TRADE",
        payload={"message": message},
    )


def test_drain_marks_rows_sent_when_channel_succeeds(database):
    _emit(database)
    channel = FakeNotificationChannel()
    result = drain_pending_notifications(database, channel)
    assert result == {"attempted": 1, "sent": 1, "failed": 0}
    assert channel.sent == ["[INFO] No trade found (0 candidates)"]
    statuses = [row["delivery_status"] for row in database.rows("SELECT delivery_status FROM notifications")]
    assert statuses == ["SENT"]


def test_drain_marks_rows_failed_when_channel_fails(database):
    _emit(database)
    result = drain_pending_notifications(database, FakeNotificationChannel(fail=True))
    assert result == {"attempted": 1, "sent": 0, "failed": 1}
    statuses = [row["delivery_status"] for row in database.rows("SELECT delivery_status FROM notifications")]
    assert statuses == ["FAILED"]


def test_drain_is_a_noop_without_a_configured_channel(database):
    _emit(database)
    result = drain_pending_notifications(database, None)
    assert result == {"attempted": 0, "sent": 0, "failed": 0}
    statuses = [row["delivery_status"] for row in database.rows("SELECT delivery_status FROM notifications")]
    assert statuses == ["PENDING"]


def test_telegram_channel_never_raises_and_never_leaks_the_token(monkeypatch, capsys):
    def _boom(*args, **kwargs):
        raise RuntimeError("network exploded")

    monkeypatch.setattr("src.swing.notification_channels.httpx.post", _boom)
    channel = TelegramChannel("super-secret-token", "12345")
    assert channel.send("hello") is False
    captured = capsys.readouterr()
    assert "super-secret-token" not in captured.out
    assert "super-secret-token" not in captured.err


def test_telegram_channel_sends_expected_request(monkeypatch):
    captured = {}

    class _Response:
        status_code = 200

    def _post(url, json, timeout):
        captured["url"] = url
        captured["json"] = json
        return _Response()

    monkeypatch.setattr("src.swing.notification_channels.httpx.post", _post)
    channel = TelegramChannel("tok", "chat-1")
    assert channel.send("hi there") is True
    assert captured["url"] == "https://api.telegram.org/bottok/sendMessage"
    assert captured["json"] == {"chat_id": "chat-1", "text": "hi there"}


def test_telegram_channel_returns_false_on_non_200(monkeypatch):
    class _Response:
        status_code = 401

    monkeypatch.setattr("src.swing.notification_channels.httpx.post", lambda *a, **k: _Response())
    assert TelegramChannel("tok", "chat-1").send("hi") is False


def test_channel_if_configured_is_none_without_both_credentials(monkeypatch, tmp_path):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    settings = SwingSettings(database_path=tmp_path / "x.db")
    assert settings.telegram.bot_token is None
    assert cli_module._channel_if_configured(settings) is None


def test_channel_if_configured_returns_telegram_channel_when_both_set(tmp_path):
    settings = SwingSettings(
        database_path=tmp_path / "x.db",
        telegram=TelegramSettings(bot_token="tok", chat_id="chat-1"),
    )
    channel = cli_module._channel_if_configured(settings)
    assert isinstance(channel, TelegramChannel)
