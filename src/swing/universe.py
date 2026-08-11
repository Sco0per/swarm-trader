"""Deterministic, curated liquid U.S. stock/ETF universe.

No paid screener API is used (matches the repo's "zero paid data APIs" stance).
Every entry is a normal U.S. common stock or a non-leveraged, non-inverse ETF —
crypto, futures, options, and penny stocks are excluded by construction, not by
runtime classification. Leveraged/inverse ETFs are additionally cross-checked
against ``LEVERAGED_OR_INVERSE_ETFS`` in ``src.swing.risk`` at scan and risk time.

Sector labels match the eleven GICS sector names used by ``SECTOR_ETFS`` in
``src.swing.market`` so sector relative-strength scoring works out of the box.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class UniverseEntry:
    symbol: str
    sector: str
    is_etf: bool = False


# Large/mid-cap, liquid U.S. common stocks, grouped by GICS sector.
_STOCKS: dict[str, list[str]] = {
    "Technology": [
        "AAPL", "MSFT", "NVDA", "AVGO", "ORCL", "CRM", "ADBE", "AMD", "CSCO", "ACN",
        "IBM", "TXN", "QCOM", "INTU", "NOW", "AMAT", "MU", "PANW", "SNPS", "CDNS",
        "LRCX", "KLAC", "ANET", "FTNT", "ADI", "APH", "MSI", "ROP", "CRWD", "DELL",
    ],
    "Communication Services": [
        "GOOGL", "META", "NFLX", "DIS", "CMCSA", "TMUS", "VZ", "T", "EA", "TTWO",
        "WBD", "OMC", "LYV", "PARA",
    ],
    "Consumer Discretionary": [
        "AMZN", "TSLA", "HD", "MCD", "NKE", "LOW", "SBUX", "TJX", "BKNG", "CMG",
        "MAR", "GM", "F", "ORLY", "AZO", "YUM", "ROST", "DHI", "LEN", "HLT",
    ],
    "Consumer Staples": [
        "PG", "KO", "PEP", "COST", "WMT", "PM", "MDLZ", "CL", "MO", "TGT",
        "KMB", "GIS", "STZ", "SYY", "KDP",
    ],
    "Financials": [
        "JPM", "V", "MA", "BAC", "WFC", "GS", "MS", "SPGI", "AXP", "SCHW",
        "C", "BLK", "PGR", "CB", "MMC", "ICE", "USB", "PNC", "TFC", "AON",
    ],
    "Health Care": [
        "UNH", "LLY", "JNJ", "ABBV", "MRK", "TMO", "ABT", "PFE", "DHR", "AMGN",
        "ISRG", "SYK", "BSX", "VRTX", "REGN", "GILD", "ELV", "MDT", "CI", "ZTS",
    ],
    "Industrials": [
        "GE", "CAT", "RTX", "HON", "UNP", "BA", "DE", "LMT", "ADP", "UPS",
        "ETN", "NOC", "GD", "ITW", "CSX", "EMR", "WM", "NSC", "PH", "FDX",
    ],
    "Energy": [
        "XOM", "CVX", "COP", "WMB", "EOG", "SLB", "MPC", "PSX", "OXY", "VLO",
    ],
    "Materials": [
        "LIN", "SHW", "APD", "ECL", "FCX", "NEM", "NUE", "DOW", "PPG", "VMC",
    ],
    "Real Estate": [
        "PLD", "AMT", "EQIX", "WELL", "SPG", "PSA", "O", "DLR", "CCI", "VTR",
    ],
    "Utilities": [
        "NEE", "SO", "DUK", "AEP", "SRE", "D", "EXC", "XEL", "ED", "WEC",
    ],
}

# Broad-market / index ETFs — non-leveraged, non-inverse only.
_ETFS: list[str] = [
    "SPY", "QQQ", "IWM", "DIA", "VTI", "VOO",
    "XLK", "XLC", "XLY", "XLP", "XLF", "XLV", "XLI", "XLE", "XLB", "XLRE", "XLU",
    "GLD", "SLV", "TLT", "IEF", "HYG", "LQD",
    "EFA", "EEM", "VEA", "VWO",
]


def build_universe() -> list[UniverseEntry]:
    """Return the curated universe as (symbol, sector, is_etf) entries."""
    entries: list[UniverseEntry] = []
    seen: set[str] = set()
    for sector, symbols in _STOCKS.items():
        for symbol in symbols:
            if symbol in seen:
                continue
            seen.add(symbol)
            entries.append(UniverseEntry(symbol=symbol, sector=sector, is_etf=False))
    for symbol in _ETFS:
        if symbol in seen:
            continue
        seen.add(symbol)
        entries.append(UniverseEntry(symbol=symbol, sector="Unknown", is_etf=True))
    return entries


UNIVERSE: list[UniverseEntry] = build_universe()
UNIVERSE_SYMBOLS: list[str] = [entry.symbol for entry in UNIVERSE]
SECTOR_BY_SYMBOL: dict[str, str] = {entry.symbol: entry.sector for entry in UNIVERSE}
