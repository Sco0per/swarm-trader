"""Outbound delivery for durable notification rows. A delivery failure must
never affect the trading pipeline -- see drain_pending_notifications, the
only place this module is called from (src/swing/cli.py's main())."""

from __future__ import annotations

import json
from abc import ABC, abstractmethod

import httpx

from .database import SwingDatabase
from .security import redact_text

_TELEGRAM_TEXT_LIMIT = 4000  # Telegram's sendMessage cap is 4096; leave headroom


class NotificationChannel(ABC):
    @abstractmethod
    def send(self, text: str) -> bool:
        """Return True on confirmed delivery. Must never raise."""
        ...


class TelegramChannel(NotificationChannel):
    def __init__(self, bot_token: str, chat_id: str, *, timeout_seconds: float = 10.0):
        self._bot_token = bot_token
        self._chat_id = chat_id
        self._timeout = timeout_seconds

    def send(self, text: str) -> bool:
        try:
            response = httpx.post(
                f"https://api.telegram.org/bot{self._bot_token}/sendMessage",
                json={"chat_id": self._chat_id, "text": text[:_TELEGRAM_TEXT_LIMIT]},
                timeout=self._timeout,
            )
            return response.status_code == 200
        except Exception:
            # Never str()/log this exception: Telegram's Bot API puts the
            # token in the URL path (.../bot<TOKEN>/sendMessage), not a
            # header, so a raised httpx error's own text can embed it.
            # redact_text()'s key=value/Bearer-shaped regex won't reliably
            # catch a bare token in that position -- swallow instead.
            return False


class FakeNotificationChannel(NotificationChannel):
    """Deterministic double for tests. Never touches the network."""

    def __init__(self, *, fail: bool = False):
        self.sent: list[str] = []
        self._fail = fail

    def send(self, text: str) -> bool:
        self.sent.append(text)
        return not self._fail


def drain_pending_notifications(database: SwingDatabase, channel: NotificationChannel | None, *, limit: int = 50) -> dict:
    """Best-effort delivery of PENDING notification rows. Never raises."""
    if channel is None:
        return {"attempted": 0, "sent": 0, "failed": 0}
    attempted = sent = failed = 0
    for row in database.rows(
        "SELECT * FROM notifications WHERE delivery_status='PENDING' ORDER BY created_at ASC LIMIT ?",
        (limit,),
    ):
        attempted += 1
        try:
            ok = bool(channel.send(redact_text(_text_for(row))))
        except Exception:
            ok = False
        database.mark_notification_delivery(row["notification_id"], "SENT" if ok else "FAILED")
        sent += int(ok)
        failed += int(not ok)
    return {"attempted": attempted, "sent": sent, "failed": failed}


def _text_for(row: dict) -> str:
    payload = json.loads(row["payload_json"]) if row.get("payload_json") else {}
    message = payload.get("message")
    body = message if isinstance(message, str) and message else row["title"]
    return f"[{row['severity']}] {body}"
