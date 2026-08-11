"""yfinance-based data feed adapter for the deterministic swing scanner.

Mirrors the lazy-import + disk-cache pattern already used by
``src.tools.api_free`` so this module has no hard yfinance/network dependency
at import time; tests monkeypatch ``_yf`` to run fully offline.

Known limitation (documented, not silently papered over): there is no free,
reliable halt-status feed, so ``UniverseAsset.is_halted`` always defaults to
``False`` here. This matches the gap already called out in
``docs/REMEDIATION_STATUS.md``.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from .market import SECTOR_ETFS, UniverseAsset
from .universe import UNIVERSE, UniverseEntry

CACHE_DIR = Path(__file__).resolve().parents[2] / ".cache" / "swing_data_feed"
BAR_CACHE_TTL_SECONDS = 6 * 3600
EARNINGS_CACHE_TTL_SECONDS = 24 * 3600
REQUIRED_COLUMNS = ("open", "high", "low", "close", "volume")


def _yf():
    import yfinance as yf

    return yf


def _cache_path(namespace: str, key: str) -> Path:
    safe = hashlib.md5(key.encode()).hexdigest()
    return CACHE_DIR / namespace / f"{safe}.json"


def _cache_get(namespace: str, key: str, ttl_seconds: int):
    path = _cache_path(namespace, key)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text())
        if time.time() - payload.get("ts", 0) < ttl_seconds:
            return payload["data"]
    except Exception:
        return None
    return None


def _cache_set(namespace: str, key: str, data) -> None:
    path = _cache_path(namespace, key)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.write_text(json.dumps({"ts": time.time(), "data": data}, default=str))
    except Exception:
        pass


def _bars_to_records(frame: pd.DataFrame) -> list[dict]:
    records = []
    for index, row in frame.iterrows():
        records.append({
            "date": index.isoformat() if hasattr(index, "isoformat") else str(index),
            "open": float(row["open"]), "high": float(row["high"]),
            "low": float(row["low"]), "close": float(row["close"]), "volume": float(row["volume"]),
        })
    return records


def _records_to_frame(records: list[dict]) -> pd.DataFrame:
    frame = pd.DataFrame(records)
    frame["date"] = pd.to_datetime(frame["date"], utc=True)
    frame = frame.set_index("date").sort_index()
    return frame[list(REQUIRED_COLUMNS)].astype(float)


def fetch_bars(symbols: list[str], *, lookback_days: int = 320) -> dict[str, pd.DataFrame]:
    """Fetch daily OHLCV bars per symbol, disk-cached for BAR_CACHE_TTL_SECONDS."""
    yf = _yf()
    result: dict[str, pd.DataFrame] = {}
    for symbol in symbols:
        cache_key = f"{symbol}:{lookback_days}"
        cached = _cache_get("bars", cache_key, BAR_CACHE_TTL_SECONDS)
        if cached:
            try:
                result[symbol] = _records_to_frame(cached)
                continue
            except Exception:
                pass
        try:
            frame = yf.Ticker(symbol).history(period=f"{lookback_days + 30}d", interval="1d", auto_adjust=True)
        except Exception:
            continue
        if frame is None or frame.empty:
            continue
        frame = frame.rename(columns=str.lower)
        if not set(REQUIRED_COLUMNS).issubset(frame.columns):
            continue
        frame = frame[list(REQUIRED_COLUMNS)].dropna()
        if frame.empty:
            continue
        result[symbol] = frame
        _cache_set("bars", cache_key, _bars_to_records(frame))
    return result


def _approx_trading_days_until(target: date, today: date) -> int:
    if target <= today:
        return 0
    days = 0
    current = today
    while current < target:
        current += timedelta(days=1)
        if current.weekday() < 5:
            days += 1
    return days


def fetch_earnings_trading_days(symbol: str) -> int | None:
    """Approximate trading days until the next known earnings date, or None if unknown."""
    cached = _cache_get("earnings", symbol, EARNINGS_CACHE_TTL_SECONDS)
    if cached is not None:
        return cached.get("trading_days")
    yf = _yf()
    try:
        calendar = yf.Ticker(symbol).get_earnings_dates(limit=4)
    except Exception:
        _cache_set("earnings", symbol, {"trading_days": None})
        return None
    index = getattr(calendar, "index", None)
    if calendar is None or index is None or len(index) == 0:
        _cache_set("earnings", symbol, {"trading_days": None})
        return None
    today = datetime.now(timezone.utc).date()
    upcoming = [
        (idx.date() if hasattr(idx, "date") else idx)
        for idx in index
        if (idx.date() if hasattr(idx, "date") else idx) >= today
    ]
    if not upcoming:
        _cache_set("earnings", symbol, {"trading_days": None})
        return None
    trading_days = _approx_trading_days_until(min(upcoming), today)
    _cache_set("earnings", symbol, {"trading_days": trading_days})
    return trading_days


def build_universe_assets(entries: list[UniverseEntry] | None = None) -> list[UniverseAsset]:
    entries = entries if entries is not None else UNIVERSE
    assets: list[UniverseAsset] = []
    for entry in entries:
        earnings_days = None if entry.is_etf else fetch_earnings_trading_days(entry.symbol)
        assets.append(UniverseAsset(
            symbol=entry.symbol,
            sector=entry.sector,
            is_etf=entry.is_etf,
            is_tradable=True,
            is_halted=False,
            is_leveraged_or_inverse=False,
            earnings_trading_days=earnings_days,
        ))
    return assets


@dataclass
class ScanInputs:
    assets: list[UniverseAsset]
    bars_by_symbol: dict[str, pd.DataFrame]
    spy_bars: pd.DataFrame
    qqq_bars: pd.DataFrame
    sector_bars: dict[str, pd.DataFrame]


def load_scan_inputs(*, entries: list[UniverseEntry] | None = None, lookback_days: int = 320) -> ScanInputs:
    """Fetch everything DeterministicSwingScanner.scan() needs, in one call."""
    entries = entries if entries is not None else UNIVERSE
    symbols = sorted({entry.symbol for entry in entries} | {"SPY", "QQQ"} | set(SECTOR_ETFS.values()))
    bars = fetch_bars(symbols, lookback_days=lookback_days)
    spy_bars = bars.get("SPY")
    qqq_bars = bars.get("QQQ")
    if spy_bars is None or qqq_bars is None:
        raise RuntimeError("SPY/QQQ bars are required for regime classification and were not available")
    sector_bars = {etf: bars[etf] for etf in SECTOR_ETFS.values() if etf in bars}
    assets = build_universe_assets(entries)
    bars_by_symbol = {entry.symbol: bars[entry.symbol] for entry in entries if entry.symbol in bars}
    return ScanInputs(
        assets=assets, bars_by_symbol=bars_by_symbol, spy_bars=spy_bars, qqq_bars=qqq_bars, sector_bars=sector_bars,
    )
