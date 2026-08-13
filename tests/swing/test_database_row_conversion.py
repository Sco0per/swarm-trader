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
from datetime import datetime, timezone

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


class _RoutedFakeConnection:
    """Fake connection for functions that run several different queries per call.

    Each execute() is matched against `routes` by the first substring found
    in the query text; queries with no match (INSERT/UPDATE/BEGIN) get an
    empty cursor, since callers only ever fetch results from SELECTs they
    expect a route for.
    """

    def __init__(self, routes: dict[str, tuple[list[str], list[tuple]]]):
        self._routes = routes

    def execute(self, query, params=()):
        for needle, (columns, rows) in self._routes.items():
            if needle in query:
                return _FakeCursor(columns, rows)
        return _FakeCursor([], [])


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


def test_get_halts_cache_works_when_the_driver_returns_a_plain_tuple(monkeypatch, tmp_path):
    # Reproduces a real production failure: get_halts_cache's original
    # implementation indexed the fetched row by column name directly
    # (row["updated_at"]), which raised "tuple indices must be integers or
    # slices, not str" against the real hosted Turso database -- confirmed
    # live on 2026-08-12, the exact bug class this test file exists for.
    database = SwingDatabase(tmp_path / "x.db")
    fresh = datetime.now(timezone.utc).isoformat()

    @contextmanager
    def fake_connect():
        yield _FakeConnection(["halted_symbols_json", "updated_at"], [('["AAA", "BBB"]', fresh)])

    monkeypatch.setattr(database, "connect", fake_connect)

    assert database.get_halts_cache(ttl_seconds=86400) == {"AAA", "BBB"}


def test_get_halts_cache_returns_none_without_touching_row_conversion_when_absent(monkeypatch, tmp_path):
    database = SwingDatabase(tmp_path / "x.db")

    @contextmanager
    def fake_connect():
        yield _FakeConnection(["halted_symbols_json", "updated_at"], [])

    monkeypatch.setattr(database, "connect", fake_connect)

    assert database.get_halts_cache(ttl_seconds=86400) is None


def test_get_state_works_when_the_driver_returns_a_plain_tuple(monkeypatch, tmp_path):
    # Reproduces the real production failure this test file was extended
    # for: get_state's original implementation indexed the fetched row by
    # column name directly (row["value_json"]), which raised "tuple indices
    # must be integers or slices, not str" against the real hosted Turso
    # database while running `swing-trader report daily` -- confirmed live
    # on 2026-08-12.
    database = SwingDatabase(tmp_path / "x.db")

    @contextmanager
    def fake_connect():
        yield _FakeConnection(["value_json"], [('{"on": true}',)])

    monkeypatch.setattr(database, "connect", fake_connect)

    assert database.get_state("kill_switch", False) == {"on": True}


def test_get_state_returns_default_without_touching_row_conversion_when_absent(monkeypatch, tmp_path):
    database = SwingDatabase(tmp_path / "x.db")

    @contextmanager
    def fake_connect():
        yield _FakeConnection(["value_json"], [])

    monkeypatch.setattr(database, "connect", fake_connect)

    assert database.get_state("missing_key", "fallback") == "fallback"


def test_equity_high_works_when_the_driver_returns_a_plain_tuple(monkeypatch, tmp_path):
    database = SwingDatabase(tmp_path / "x.db")

    @contextmanager
    def fake_connect():
        yield _FakeConnection(["high"], [(12345.67,)])

    monkeypatch.setattr(database, "connect", fake_connect)

    assert database.equity_high(fallback=0.0) == 12345.67


def test_equity_high_falls_back_when_no_snapshots_exist(monkeypatch, tmp_path):
    database = SwingDatabase(tmp_path / "x.db")

    @contextmanager
    def fake_connect():
        yield _FakeConnection(["high"], [(None,)])

    monkeypatch.setattr(database, "connect", fake_connect)

    assert database.equity_high(fallback=100.0) == 100.0


def test_equity_high_since_works_when_the_driver_returns_a_plain_tuple(monkeypatch, tmp_path):
    database = SwingDatabase(tmp_path / "x.db")

    @contextmanager
    def fake_connect():
        yield _FakeConnection(["high"], [(999.0,)])

    monkeypatch.setattr(database, "connect", fake_connect)

    assert database.equity_high_since("2026-01-01", fallback=0.0) == 999.0


def test_trade_price_extremes_works_when_the_driver_returns_a_plain_tuple(monkeypatch, tmp_path):
    database = SwingDatabase(tmp_path / "x.db")

    @contextmanager
    def fake_connect():
        yield _FakeConnection(["high", "low"], [(105.5, 98.25)])

    monkeypatch.setattr(database, "connect", fake_connect)

    assert database.trade_price_extremes("t1", fallback=100.0) == (105.5, 98.25)


def test_trade_price_extremes_falls_back_when_no_snapshots_exist(monkeypatch, tmp_path):
    database = SwingDatabase(tmp_path / "x.db")

    @contextmanager
    def fake_connect():
        yield _FakeConnection(["high", "low"], [(None, None)])

    monkeypatch.setattr(database, "connect", fake_connect)

    assert database.trade_price_extremes("t1", fallback=100.0) == (100.0, 100.0)


def test_record_entry_fill_works_when_the_driver_returns_plain_tuples(monkeypatch, tmp_path):
    # Reproduces the real production failure: record_entry_fill's original
    # implementation indexed trade["status"], trade["initial_stop"], and
    # totals["quantity"]/["cost"] directly, which raised the identical
    # "tuple indices must be integers or slices, not str" against the real
    # hosted Turso database while running `swing-trader reconcile` on the
    # ordinary case (a healthy open position with a known fill price) --
    # confirmed live on 2026-08-12.
    database = SwingDatabase(tmp_path / "x.db")

    @contextmanager
    def fake_connect():
        yield _RoutedFakeConnection(
            {
                "FROM trades WHERE trade_id": (["status", "initial_stop"], [("OPEN", 45.0)]),
                "side='EXIT'": (["n"], [(0,)]),
                "side='ENTRY'": (["quantity", "cost"], [(10.0, 500.0)]),
            }
        )

    monkeypatch.setattr(database, "connect", fake_connect)

    database.record_entry_fill("t1", quantity=10.0, price=50.0, filled_at="2026-08-12T00:00:00+00:00", broker_fill_id="f1")


def test_reserve_entry_admission_works_when_the_driver_returns_plain_tuples(monkeypatch, tmp_path):
    # Reproduces the real production failure: reserve_entry_admission's
    # original implementation indexed rows from three different SELECTs
    # (active reservations, open positions, open positions with sector) by
    # column name, plus `"payload_json" in row.keys()` -- plain tuples have
    # no .keys() either. Currently masked in production only because
    # TRADING_ENABLED is off and an earlier step in the run pipeline fails
    # first, but this is the entry-admission gate every future trade must
    # clear once real setups start matching.
    database = SwingDatabase(tmp_path / "x.db")

    @contextmanager
    def fake_connect():
        yield _RoutedFakeConnection(
            {
                "entry_admissions WHERE intent_id": ([], []),
                "trade_id IS NULL": (["ticker", "planned_dollar_risk", "created_at", "payload_json"], []),
                "COUNT(*) FROM trades WHERE entry_datetime": (["n"], [(0,)]),
                "ticker, planned_dollar_risk FROM trades WHERE status IN": (["ticker", "planned_dollar_risk"], []),
                "ticker,sector,planned_dollar_risk": (["ticker", "sector", "planned_dollar_risk"], []),
            }
        )

    monkeypatch.setattr(database, "connect", fake_connect)

    result = database.reserve_entry_admission(
        intent_id="i1",
        decision_id="d1",
        ticker="AAPL",
        planned_dollar_risk=500.0,
        account_equity=100_000.0,
        payload={"intent": {"quantity": 10}},
        day_start_iso="2026-08-12T00:00:00+00:00",
        week_start_iso="2026-08-10T00:00:00+00:00",
        broker_position_symbols=set(),
        max_positions=3,
        max_day=1,
        max_week=3,
        max_open_risk_pct=0.02,
        sector="Technology",
        cluster="mega_cap_tech",
        cluster_by_ticker={},
        max_sector_risk_pct=0.015,
        max_cluster_risk_pct=0.01,
    )

    assert result == (True, None, "Entry admission reserved")
