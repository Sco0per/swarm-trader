"""Alpaca-backed data feed adapter for the deterministic swing scanner.

Bars come from Alpaca's market data API (already a configured broker
dependency, see ``src/swing/brokers/alpaca.py``) rather than yfinance:
Yahoo Finance actively blocks requests from cloud/datacenter IP ranges
(observed as 100% connection resets from this project's cloud routine
environment), which yfinance has no way around. Alpaca is a legitimate,
key-authenticated API with no such anti-scraping wall.

Earnings-date lookup still uses yfinance (Alpaca's data API has no earnings
calendar) via the lazy ``_yf()`` import below, matching the original
lazy-import + disk-cache pattern used by ``src.tools.api_free`` so this
module has no hard yfinance/network dependency at import time; tests
monkeypatch ``_yf`` to run fully offline. If earnings lookups also turn out
to be blocked from the cloud environment, they already degrade safely: a
failed lookup returns ``None``, and ``market.py`` treats an unknown earnings
date as a scoring exclusion (not a crash) -- see the ``event_fraction``
calculation there.

Per-symbol lookups run concurrently under one shared
``EARNINGS_LOOKUP_TIMEOUT_SECONDS`` deadline (see
``fetch_earnings_trading_days_batch``) rather than sequentially each paying
their own timeout -- with ~200 stock symbols in the universe, sequential
lookups turned an 8-second worst case per symbol into a ~20-minute scan when
Yahoo was blocking (or hanging on) most of them. Results are also cached in
the hosted Turso database when one is passed in, so a fresh cloud-routine
sandbox later the same day can skip the network call entirely instead of
re-paying it every run -- the local disk cache below only helps within a
single process.

Known limitation (documented, not silently papered over): there is no free,
reliable halt-status feed, so ``UniverseAsset.is_halted`` always defaults to
``False`` here. This matches the gap already called out in
``docs/REMEDIATION_STATUS.md``.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING

import pandas as pd
import requests

from .market import SECTOR_ETFS, UniverseAsset
from .universe import UNIVERSE, UniverseEntry

if TYPE_CHECKING:
    from .database import SwingDatabase

CACHE_DIR = Path(__file__).resolve().parents[2] / ".cache" / "swing_data_feed"
BAR_CACHE_TTL_SECONDS = 6 * 3600
EARNINGS_CACHE_TTL_SECONDS = 24 * 3600
REQUIRED_COLUMNS = ("open", "high", "low", "close", "volume")

ALPACA_DATA_BASE = "https://data.alpaca.markets/v2"
ALPACA_BATCH_SIZE = 50


def _yf():
    import yfinance as yf

    return yf


def _alpaca_headers() -> dict[str, str]:
    api_key = os.getenv("ALPACA_API_KEY", "")
    api_secret = os.getenv("ALPACA_API_SECRET", "")
    if not api_key or not api_secret:
        raise RuntimeError("ALPACA_API_KEY/ALPACA_API_SECRET must be set to fetch market data from Alpaca")
    return {"APCA-API-KEY-ID": api_key, "APCA-API-SECRET-KEY": api_secret}


def _alpaca_get(path: str, params: dict) -> dict:
    """Thin, mockable seam over the Alpaca data API -- tests monkeypatch this."""
    response = requests.get(f"{ALPACA_DATA_BASE}{path}", headers=_alpaca_headers(), params=params, timeout=30)
    if not response.ok:
        raise RuntimeError(f"Alpaca data API {response.status_code} for {path}: {response.text[:500]}")
    return response.json()


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


def _fetch_alpaca_bars_batch(symbols: list[str], *, start: str, end: str) -> dict[str, pd.DataFrame]:
    """One batched Alpaca call (with pagination) covering up to ALPACA_BATCH_SIZE symbols."""
    rows_by_symbol: dict[str, list[dict]] = {symbol: [] for symbol in symbols}
    page_token: str | None = None
    while True:
        params = {
            "symbols": ",".join(symbols), "timeframe": "1Day", "start": start, "end": end,
            "limit": 10000, "adjustment": "all", "feed": "iex",
        }
        if page_token:
            params["page_token"] = page_token
        payload = _alpaca_get("/stocks/bars", params)
        for symbol, rows in (payload.get("bars") or {}).items():
            rows_by_symbol.setdefault(symbol, []).extend(rows)
        page_token = payload.get("next_page_token")
        if not page_token:
            break

    result: dict[str, pd.DataFrame] = {}
    for symbol, rows in rows_by_symbol.items():
        if not rows:
            continue
        frame = pd.DataFrame(rows)
        if frame.empty:
            continue
        frame = frame.rename(columns={"t": "date", "o": "open", "h": "high", "l": "low", "c": "close", "v": "volume"})
        if not {"date", *REQUIRED_COLUMNS}.issubset(frame.columns):
            continue
        frame["date"] = pd.to_datetime(frame["date"], utc=True)
        frame = frame.set_index("date").sort_index()
        frame = frame[list(REQUIRED_COLUMNS)].dropna().astype(float)
        if frame.empty:
            continue
        result[symbol] = frame
    return result


def fetch_bars(symbols: list[str], *, lookback_days: int = 320) -> dict[str, pd.DataFrame]:
    """Fetch daily OHLCV bars per symbol from Alpaca, disk-cached for BAR_CACHE_TTL_SECONDS."""
    result: dict[str, pd.DataFrame] = {}
    to_fetch: list[str] = []
    for symbol in symbols:
        cache_key = f"{symbol}:{lookback_days}"
        cached = _cache_get("bars", cache_key, BAR_CACHE_TTL_SECONDS)
        if cached:
            try:
                result[symbol] = _records_to_frame(cached)
                continue
            except Exception:
                pass
        to_fetch.append(symbol)

    if not to_fetch:
        return result

    start = (datetime.now(timezone.utc) - timedelta(days=lookback_days + 30)).strftime("%Y-%m-%d")
    end = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    for i in range(0, len(to_fetch), ALPACA_BATCH_SIZE):
        batch = to_fetch[i : i + ALPACA_BATCH_SIZE]
        for symbol, frame in _fetch_alpaca_bars_batch(batch, start=start, end=end).items():
            result[symbol] = frame
            _cache_set("bars", f"{symbol}:{lookback_days}", _bars_to_records(frame))

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


EARNINGS_LOOKUP_TIMEOUT_SECONDS = 8


def _fetch_earnings_calendar(symbol: str, yf_module, outcomes: dict) -> None:
    try:
        outcomes[symbol] = {"calendar": yf_module.Ticker(symbol).get_earnings_dates(limit=4)}
    except Exception:
        pass


def _trading_days_from_calendar(calendar, today: date) -> int | None:
    index = getattr(calendar, "index", None)
    if calendar is None or index is None or len(index) == 0:
        return None
    upcoming = [
        (idx.date() if hasattr(idx, "date") else idx)
        for idx in index
        if (idx.date() if hasattr(idx, "date") else idx) >= today
    ]
    if not upcoming:
        return None
    return _approx_trading_days_until(min(upcoming), today)


def fetch_earnings_trading_days_batch(symbols: list[str], database: "SwingDatabase | None" = None) -> dict[str, int | None]:
    """Resolve trading-days-to-next-earnings for many symbols within one shared timeout window.

    Checked cheapest-first: the hosted Turso cache (survives across routine
    runs/days), the local disk cache (survives within this process only),
    then yfinance. Remaining symbols are looked up concurrently -- one daemon
    thread per symbol, all started together and joined against a single
    shared deadline -- so the whole batch costs at most
    EARNINGS_LOOKUP_TIMEOUT_SECONDS once, not multiplied by len(symbols).
    Threads still running past the deadline are abandoned (daemonized, same
    as the previous per-symbol design) rather than awaited.
    """
    results: dict[str, int | None] = {}
    remaining = list(dict.fromkeys(symbols))

    if database is not None and remaining:
        try:
            hosted = database.get_earnings_cache_many(remaining, EARNINGS_CACHE_TTL_SECONDS)
        except Exception:
            hosted = {}
        results.update(hosted)
        remaining = [symbol for symbol in remaining if symbol not in results]

    still_remaining: list[str] = []
    for symbol in remaining:
        cached = _cache_get("earnings", symbol, EARNINGS_CACHE_TTL_SECONDS)
        if cached is not None:
            results[symbol] = cached.get("trading_days")
        else:
            still_remaining.append(symbol)

    if still_remaining:
        yf = _yf()
        outcomes: dict[str, dict] = {}
        threads = [
            threading.Thread(target=_fetch_earnings_calendar, args=(symbol, yf, outcomes), daemon=True)
            for symbol in still_remaining
        ]
        for thread in threads:
            thread.start()
        deadline = time.monotonic() + EARNINGS_LOOKUP_TIMEOUT_SECONDS
        for thread in threads:
            thread.join(timeout=max(0.0, deadline - time.monotonic()))

        today = datetime.now(timezone.utc).date()
        freshly_resolved: list[tuple[str, int | None]] = []
        for symbol in still_remaining:
            outcome = outcomes.get(symbol)
            trading_days = _trading_days_from_calendar(outcome["calendar"], today) if outcome else None
            results[symbol] = trading_days
            freshly_resolved.append((symbol, trading_days))
            _cache_set("earnings", symbol, {"trading_days": trading_days})

        if database is not None:
            try:
                database.set_earnings_cache_many(freshly_resolved)
            except Exception:
                pass

    return results


def fetch_earnings_trading_days(symbol: str, database: "SwingDatabase | None" = None) -> int | None:
    """Single-symbol convenience wrapper around fetch_earnings_trading_days_batch."""
    return fetch_earnings_trading_days_batch([symbol], database=database).get(symbol)


def build_universe_assets(
    entries: list[UniverseEntry] | None = None, database: "SwingDatabase | None" = None,
) -> list[UniverseAsset]:
    entries = entries if entries is not None else UNIVERSE
    stock_symbols = [entry.symbol for entry in entries if not entry.is_etf]
    earnings_by_symbol = fetch_earnings_trading_days_batch(stock_symbols, database=database) if stock_symbols else {}
    assets: list[UniverseAsset] = []
    for entry in entries:
        assets.append(UniverseAsset(
            symbol=entry.symbol,
            sector=entry.sector,
            is_etf=entry.is_etf,
            is_tradable=True,
            is_halted=False,
            is_leveraged_or_inverse=False,
            earnings_trading_days=None if entry.is_etf else earnings_by_symbol.get(entry.symbol),
        ))
    return assets


@dataclass
class ScanInputs:
    assets: list[UniverseAsset]
    bars_by_symbol: dict[str, pd.DataFrame]
    spy_bars: pd.DataFrame
    qqq_bars: pd.DataFrame
    sector_bars: dict[str, pd.DataFrame]


def load_scan_inputs(
    *, entries: list[UniverseEntry] | None = None, lookback_days: int = 320, database: "SwingDatabase | None" = None,
) -> ScanInputs:
    """Fetch everything DeterministicSwingScanner.scan() needs, in one call."""
    entries = entries if entries is not None else UNIVERSE
    symbols = sorted({entry.symbol for entry in entries} | {"SPY", "QQQ"} | set(SECTOR_ETFS.values()))
    bars = fetch_bars(symbols, lookback_days=lookback_days)
    spy_bars = bars.get("SPY")
    qqq_bars = bars.get("QQQ")
    if spy_bars is None or qqq_bars is None:
        raise RuntimeError("SPY/QQQ bars are required for regime classification and were not available")
    sector_bars = {etf: bars[etf] for etf in SECTOR_ETFS.values() if etf in bars}
    assets = build_universe_assets(entries, database=database)
    bars_by_symbol = {entry.symbol: bars[entry.symbol] for entry in entries if entry.symbol in bars}
    return ScanInputs(
        assets=assets, bars_by_symbol=bars_by_symbol, spy_bars=spy_bars, qqq_bars=qqq_bars, sector_bars=sector_bars,
    )
