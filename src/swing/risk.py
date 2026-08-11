"""Deterministic final risk authority. No model output can bypass this module."""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

from .config import SwingSettings, load_symbol_blacklist
from .database import SwingDatabase
from .models import BrokerAsset, Decision, MarketRegime, PortfolioRiskState, Quote, RiskDecision, SwingCandidate, TradeProposal


LEVERAGED_OR_INVERSE_ETFS = {
    "TQQQ", "SQQQ", "SOXL", "SOXS", "UPRO", "SPXU", "SPXL", "SPXS", "TECL", "TECS",
    "FAS", "FAZ", "LABU", "LABD", "NAIL", "DRV", "CURE", "HIBL", "HIBS", "UDOW", "SDOW",
    "QLD", "QID", "SSO", "SDS", "UCO", "SCO", "BOIL", "KOLD", "UVXY", "SVXY",
}


class SwingRiskManager:
    """Approves quantity only after every independent safety rule passes."""

    def __init__(self, settings: SwingSettings, database: SwingDatabase):
        self.settings = settings
        self.database = database
        self.blacklist = load_symbol_blacklist(settings.do_not_trade_path)

    def reject(self, rule: str, reason: str, proposal: TradeProposal | None = None, intent_id: str | None = None) -> RiskDecision:
        self.database.record_violation(
            rule,
            reason,
            ticker=proposal.ticker if proposal else None,
            decision_id=proposal.decision_id if proposal else None,
            intent_id=intent_id,
            blocked=True,
        )
        return RiskDecision(approved=False, reason=reason, rule=rule)

    def validate_entry(
        self,
        proposal: TradeProposal,
        candidate: SwingCandidate,
        quote: Quote,
        portfolio: PortfolioRiskState,
        intent_id: str,
        asset: BrokerAsset | None = None,
    ) -> RiskDecision:
        settings = self.settings
        symbol = proposal.ticker.upper()
        now = datetime.now(timezone.utc)

        if proposal.decision != Decision.BUY:
            return self.reject("long_only", "Only BUY proposals are valid entry intents", proposal, intent_id)
        if proposal.ticker != candidate.ticker or quote.symbol.upper() != symbol:
            return self.reject("symbol_mismatch", "Proposal, candidate, and quote symbols do not match", proposal, intent_id)
        if asset is not None:
            if asset.symbol.upper() != symbol:
                return self.reject("asset_symbol_mismatch", "Broker asset identity does not match the proposal", proposal, intent_id)
            if asset.asset_class not in {"us_equity", "equity"}:
                return self.reject("unsupported_asset_class", f"Broker classifies {symbol} as {asset.asset_class}", proposal, intent_id)
            if not asset.tradable or asset.status != "active":
                return self.reject("asset_not_tradable", f"Broker reports {symbol} as {asset.status}/not tradable", proposal, intent_id)
            if asset.is_leveraged_or_inverse is True:
                return self.reject("banned_instrument", f"Broker classifies {symbol} as leveraged or inverse", proposal, intent_id)
        if portfolio.kill_switch or bool(self.database.get_state("kill_switch", False)):
            return self.reject("kill_switch", "Global kill switch is active", proposal, intent_id)
        if portfolio.reconciliation_halt_latched or bool(self.database.get_state("reconciliation_halt", False)):
            return self.reject(
                "reconciliation_halt",
                "New entries are halted until broker/database discrepancies are reconciled",
                proposal,
                intent_id,
            )
        if portfolio.loss_streak_halt_latched or bool(self.database.get_state("loss_streak_halt", False)):
            return self.reject(
                "consecutive_loss_halt",
                "The consecutive-loss halt is latched pending explicit human review",
                proposal,
                intent_id,
            )
        if settings.execution_mode == "live":
            if not settings.trading_enabled:
                return self.reject("trading_disabled", "TRADING_ENABLED is false", proposal, intent_id)
            if portfolio.account.is_paper:
                return self.reject("account_mode_mismatch", "Live mode cannot use a paper account", proposal, intent_id)
            if portfolio.account.is_margin_enabled:
                return self.reject("margin_forbidden", "Margin-enabled execution is forbidden for this experiment", proposal, intent_id)
            if portfolio.account.dedicated_agentic_account is not True:
                return self.reject("account_not_verified", "Dedicated agentic account identity was not verified", proposal, intent_id)
        elif settings.execution_mode == "paper" and not portfolio.account.is_paper:
            return self.reject("account_mode_mismatch", "Paper mode requires a verified paper account", proposal, intent_id)

        if self.database.intent_was_submitted(intent_id):
            return self.reject("duplicate_intent", f"Intent {intent_id} was already submitted", proposal, intent_id)
        if symbol in self.blacklist:
            return self.reject("long_term_blacklist", f"{symbol} is on the do-not-trade list", proposal, intent_id)
        if candidate.is_leveraged_or_inverse or symbol in LEVERAGED_OR_INVERSE_ETFS:
            return self.reject("banned_instrument", f"{symbol} is leveraged, inverse, or volatility-linked", proposal, intent_id)
        if candidate.is_halted:
            return self.reject("trading_halt", f"{symbol} is halted", proposal, intent_id)
        if candidate.price <= settings.minimum_price or quote.last <= settings.minimum_price:
            return self.reject("minimum_price", f"Price must be greater than ${settings.minimum_price:.2f}", proposal, intent_id)
        if candidate.average_daily_volume <= settings.minimum_average_volume:
            return self.reject("minimum_liquidity", "Average daily volume does not exceed 1,000,000 shares", proposal, intent_id)
        if quote.spread_pct is None:
            return self.reject("spread_unavailable", "A current bid/ask spread is required", proposal, intent_id)
        if quote.spread_pct > settings.maximum_spread_pct:
            return self.reject("spread_too_wide", f"Spread {quote.spread_pct:.3%} exceeds limit", proposal, intent_id)
        future_tolerance = 5.0
        if (quote.retrieved_at - now).total_seconds() > future_tolerance:
            return self.reject("future_quote", "Quote retrieval timestamp is in the future", proposal, intent_id)
        if (now - quote.retrieved_at).total_seconds() > settings.quote_freshness_seconds:
            return self.reject("stale_quote", "Quote retrieval timestamp is stale", proposal, intent_id)
        market_time = quote.market_timestamp or quote.retrieved_at
        if (market_time - now).total_seconds() > future_tolerance:
            return self.reject("future_market_data", "Quote market timestamp is in the future", proposal, intent_id)
        if (now - market_time).total_seconds() > settings.quote_freshness_seconds:
            return self.reject("stale_market_data", "Quote market timestamp is stale", proposal, intent_id)
        candidate_market_time = candidate.data.market_timestamp or candidate.data.retrieved_at
        if (candidate.data.retrieved_at - now).total_seconds() > future_tolerance or (candidate_market_time - now).total_seconds() > future_tolerance:
            return self.reject("future_candidate", "Candidate data timestamp is in the future", proposal, intent_id)
        if (now - candidate_market_time).total_seconds() > 86_400 * 4:
            return self.reject("stale_candidate", "Candidate analysis is older than four calendar days", proposal, intent_id)
        if (portfolio.account.retrieved_at - now).total_seconds() > future_tolerance:
            return self.reject("future_account", "Account snapshot timestamp is in the future", proposal, intent_id)
        if (now - portfolio.account.retrieved_at).total_seconds() > settings.quote_freshness_seconds:
            return self.reject("stale_account", "Account snapshot is stale", proposal, intent_id)
        if not candidate.is_etf:
            if candidate.earnings_trading_days is None:
                return self.reject("earnings_unknown", "Earnings proximity is unknown for an individual stock", proposal, intent_id)
            if candidate.earnings_trading_days <= settings.earnings_exclusion_trading_days:
                return self.reject("earnings_exclusion", "Earnings are expected within five trading days", proposal, intent_id)

        if proposal.setup_type != candidate.setup_type:
            return self.reject("setup_mismatch", "Proposal setup does not match deterministic candidate setup", proposal, intent_id)
        if proposal.candidate_score != candidate.score:
            return self.reject("score_mismatch", "Proposal score does not match deterministic candidate score", proposal, intent_id)
        if candidate.score < settings.buy_score_threshold:
            return self.reject("candidate_score", f"Candidate score {candidate.score:.1f} is below {settings.buy_score_threshold:.1f}", proposal, intent_id)
        if proposal.market_regime != candidate.market_regime:
            return self.reject("regime_mismatch", "Proposal regime does not match deterministic regime", proposal, intent_id)
        if candidate.market_regime in {MarketRegime.BEAR, MarketRegime.HIGH_VOLATILITY_RISK_OFF}:
            return self.reject("unsuitable_regime", f"New longs are disabled in {candidate.market_regime.value}", proposal, intent_id)

        if not (proposal.stop < quote.last < proposal.target):
            return self.reject("invalid_stop_target", "Live quote must remain between technical stop and target", proposal, intent_id)
        risk_per_share = quote.last - proposal.stop
        if risk_per_share <= 0:
            return self.reject("invalid_stop", "Technical stop must be below current entry quote", proposal, intent_id)
        current_rr = (proposal.target - quote.last) / risk_per_share
        if current_rr < settings.minimum_rr:
            if not (settings.exceptional_minimum_rr <= current_rr < settings.minimum_rr and proposal.exceptional_rr_evidence):
                return self.reject("minimum_rr", f"Current structural reward/risk {current_rr:.2f} is below {settings.minimum_rr:.2f}", proposal, intent_id)

        # Do not chase a quote that moved more than 0.25R above the analyzed entry.
        if quote.last > proposal.entry + 0.25 * (proposal.entry - proposal.stop):
            return self.reject("chasing_entry", "Current quote has moved more than 0.25R above the planned entry", proposal, intent_id)

        existing_symbols = {position.symbol.upper() for position in portfolio.positions if position.quantity != 0}
        if symbol in existing_symbols:
            return self.reject("no_adding", "Adding to an existing position is disabled; averaging down is forbidden", proposal, intent_id)
        if any(str(order.get("symbol", "")).upper() == symbol for order in portfolio.open_orders):
            return self.reject("existing_open_order", f"An open order already exists for {symbol}", proposal, intent_id)
        prior_loss = self.database.last_losing_trade(symbol)
        if prior_loss and prior_loss.get("exit_datetime"):
            exit_date = datetime.fromisoformat(prior_loss["exit_datetime"]).date()
            current_date = now.date()
            # Weekdays are a conservative approximation when an exchange calendar is unavailable.
            trading_days = sum(
                1 for offset in range(1, max(1, (current_date - exit_date).days + 1))
                if (exit_date + timedelta(days=offset)).weekday() < 5
            )
            if trading_days <= settings.cooldown_trading_days:
                return self.reject("loss_cooldown", f"{symbol} is in a post-loss cooldown", proposal, intent_id)
            required_answers = {"what_failed", "what_changed", "failure_resolved", "new_setup"}
            if not proposal.reentry_context or not required_answers.issubset(proposal.reentry_context) or not all(proposal.reentry_context[key].strip() for key in required_answers):
                return self.reject("reentry_review_missing", "Post-loss re-entry requires four explicit review answers", proposal, intent_id)
            if prior_loss.get("setup_type") == proposal.setup_type.value and proposal.reentry_context["new_setup"].strip().lower() in {"no", "false", "same"}:
                return self.reject("reentry_not_new_setup", "Re-entry is not a genuinely new setup", proposal, intent_id)
        if len(existing_symbols) >= settings.max_open_positions:
            return self.reject("max_positions", "Maximum of three simultaneous positions reached", proposal, intent_id)
        if portfolio.new_positions_today >= settings.max_new_positions_day:
            return self.reject("daily_entry_limit", "Maximum of one new position per day reached", proposal, intent_id)
        if portfolio.new_positions_this_week >= settings.max_new_positions_week:
            return self.reject("weekly_entry_limit", "Maximum of three new positions per week reached", proposal, intent_id)

        drawdown = max(portfolio.drawdown_pct, (portfolio.equity_high - portfolio.account.equity) / portfolio.equity_high if portfolio.equity_high else 0)
        if drawdown >= settings.halt_drawdown_pct or portfolio.drawdown_halt_latched or bool(self.database.get_state("drawdown_halt", False)):
            self.database.set_state("drawdown_halt", True, "risk_manager")
            return self.reject("drawdown_halt", f"New trading halted at {drawdown:.2%} drawdown; human review required", proposal, intent_id)
        if portfolio.consecutive_losses >= settings.consecutive_loss_halt:
            self.database.set_state("loss_streak_halt", True, "risk_manager")
            return self.reject("consecutive_loss_halt", "Three consecutive completed losses require review", proposal, intent_id)

        risk_pct = settings.reduced_risk_pct if drawdown >= settings.reduce_risk_drawdown_pct else settings.normal_risk_pct
        requested = proposal.requested_risk_pct or risk_pct
        a_plus_eligible = (
            candidate.score >= settings.a_plus_score_threshold
            and proposal.confidence_score >= 90
            and candidate.market_regime in {MarketRegime.STRONG_BULL, MarketRegime.BULL}
        )
        allowed_request = settings.a_plus_risk_pct if a_plus_eligible and drawdown < settings.reduce_risk_drawdown_pct else risk_pct
        applied_risk_pct = min(requested, allowed_request, settings.absolute_max_risk_pct)
        allowed_dollar_risk = portfolio.account.equity * applied_risk_pct
        quantity = math.floor(allowed_dollar_risk / risk_per_share)

        available_cash = min(portfolio.account.cash, portfolio.account.buying_power)
        max_exposure = min(available_cash, portfolio.account.equity * settings.max_position_exposure_pct)
        quantity = min(quantity, math.floor(max_exposure / quote.last))
        if quantity < 1:
            return self.reject("position_too_small", "One share does not fit both technical risk and cash exposure limits", proposal, intent_id)

        planned_risk = quantity * risk_per_share
        account_risk_pct = planned_risk / portfolio.account.equity
        if account_risk_pct > settings.absolute_max_risk_pct + 1e-12:
            return self.reject("absolute_risk_max", "Planned trade risk exceeds the 1% hard maximum", proposal, intent_id)
        if portfolio.planned_open_risk + planned_risk > portfolio.account.equity * settings.max_combined_open_risk_pct + 1e-9:
            return self.reject("max_combined_open_risk", "Combined planned open risk would exceed 2% of equity", proposal, intent_id)

        return RiskDecision(
            approved=True,
            reason="All deterministic swing risk rules passed",
            quantity=quantity,
            allowed_dollar_risk=round(allowed_dollar_risk, 2),
            planned_dollar_risk=round(planned_risk, 2),
            planned_account_risk_pct=account_risk_pct,
            planned_rr=current_rr,
            applied_risk_pct=applied_risk_pct,
        )

    def validate_stop_change(self, ticker: str, current_stop: float, proposed_stop: float) -> RiskDecision:
        """For a long position the stop may stay unchanged or rise, never fall."""
        if current_stop <= 0 or proposed_stop <= 0:
            return self.reject("invalid_stop", f"Invalid stop for {ticker}")
        if proposed_stop < current_stop:
            return self.reject("stop_widening", f"Stop cannot widen from {current_stop:.2f} to {proposed_stop:.2f}")
        return RiskDecision(approved=True, reason="Stop is unchanged or tighter")
