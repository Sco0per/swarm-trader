"""Non-executing Robinhood adapter placeholder pending verified MCP schemas."""

from __future__ import annotations

from .base import BrokerProvider, BrokerUnavailable


class RobinhoodMCPProvider(BrokerProvider):
    """Deliberately unavailable: no Robinhood MCP tools were exposed during implementation."""

    name = "robinhood-mcp-unavailable"
    available = False

    def _unavailable(self, *_args, **_kwargs):
        raise BrokerUnavailable("Robinhood MCP is not available. Verify real tool schemas and dedicated Agentic account identity before implementing execution.")

    get_account = _unavailable
    get_buying_power = _unavailable
    get_positions = _unavailable
    get_open_orders = _unavailable
    get_asset = _unavailable
    get_quote = _unavailable
    review_order = _unavailable
    place_order = _unavailable
    cancel_order = _unavailable
    replace_stop = _unavailable
    close_position = _unavailable
    get_order_status = _unavailable
    get_trade_history = _unavailable
