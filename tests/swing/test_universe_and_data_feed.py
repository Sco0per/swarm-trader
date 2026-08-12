from __future__ import annotations

import pandas as pd

from src.swing import data_feed
from src.swing.market import SECTOR_ETFS
from src.swing.risk import LEVERAGED_OR_INVERSE_ETFS
from src.swing.universe import load_universe, UNIVERSE, UNIVERSE_SYMBOLS


def test_earnings_cache_round_trips_through_database(database):
    assert database.get_earnings_cache_many(["AAA", "BBB"], ttl_seconds=86400) == {}

    database.set_earnings_cache_many([("AAA", 7), ("BBB", None)])
    cached = database.get_earnings_cache_many(["AAA", "BBB", "CCC"], ttl_seconds=86400)
    assert cached == {"AAA": 7, "BBB": None}

    assert database.get_earnings_cache_many(["AAA"], ttl_seconds=0) == {}


def test_universe_excludes_leveraged_and_uses_valid_sectors():
    assert len(UNIVERSE_SYMBOLS) == len(set(UNIVERSE_SYMBOLS))
    assert not (set(UNIVERSE_SYMBOLS) & LEVERAGED_OR_INVERSE_ETFS)
    valid_sectors = set(SECTOR_ETFS) | {"Unknown"}
    for entry in UNIVERSE:
        assert entry.sector in valid_sectors
    assert len(UNIVERSE) >= 100
    assert "SPY" in UNIVERSE_SYMBOLS and "QQQ" in UNIVERSE_SYMBOLS


def test_custom_universe_cannot_add_an_unapproved_etf(tmp_path):
    path = tmp_path / "universe.csv"
    path.write_text(
        "symbol,sector,security_type,source_membership\nMYSTERY,Technology,etf,CUSTOM\n",
        encoding="utf-8",
    )
    try:
        load_universe(path)
    except RuntimeError as exc:
        assert "unleveraged ETF allowlist" in str(exc)
    else:
        raise AssertionError("An unapproved ETF must fail closed")


class _FakeTicker:
    def __init__(self, symbol, earnings_index=None):
        self.symbol = symbol
        self._earnings_index = earnings_index

    def get_earnings_dates(self, limit=4):
        if self._earnings_index is None:
            return pd.DataFrame()
        return pd.DataFrame(index=self._earnings_index)


def _sample_alpaca_bars(symbol: str, count: int = 5) -> dict:
    base = pd.Timestamp.now("UTC").normalize()
    return {
        "bars": {
            symbol: [
                {
                    "t": (base - pd.Timedelta(days=count - i)).isoformat(),
                    "o": 1.0 + i,
                    "h": 1.5 + i,
                    "l": 0.5 + i,
                    "c": 1.2 + i,
                    "v": 1_000_000,
                }
                for i in range(count)
            ]
        }
    }


def test_fetch_bars_normalizes_columns_and_caches(monkeypatch, tmp_path):
    monkeypatch.setattr(data_feed, "CACHE_DIR", tmp_path)
    payload = _sample_alpaca_bars("AAA")

    monkeypatch.setattr(data_feed, "_alpaca_get", lambda path, params: payload)
    bars = data_feed.fetch_bars(["AAA"], lookback_days=10)
    assert "AAA" in bars
    assert list(bars["AAA"].columns) == ["open", "high", "low", "close", "volume"]
    assert len(bars["AAA"]) == 5

    def _exploding_get(path, params):
        raise AssertionError("cache hit should not call Alpaca again")

    monkeypatch.setattr(data_feed, "_alpaca_get", _exploding_get)
    cached = data_feed.fetch_bars(["AAA"], lookback_days=10)
    assert len(cached["AAA"]) == 5


def test_fetch_bars_skips_symbols_with_no_data(monkeypatch, tmp_path):
    monkeypatch.setattr(data_feed, "CACHE_DIR", tmp_path)

    monkeypatch.setattr(data_feed, "_alpaca_get", lambda path, params: {"bars": {}})
    bars = data_feed.fetch_bars(["MISSING"], lookback_days=10)
    assert bars == {}


def test_fetch_earnings_trading_days_counts_weekdays_only(monkeypatch, tmp_path):
    monkeypatch.setattr(data_feed, "CACHE_DIR", tmp_path)
    future = pd.Timestamp.now("UTC").normalize() + pd.Timedelta(days=10)

    class _FakeYF:
        def Ticker(self, symbol):
            return _FakeTicker(symbol, earnings_index=pd.DatetimeIndex([future]))

    monkeypatch.setattr(data_feed, "_yf", lambda: _FakeYF())
    trading_days = data_feed.fetch_earnings_trading_days("AAA")
    assert trading_days is not None
    assert 0 < trading_days <= 10


def test_fetch_earnings_trading_days_none_when_unavailable(monkeypatch, tmp_path):
    monkeypatch.setattr(data_feed, "CACHE_DIR", tmp_path)

    class _FakeYF:
        def Ticker(self, symbol):
            return _FakeTicker(symbol, earnings_index=None)

    monkeypatch.setattr(data_feed, "_yf", lambda: _FakeYF())
    assert data_feed.fetch_earnings_trading_days("AAA") is None


def test_fetch_earnings_trading_days_batch_runs_concurrently(monkeypatch, tmp_path):
    """N symbols should cost ~one lookup's wall time, not N times it."""
    monkeypatch.setattr(data_feed, "CACHE_DIR", tmp_path)
    future = pd.Timestamp.now("UTC").normalize() + pd.Timedelta(days=10)
    delay_seconds = 0.2
    symbols = [f"SYM{i}" for i in range(8)]

    class _SlowTicker:
        def __init__(self, symbol):
            self.symbol = symbol

        def get_earnings_dates(self, limit=4):
            import time as _time

            _time.sleep(delay_seconds)
            return pd.DataFrame(index=pd.DatetimeIndex([future]))

    class _FakeYF:
        def Ticker(self, symbol):
            return _SlowTicker(symbol)

    monkeypatch.setattr(data_feed, "_yf", lambda: _FakeYF())
    import time

    started = time.monotonic()
    results = data_feed.fetch_earnings_trading_days_batch(symbols)
    elapsed = time.monotonic() - started

    assert set(results) == set(symbols)
    assert all(days is not None and 0 < days <= 10 for days in results.values())
    assert elapsed < delay_seconds * len(symbols)


def test_fetch_earnings_trading_days_batch_uses_turso_cache_and_skips_lookup(monkeypatch, tmp_path):
    monkeypatch.setattr(data_feed, "CACHE_DIR", tmp_path)

    def _boom(symbol):
        raise AssertionError("cache hit should not call yfinance")

    monkeypatch.setattr(data_feed, "_yf", _boom)

    class _FakeDatabase:
        def get_earnings_cache_many(self, symbols, ttl_seconds):
            return {"AAA": 5}

        def set_earnings_cache_many(self, entries):
            raise AssertionError("nothing should need writing back on a full cache hit")

    results = data_feed.fetch_earnings_trading_days_batch(["AAA"], database=_FakeDatabase())  # type: ignore[arg-type]
    assert results == {"AAA": 5}


def test_fetch_earnings_trading_days_batch_survives_turso_errors(monkeypatch, tmp_path):
    monkeypatch.setattr(data_feed, "CACHE_DIR", tmp_path)
    future = pd.Timestamp.now("UTC").normalize() + pd.Timedelta(days=3)

    class _FakeYF:
        def Ticker(self, symbol):
            return _FakeTicker(symbol, earnings_index=pd.DatetimeIndex([future]))

    monkeypatch.setattr(data_feed, "_yf", lambda: _FakeYF())

    class _BrokenDatabase:
        def get_earnings_cache_many(self, symbols, ttl_seconds):
            raise RuntimeError("Turso unreachable")

        def set_earnings_cache_many(self, entries):
            raise RuntimeError("Turso unreachable")

    results = data_feed.fetch_earnings_trading_days_batch(["AAA"], database=_BrokenDatabase())  # type: ignore[arg-type]
    assert results["AAA"] is not None


def test_build_universe_assets_marks_etfs_without_earnings_lookup(monkeypatch, tmp_path):
    monkeypatch.setattr(data_feed, "CACHE_DIR", tmp_path)

    def _boom(symbols, database=None):
        raise AssertionError("ETFs must not trigger an earnings lookup")

    monkeypatch.setattr(data_feed, "fetch_earnings_trading_days_batch", _boom)
    from src.swing.universe import UniverseEntry

    assets = data_feed.build_universe_assets([UniverseEntry(symbol="SPY", sector="Unknown", is_etf=True)])
    assert assets[0].is_etf is True
    assert assets[0].earnings_trading_days is None
    assert assets[0].is_halted is False


def test_build_universe_assets_passes_database_through_for_stocks(monkeypatch, tmp_path):
    monkeypatch.setattr(data_feed, "CACHE_DIR", tmp_path)
    seen: dict = {}

    def _fake_batch(symbols, database=None):
        seen["symbols"] = symbols
        seen["database"] = database
        return {symbol: None for symbol in symbols}

    monkeypatch.setattr(data_feed, "fetch_earnings_trading_days_batch", _fake_batch)
    from src.swing.universe import UniverseEntry

    sentinel_database = object()
    entries = [UniverseEntry(symbol="AAA", sector="Technology", is_etf=False)]
    assets = data_feed.build_universe_assets(entries, database=sentinel_database)  # type: ignore[arg-type]

    assert seen["symbols"] == ["AAA"]
    assert seen["database"] is sentinel_database
    assert assets[0].earnings_trading_days is None


_HALTS_FEED_XML = """<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0" xmlns:ndaq="http://www.nasdaqtrader.com/">
  <channel>
    <item>
      <ndaq:IssueSymbol>FRESH</ndaq:IssueSymbol>
      <ndaq:ResumptionDate />
    </item>
    <item>
      <ndaq:IssueSymbol>STALE</ndaq:IssueSymbol>
      <ndaq:ResumptionDate />
    </item>
    <item>
      <ndaq:IssueSymbol>STALE</ndaq:IssueSymbol>
      <ndaq:ResumptionDate>08/09/2026</ndaq:ResumptionDate>
    </item>
    <item>
      <ndaq:IssueSymbol>RESUMED</ndaq:IssueSymbol>
      <ndaq:ResumptionDate>08/10/2026</ndaq:ResumptionDate>
    </item>
  </channel>
</rss>"""


class _FakeHaltsResponse:
    def __init__(self, content: bytes, status: int = 200):
        self.content = content
        self._status = status

    def raise_for_status(self):
        if self._status != 200:
            raise RuntimeError(f"HTTP {self._status}")


def test_fetch_current_halts_uses_latest_event_per_symbol(monkeypatch, tmp_path):
    monkeypatch.setattr(data_feed, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(data_feed.requests, "get", lambda *a, **k: _FakeHaltsResponse(_HALTS_FEED_XML.encode()))

    halted = data_feed.fetch_current_halts()

    assert halted is not None
    assert halted == {"FRESH", "STALE"}, "STALE's most recent (first, newest-first) entry has no resumption yet"
    assert "RESUMED" not in halted


def test_fetch_current_halts_caches_between_calls(monkeypatch, tmp_path):
    monkeypatch.setattr(data_feed, "CACHE_DIR", tmp_path)
    calls = {"count": 0}

    def _get(*args, **kwargs):
        calls["count"] += 1
        return _FakeHaltsResponse(_HALTS_FEED_XML.encode())

    monkeypatch.setattr(data_feed.requests, "get", _get)

    first = data_feed.fetch_current_halts()
    second = data_feed.fetch_current_halts()

    assert first == second == {"FRESH", "STALE"}
    assert calls["count"] == 1, "second call must be served from cache, not a second network fetch"


def test_fetch_current_halts_returns_none_and_does_not_cache_on_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(data_feed, "CACHE_DIR", tmp_path)

    def _boom(*args, **kwargs):
        raise RuntimeError("network unreachable")

    monkeypatch.setattr(data_feed.requests, "get", _boom)
    assert data_feed.fetch_current_halts() is None

    # A failure must not poison the cache -- a later successful fetch should still work.
    monkeypatch.setattr(data_feed.requests, "get", lambda *a, **k: _FakeHaltsResponse(_HALTS_FEED_XML.encode()))
    assert data_feed.fetch_current_halts() == {"FRESH", "STALE"}


def test_fetch_current_halts_returns_none_on_non_200(monkeypatch, tmp_path):
    monkeypatch.setattr(data_feed, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(data_feed.requests, "get", lambda *a, **k: _FakeHaltsResponse(b"", status=503))
    assert data_feed.fetch_current_halts() is None


def test_build_universe_assets_marks_symbol_halted_from_feed(monkeypatch, tmp_path):
    monkeypatch.setattr(data_feed, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(data_feed, "fetch_earnings_trading_days_batch", lambda symbols, database=None: {s: None for s in symbols})
    from src.swing.universe import UniverseEntry

    entries = [
        UniverseEntry(symbol="HALTED", sector="Technology", is_etf=False),
        UniverseEntry(symbol="CLEAR", sector="Technology", is_etf=False),
    ]
    assets = data_feed.build_universe_assets(entries, halted_symbols={"HALTED"}, production_metadata=True)
    by_symbol = {a.symbol: a for a in assets}

    assert by_symbol["HALTED"].halt_status_known is True
    assert by_symbol["HALTED"].is_halted is True
    assert by_symbol["CLEAR"].halt_status_known is True
    assert by_symbol["CLEAR"].is_halted is False


def test_build_universe_assets_fails_closed_when_halts_feed_unavailable(monkeypatch, tmp_path):
    monkeypatch.setattr(data_feed, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(data_feed, "fetch_earnings_trading_days_batch", lambda symbols, database=None: {s: None for s in symbols})
    from src.swing.universe import UniverseEntry

    entries = [UniverseEntry(symbol="AAA", sector="Technology", is_etf=False)]
    assets = data_feed.build_universe_assets(entries, halted_symbols=None, production_metadata=True)

    assert assets[0].halt_status_known is False, "an unreachable halts feed must fail closed, not assume nothing is halted"


def test_build_universe_assets_non_production_path_keeps_halt_status_known_true(monkeypatch, tmp_path):
    monkeypatch.setattr(data_feed, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(data_feed, "fetch_earnings_trading_days_batch", lambda symbols, database=None: {s: None for s in symbols})
    from src.swing.universe import UniverseEntry

    entries = [UniverseEntry(symbol="AAA", sector="Technology", is_etf=False)]
    assets = data_feed.build_universe_assets(entries, production_metadata=False)

    assert assets[0].halt_status_known is True
    assert assets[0].is_halted is False
