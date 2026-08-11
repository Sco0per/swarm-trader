from __future__ import annotations

import pandas as pd
import pytest

from src.swing import data_feed
from src.swing.market import SECTOR_ETFS
from src.swing.risk import LEVERAGED_OR_INVERSE_ETFS
from src.swing.universe import UNIVERSE, UNIVERSE_SYMBOLS


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
                    "o": 1.0 + i, "h": 1.5 + i, "l": 0.5 + i, "c": 1.2 + i, "v": 1_000_000,
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
    assert all(0 < days <= 10 for days in results.values())
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

    results = data_feed.fetch_earnings_trading_days_batch(["AAA"], database=_FakeDatabase())
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

    results = data_feed.fetch_earnings_trading_days_batch(["AAA"], database=_BrokenDatabase())
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
    assets = data_feed.build_universe_assets(entries, database=sentinel_database)

    assert seen["symbols"] == ["AAA"]
    assert seen["database"] is sentinel_database
    assert assets[0].earnings_trading_days is None
