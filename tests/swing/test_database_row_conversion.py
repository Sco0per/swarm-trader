"""Regression coverage for the dict(row) vs turso_serverless.Row bug.

Every real query in tests/swing runs against actual sqlite3.Row objects,
which happen to support .keys() and so never exercise the failure mode
that broke every read against the real hosted Turso database in
production: dict(row) silently requires a .keys() method, and
turso_serverless's Row supports index/column-name __getitem__ the same
way sqlite3.Row does but not .keys() -- dict() then misinterprets the row
as an iterable of (key, value) pairs and raises on the first plain value.
These tests fake the driver layer with plain tuples (no .keys() at all,
the same shape that broke) to prove the fix is driver-agnostic rather
than accidentally sqlite3-specific.
"""

from __future__ import annotations

from contextlib import contextmanager

from src.swing.database import _row_to_dict, SwingDatabase


class _FakeCursor:
    def __init__(self, columns: list[str], rows: list[tuple]):
        self.description = [(column,) for column in columns]
        self._rows = rows

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None


class _FakeConnection:
    def __init__(self, columns: list[str], rows: list[tuple]):
        self._columns = columns
        self._rows = rows

    def execute(self, query, params=()):
        return _FakeCursor(self._columns, self._rows)


class _DescriptionOnly:
    description = [("a",), ("b",)]


def test_row_to_dict_builds_a_mapping_from_a_plain_tuple():
    assert _row_to_dict(_DescriptionOnly(), (1, 2)) == {"a": 1, "b": 2}


def test_rows_works_when_the_driver_returns_plain_tuples_not_row_objects(monkeypatch, tmp_path):
    database = SwingDatabase(tmp_path / "x.db")

    @contextmanager
    def fake_connect():
        yield _FakeConnection(["total", "major"], [(3, 1)])

    monkeypatch.setattr(database, "connect", fake_connect)

    assert database.rows("SELECT COUNT(*) AS total, SUM(x) AS major FROM t") == [{"total": 3, "major": 1}]


def test_get_trade_works_when_the_driver_returns_a_plain_tuple(monkeypatch, tmp_path):
    database = SwingDatabase(tmp_path / "x.db")

    @contextmanager
    def fake_connect():
        yield _FakeConnection(["trade_id", "ticker", "status"], [("t1", "AAPL", "OPEN")])

    monkeypatch.setattr(database, "connect", fake_connect)

    assert database.get_trade("t1") == {"trade_id": "t1", "ticker": "AAPL", "status": "OPEN"}


def test_get_trade_returns_none_without_touching_row_conversion_when_absent(monkeypatch, tmp_path):
    database = SwingDatabase(tmp_path / "x.db")

    @contextmanager
    def fake_connect():
        yield _FakeConnection(["trade_id"], [])

    monkeypatch.setattr(database, "connect", fake_connect)

    assert database.get_trade("missing") is None
