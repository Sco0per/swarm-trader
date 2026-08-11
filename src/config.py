"""Compatibility configuration for the swing-only production posture.

New code uses :mod:`src.swing.config`. The names here keep legacy research and
monitoring imports working while making day/auto selection impossible.
"""

from __future__ import annotations

import os

TRADING_STYLE = "swing"
EXECUTION_MODE = os.environ.get("EXECUTION_MODE", "paper").lower()
TRADING_ENABLED = os.environ.get("TRADING_ENABLED", "false").lower() in {"1", "true", "yes", "on"}
STRATEGY_VERSION = os.environ.get("STRATEGY_VERSION", "SWING_V1.0")

SWING_UNIVERSE = {
    "large_cap_liquid": {
        "label": "Liquid U.S. Stocks",
        "tickers": ["AAPL", "AMZN", "AVGO", "GOOGL", "JPM", "META", "MSFT", "NVDA", "UNH", "V"],
        "max_sector_pct": 0.35,
        "max_per_stock_pct": 0.35,
        "target_pct": 0.0,
    },
    "ordinary_etfs": {
        "label": "Unleveraged ETFs",
        "tickers": ["SPY", "QQQ", "IWM", "DIA", "XLK", "XLF", "XLE", "XLV"],
        "max_sector_pct": 0.35,
        "max_per_stock_pct": 0.35,
        "target_pct": 0.0,
    },
}

MODES = {
    "swing": {
        "label": "Selective Long-Only Swing Trading (2-10, up to 20 trading days)",
        "universe": SWING_UNIVERSE,
        "risk": {
            # Legacy scripts read these fields, but new entry sizing is exclusively
            # technical-stop based in src.swing.risk.
            "max_position_pct": 0.35,
            "max_sector_pct": 0.35,
            "stop_loss_pct": 0.0,
            "trailing_stop_pct": 0.0,
            "daily_loss_limit": 0.08,
            "weekly_loss_limit": 0.08,
            "no_buy_if_down_pct": 0.05,
            "max_trades_per_day": 1,
            "max_open_positions": 3,
            "min_cash_pct": 0.0,
            "max_tactical_pct": 0.0,
            "flatten_eod": False,
            "allow_leveraged_etfs": False,
            "normal_risk_per_trade": 0.005,
            "a_plus_risk_per_trade": 0.0075,
            "absolute_max_risk_per_trade": 0.01,
            "max_combined_open_risk": 0.02,
            "max_new_positions_week": 3,
        },
    }
}


def resolve_mode(cli_mode: str | None = None) -> str:
    requested = (cli_mode or os.environ.get("TRADING_STYLE") or os.environ.get("TRADING_MODE") or "swing").lower()
    if requested != "swing":
        raise ValueError("Production day/auto trading is disabled; TRADING_STYLE must be 'swing'")
    return "swing"


def set_mode(
    mode: str,
    reason: str | None = None,
    updated_by: str = "agent",
    override: bool = False,
    override_hours: float | None = None,
) -> str:
    del reason, updated_by, override, override_hours
    if mode.lower() != "swing":
        raise ValueError("Mode switching is disabled; this project is swing-only")
    return "swing"


def get_mode_config(mode: str | None = None) -> dict:
    resolved = resolve_mode(mode)
    return MODES[resolved]


# Backward-compatible research aliases. Day-mode reference agents can import,
# but production configuration exposes no day universe or day mode.
UNIVERSE_V2 = SWING_UNIVERSE
ALL_V2_TICKERS = [ticker for group in SWING_UNIVERSE.values() for ticker in group["tickers"]]
DAY_TRADE_UNIVERSE: dict = {}
ALL_DAY_TRADE_TICKERS: list[str] = []
UNIVERSE = SWING_UNIVERSE
ALL_UNIVERSE_TICKERS = ALL_V2_TICKERS
UNIVERSE_SIMPLE = {key: group["tickers"] for key, group in SWING_UNIVERSE.items()}

MAX_POSITION_PCT = 0.35
MAX_SECTOR_PCT = 0.35
STOP_LOSS_PCT = 0.0
TRAILING_STOP_PCT = 0.0
DAILY_LOSS_CIRCUIT_BREAKER = 0.08
WEEKLY_LOSS_CIRCUIT_BREAKER = 0.08
MAX_TRADES_PER_DAY = 1
MAX_OPEN_POSITIONS = 3
MIN_CASH_PCT = 0.0
MAX_MOONSHOT_PCT = 0.0
NO_BUY_IF_DOWN_PCT = 0.05
MAX_RISK_PER_TRADE = 0.005
MAX_PORTFOLIO_HEAT = 0.02
MAX_POSITION_SIZE = 0.35
DEFAULT_STOP_PCT = 0.0
DEFAULT_TARGET_MULTIPLIER = 2.0
FLATTEN_BY = "disabled"
