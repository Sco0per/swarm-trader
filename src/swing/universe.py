"""Versioned, configurable broad U.S. liquid-equity universe."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path

UNIVERSE_VERSION = "us-liquid-2026-08-11-v1"
UNIVERSE_AS_OF = "2026-08-11"
UNIVERSE_SOURCES = (
    "https://www.spglobal.com/spdji/en/indices/equity/sp-500/",
    "https://www.nasdaq.com/solutions/global-indexes/nasdaq-100/companies",
)
DEFAULT_UNIVERSE_PATH = Path(__file__).resolve().parents[2] / "config" / "universe" / "us_liquid_2026-08-11.csv"


@dataclass(frozen=True)
class UniverseEntry:
    symbol: str
    sector: str
    is_etf: bool = False
    source_membership: str = "SP500_OR_NASDAQ100"


def load_universe(path: Path) -> list[UniverseEntry]:
    """Load a date-stamped static constituent selection; malformed files fail closed."""
    if not path.is_file():
        raise RuntimeError(f"Required universe file is not loadable: {path}")
    try:
        with path.open(newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
    except (OSError, csv.Error) as exc:
        raise RuntimeError(f"Required universe file is not loadable: {path}") from exc
    required = {"symbol", "sector", "security_type", "source_membership"}
    if not rows or not required.issubset(rows[0]):
        raise RuntimeError(f"Universe file is malformed: {path}")
    entries: list[UniverseEntry] = []
    seen: set[str] = set()
    for row in rows:
        symbol = row["symbol"].strip().upper()
        sector = row["sector"].strip()
        security_type = row["security_type"].strip().lower()
        membership = row["source_membership"].strip()
        if not symbol or symbol in seen or not symbol.replace(".", "").replace("-", "").isalnum() or not sector or security_type not in {"common_stock", "etf"} or not membership:
            raise RuntimeError(f"Universe file contains an invalid or duplicate row for {symbol!r}")
        seen.add(symbol)
        entries.append(UniverseEntry(symbol, sector, security_type == "etf", membership))
    return entries


def build_universe(path: Path = DEFAULT_UNIVERSE_PATH) -> list[UniverseEntry]:
    return load_universe(path)


UNIVERSE: list[UniverseEntry] = build_universe()
UNIVERSE_SYMBOLS: list[str] = [entry.symbol for entry in UNIVERSE]
SECTOR_BY_SYMBOL: dict[str, str] = {entry.symbol: entry.sector for entry in UNIVERSE}
