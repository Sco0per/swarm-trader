"""Small, dependency-free guards for secret-safe persistence and logging."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

REDACTED = "[REDACTED]"
_SENSITIVE_KEY = re.compile(r"(?:api[_-]?key|api[_-]?secret|auth[_-]?token|access[_-]?token|password|credential|client[_-]?secret)", re.IGNORECASE)
_SECRET_VALUE = re.compile(r"(?i)(?:sk-ant-[A-Za-z0-9_-]{12,}|sk-[A-Za-z0-9_-]{12,}|(?:bearer\s+)[A-Za-z0-9._~+/=-]{12,}|(?:api[_-]?(?:key|secret)|auth[_-]?token|password)\s*[=:]\s*[^\s,;]+)")


def redact_text(value: Any) -> str:
    """Return text with common credential shapes removed."""

    return _SECRET_VALUE.sub(REDACTED, str(value))


def redact_sensitive(value: Any) -> Any:
    """Recursively redact secret-bearing keys and recognizable token values."""

    if isinstance(value, Mapping):
        return {str(key): REDACTED if _SENSITIVE_KEY.search(str(key)) else redact_sensitive(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return tuple(redact_sensitive(item) for item in value)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [redact_sensitive(item) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    return value
