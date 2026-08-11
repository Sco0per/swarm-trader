from __future__ import annotations

import pandas as pd
import pytest

from src.swing import data_feed
from src.swing.market import SECTOR_ETFS
from src.swing.risk import LEVERAGED_OR_INVERSE_ETFS
from src.swing.universe import UNIVERSE, UNIVERSE_SYMBOLS


def test_universe_excludes_leveraged_and_uses_valid_sectors():
    assert len(UNIVERSE_SYMBOLS) == len(set(UNIVERSE_SYMBOLS))
    assert not (set(UNIVERSE_SYMBOLS) & LEVERAGED_OR_INVERSE_ETFS)
    valid_sectors = set(SECTOR_ETFS) | {"Unknown"}
    for entry in UNIVERSE:
        assert entry.sector in valid_sectors
    assert len(UNIVERSE) >= 100
    assert "SPY" in UNIVERSE_SYMBOLS and "QQQ" in UNIVERSE_SYMBOLS


class _FakeTicker:
    def __init__(self, symbol, frame=None, earnings_index=None):
        self.symbol = symbol
        self._frame = frame
        self._earnings_index = earnings_index

    def history(self, period, interval, auto_adjust):
        return self._frame

    def get_earnings_dates(self, limit=4):
        if self._earnings_index is None:
            return pd.DataFrame()
        return pd.DataFrame(index=self._earnings_index)


def _sample_frame() -> pd.DataFrame:
    index = pd.date_range(end=pd.Timestamp.now("UTC"), periods=5, freq="B", tz="UTC")
    return pd.DataFrame({
        "Open": [1.0, 2.0, 3.0, 4.0, 5.0], "High": [1.5, 2.5, 3.5, 4.5, 5.5],
        "Low": [0.5, 1.5, 2.5, 3.5, 4.5], "Close": [1.2, 2.2, 3.2, 4.2, 5.2],
        "Volume": [1_000_000] * 5,
    }, index=index)


def test_fetch_bars_normalizes_columns_and_caches(monkeypatch, tmp_path):
    monkeypatch.setattr(data_feed, "CACHE_DIR", tmp_path)
    frame = _sample_frame()

    class _FakeYF:
        def Ticker(self, symbol):
            return _FakeTicker(symbol, frame=frame)

    monkeypatch.setattr(data_feed, "_yf", lambda: _FakeYF())
    bars = data_feed.fetch_bars(["AAA"], lookback_days=10)
    assert "AAA" in bars
    assert list(bars["AAA"].columns) == ["open", "high", "low", "close", "volume"]
    assert len(bars["AAA"]) == 5

    class _ExplodingYF:
        def Ticker(self, symbol):
            raise AssertionError("cache hit should not call yfinance again")

    monkeypatch.setattr(data_feed, "_yf", lambda: _ExplodingYF())
    cached = data_feed.fetch_bars(["AAA"], lookback_days=10)
    assert len(cached["AAA"]) == 5


def test_fetch_bars_skips_symbols_with_no_data(monkeypatch, tmp_path):
    monkeypatch.setattr(data_feed, "CACHE_DIR", tmp_path)

    class _FakeYF:
        def Ticker(self, symbol):
            return _FakeTicker(symbol, frame=pd.DataFrame())

    monkeypatch.setattr(data_feed, "_yf", lambda: _FakeYF())
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


def test_build_universe_assets_marks_etfs_without_earnings_lookup(monkeypatch, tmp_path):
    monkeypatch.setattr(data_feed, "CACHE_DIR", tmp_path)

    def _boom(symbol):
        raise AssertionError("ETFs must not trigger an earnings lookup")

    monkeypatch.setattr(data_feed, "fetch_earnings_trading_days", _boom)
    from src.swing.universe import UniverseEntry

    assets = data_feed.build_universe_assets([UniverseEntry(symbol="SPY", sector="Unknown", is_etf=True)])
    assert assets[0].is_etf is True
    assert assets[0].earnings_trading_days is None
    assert assets[0].is_halted is False
