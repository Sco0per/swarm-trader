"""
Account routing for the swing-only Alpaca setup.

One account:
  - "swing" (Primary): ALPACA_API_KEY / ALPACA_API_SECRET — multi-day holds

Day-account routing (ALPACA_DAY_*) was removed: this project is swing-only and
`get_account_for_mode` refuses any mode other than "swing". Legacy callers pass
an optional `mode` parameter, which must be "swing" or None.
"""

import os
from dataclasses import dataclass


@dataclass
class AlpacaAccount:
    """Credentials + metadata for a single Alpaca account."""
    name: str
    account_id: str
    api_key: str
    api_secret: str
    base_url: str = "https://paper-api.alpaca.markets/v2"

    @property
    def headers(self) -> dict:
        return {
            "APCA-API-KEY-ID": self.api_key,
            "APCA-API-SECRET-KEY": self.api_secret,
            "Content-Type": "application/json",
        }


# Account registry — loaded from env vars
_ACCOUNTS: dict[str, AlpacaAccount] = {}


def _load_accounts() -> None:
    """Load account credentials from environment. Called once on import."""
    global _ACCOUNTS

    # Swing account (primary — ALPACA_API_KEY / ALPACA_API_SECRET)
    swing_key = os.environ.get("ALPACA_API_KEY", "")
    swing_secret = os.environ.get("ALPACA_API_SECRET", "")
    if swing_key and swing_secret:
        _ACCOUNTS["swing"] = AlpacaAccount(
            name="Swing",
            account_id=os.environ.get("ALPACA_ACCOUNT_ID", "unknown"),
            api_key=swing_key,
            api_secret=swing_secret,
        )


def get_account_for_mode(mode: str = None) -> AlpacaAccount:
    """
    Get the Alpaca account for the given trading mode.

    Args:
        mode: must be "swing" (or None, which resolves to "swing").

    Returns:
        AlpacaAccount with credentials for the swing account.

    Raises:
        ValueError if a non-swing mode is requested or the account is not configured.
    """
    if not _ACCOUNTS:
        _load_accounts()

    if mode is None:
        from src.config import resolve_mode
        mode = resolve_mode()
        if mode == "auto":
            mode = "swing"  # safe default

    mode = mode.lower()
    if mode != "swing":
        raise ValueError("Production account routing is swing-only")

    if mode in _ACCOUNTS:
        return _ACCOUNTS[mode]

    raise ValueError(
        f"No Alpaca account configured for mode '{mode}'. "
        "Check ALPACA_API_KEY / ALPACA_SWING_API_KEY in .env"
    )


# Auto-load on import
_load_accounts()
