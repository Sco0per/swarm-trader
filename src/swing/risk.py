"""Deterministic final risk authority. No model output can bypass this module."""

from __future__ import annotations

import csv
import math
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from .config import load_symbol_blacklist, SwingSettings
from .database import SwingDatabase
from .models import (
    BrokerAsset,
    Decision,
    MarketRegime,
    PortfolioRiskState,
    Quote,
    RiskDecision,
    SwingCandidate,
    TradeProposal,
)

LEVERAGED_OR_INVERSE_ETFS = {
    "TQQQ",
    "SQQQ",
    "SOXL",
    "SOXS",
    "UPRO",
    "SPXU",
    "SPXL",
    "SPXS",
    "TECL",
    "TECS",
    "FAS",
    "FAZ",
    "LABU",
    "LABD",
    "NAIL",
    "DRV",
    "CURE",
    "HIBL",
    "HIBS",
    "UDOW",
    "SDOW",
    "QLD",
    "QID",
    "SSO",
    "SDS",
    "UCO",
    "SCO",
    "BOIL",
    "KOLD",
    "UVXY",
    "SVXY",
}

EXPOSURE_CLUSTERS = (
    "Semiconductors",
    "Mega-cap technology",
    "High-beta growth",
    "Financials",
    "Healthcare",
    "Defensives",
    "Industrials",
    "Consumer",
    "Energy",
)

# Deliberately maintained and explicit. Unknown symbols do not inherit a sector default.
TICKER_CLUSTERS = {
    **{symbol: "Semiconductors" for symbol in ("NVDA", "AMD", "AVGO", "TSM", "SMCI", "INTC", "QCOM", "MU", "ARM", "MRVL")},
    **{symbol: "Mega-cap technology" for symbol in ("AAPL", "MSFT", "GOOG", "GOOGL", "META", "ORCL", "CRM", "ADBE")},
    **{symbol: "High-beta growth" for symbol in ("TSLA", "PLTR", "SNOW", "NET", "DDOG", "CRWD", "SHOP", "XYZ", "AAA", "BBB", "ABC")},
    **{symbol: "Financials" for symbol in ("JPM", "BAC", "WFC", "C", "GS", "MS", "SCHW", "AXP", "V", "MA")},
    **{symbol: "Healthcare" for symbol in ("UNH", "LLY", "JNJ", "MRK", "ABBV", "PFE", "TMO", "ABT", "AMGN", "ISRG")},
    **{symbol: "Defensives" for symbol in ("PG", "KO", "PEP", "WMT", "COST", "CL", "KMB", "NEE", "DUK", "SO")},
    **{symbol: "Industrials" for symbol in ("GE", "CAT", "HON", "UNP", "DE", "RTX", "LMT", "ETN", "UPS", "BA")},
    **{symbol: "Consumer" for symbol in ("AMZN", "HD", "LOW", "NKE", "SBUX", "MCD", "TGT", "BKNG", "TJX", "CMG")},
    **{symbol: "Energy" for symbol in ("XOM", "CVX", "COP", "SLB", "EOG", "OXY", "MPC", "PSX", "VLO", "KMI")},
    **{symbol: "High-beta growth" for symbol in ("SPY", "QQQ", "IWM", "VTI", "VOO", "XLK")},
    "DIA": "Industrials",
    "XLC": "Consumer",
    "XLY": "Consumer",
    "XLP": "Defensives",
    "XLF": "Financials",
    "XLV": "Healthcare",
    "XLI": "Industrials",
    "XLE": "Energy",
    "XLB": "Industrials",
    "XLRE": "Defensives",
    "XLU": "Defensives",
}

SECTOR_CLUSTERS = {
    "Technology": "High-beta growth",
    "Communication Services": "Consumer",
    "Consumer Discretionary": "Consumer",
    "Consumer Staples": "Defensives",
    "Energy": "Energy",
    "Financials": "Financials",
    "Health Care": "Healthcare",
    "Industrials": "Industrials",
    "Materials": "Industrials",
    "Real Estate": "Defensives",
    "Utilities": "Defensives",
}


def load_exposure_clusters(universe_path) -> dict[str, str]:
    """Build an explicit ticker map from the versioned universe, then apply correlation overrides."""
    try:
        with open(universe_path, newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
    except OSError as exc:
        raise RuntimeError(f"Required exposure-cluster source is not loadable: {universe_path}") from exc
    mapping: dict[str, str] = {}
    for row in rows:
        symbol = str(row.get("symbol") or "").strip().upper()
        sector = str(row.get("sector") or "").strip()
        cluster = TICKER_CLUSTERS.get(symbol) or SECTOR_CLUSTERS.get(sector)
        if symbol and cluster:
            mapping[symbol] = cluster
    mapping.update(TICKER_CLUSTERS)
    return mapping


REASON_CODES = {
    "earnings_exclusion": "REJECT_EARNINGS_WINDOW",
    "earnings_unknown": "REJECT_EARNINGS_WINDOW",
    "event_data_invalid": "REJECT_EARNINGS_WINDOW",
    "minimum_rr": "REJECT_LOW_RR",
    "max_combined_open_risk": "REJECT_OPEN_RISK",
    "cluster_risk": "REJECT_CLUSTER_RISK",
    "sector_risk": "REJECT_SECTOR_RISK",
    "stale_quote": "REJECT_STALE_DATA",
    "stale_market_data": "REJECT_STALE_DATA",
    "stale_candidate": "REJECT_STALE_DATA",
    "stale_account": "REJECT_STALE_DATA",
    "stale_asset_state": "REJECT_STALE_DATA",
    "stale_event_data": "REJECT_STALE_DATA",
    "stale_earnings_data": "REJECT_STALE_DATA",
    "incomplete_daily_bar": "REJECT_STALE_DATA",
    "market_data_missing": "REJECT_STALE_DATA",
    "spread_unavailable": "REJECT_WIDE_SPREAD",
    "spread_too_wide": "REJECT_WIDE_SPREAD",
    "invalid_stop": "REJECT_SETUP_INVALID",
    "stop_too_tight": "REJECT_SETUP_INVALID",
    "stop_too_wide": "REJECT_SETUP_INVALID",
    "setup_mismatch": "REJECT_SETUP_INVALID",
    "chasing_entry": "REJECT_ENTRY_CHASE",
    "drawdown_halt": "REJECT_DRAWDOWN_HALT",
    "weekly_drawdown_halt": "REJECT_DRAWDOWN_HALT",
    "consecutive_loss_halt": "REJECT_LOSS_STREAK",
    "kill_switch": "REJECT_KILL_SWITCH",
    "broker_mismatch": "REJECT_BROKER_MISMATCH",
    "position_too_small": "REJECT_QTY_ZERO",
    "insufficient_cash": "REJECT_INSUFFICIENT_CASH",
    "max_positions": "REJECT_MAX_POSITIONS",
    "daily_entry_limit": "REJECT_DAILY_ENTRY_LIMIT",
    "no_adding": "REJECT_AVERAGING_DOWN",
    "no_cluster_mapping": "REJECT_NO_CLUSTER_MAPPING",
    "liquidity_limit": "REJECT_LIQUIDITY",
}


class SwingRiskManager:
    """Compute the authoritative stop and approve a whole-share quantity only after every gate passes."""

    def __init__(self, settings: SwingSettings, database: SwingDatabase):
        self.settings = settings
        self.database = database
        self.blacklist = load_symbol_blacklist(settings.do_not_trade_path)
        self.clusters = load_exposure_clusters(settings.universe_path)

    def reject(
        self,
        rule: str,
        reason: str,
        proposal: TradeProposal | None = None,
        intent_id: str | None = None,
        *,
        candidate: SwingCandidate | None = None,
        computed_values: dict[str, Any] | None = None,
    ) -> RiskDecision:
        reason_code = REASON_CODES.get(rule, f"REJECT_{rule.upper()}")
        values = dict(computed_values or {})
        self.database.record_violation(
            rule,
            reason,
            ticker=proposal.ticker if proposal else candidate.ticker if candidate else None,
            decision_id=proposal.decision_id if proposal else None,
            intent_id=intent_id,
            blocked=True,
        )
        self.database.record_risk_rejection(
            reason_code=reason_code,
            rule=rule,
            reason=reason,
            ticker=proposal.ticker if proposal else candidate.ticker if candidate else None,
            decision_id=proposal.decision_id if proposal else None,
            intent_id=intent_id,
            candidate=candidate.model_dump(mode="json") if candidate else None,
            computed_values=values,
        )
        return RiskDecision(approved=False, reason=reason, rule=rule, reason_code=reason_code)

    def _authoritative_stop(self, candidate: SwingCandidate, entry: float) -> tuple[float | None, str | None, dict[str, float]]:
        invalidation = candidate.structural_invalidation
        atr = candidate.atr
        distance = entry - invalidation
        values = {
            "entry": entry,
            "structural_invalidation": invalidation,
            "atr": atr,
            "stop_distance": distance,
            "stop_distance_atr": distance / atr if atr > 0 else 0.0,
        }
        if atr <= 0 or invalidation <= 0 or distance <= 0:
            return None, "invalid_stop", values
        if distance < self.settings.minimum_stop_atr * atr:
            return None, "stop_too_tight", values
        if distance > self.settings.maximum_stop_atr * atr:
            return None, "stop_too_wide", values
        return invalidation, None, values

    def _open_risk(self, portfolio: PortfolioRiskState) -> tuple[dict[str, Any] | None, str | None]:
        open_trades = self.database.open_trades()
        positions = {position.symbol.upper(): position for position in portfolio.positions if position.quantity != 0}
        durable_symbols = {str(trade["ticker"]).upper() for trade in open_trades}
        if set(positions) - durable_symbols:
            return None, "Broker positions are missing durable trade records"

        by_position: dict[str, float] = {}
        by_sector: defaultdict[str, float] = defaultdict(float)
        by_cluster: defaultdict[str, float] = defaultdict(float)
        total = 0.0
        for trade in open_trades:
            symbol = str(trade["ticker"]).upper()
            cluster = self.clusters.get(symbol)
            if cluster is None:
                return None, f"Open position {symbol} has no cluster mapping"
            position = positions.get(symbol)
            durable_qty = abs(float(trade.get("shares") or 0))
            if durable_qty > 0 and position is None:
                return None, f"Durable open trade {symbol} is missing from the broker position snapshot"
            if position is None:
                risk = float(trade.get("planned_dollar_risk") or 0)
            else:
                quantity = abs(float(position.quantity))
                cost_basis = float(position.average_entry_price)
                protective_stop = position.current_stop
                if protective_stop is None:
                    protective_stop = float(trade.get("final_stop") or trade.get("initial_stop") or 0)
                if protective_stop <= 0:
                    return None, f"Open position {symbol} has no valid protective stop"
                # A stop above cost basis contributes zero risk, never negative risk that could offset another trade.
                risk = quantity * max(cost_basis - float(protective_stop), 0.0)
            sector = str(trade.get("sector") or "UNKNOWN")
            by_position[symbol] = by_position.get(symbol, 0.0) + risk
            by_sector[sector] += risk
            by_cluster[cluster] += risk
            total += risk

        for admission in self.database.active_admissions():
            if admission.get("trade_id"):
                continue
            risk = float(admission.get("planned_dollar_risk") or 0)
            payload = admission.get("payload_json")
            try:
                import json

                decoded = json.loads(payload) if isinstance(payload, str) else (payload or {})
                candidate = decoded.get("candidate", {})
                symbol = str(admission["ticker"]).upper()
                sector = str(candidate.get("sector") or "UNKNOWN")
                cluster = self.clusters.get(symbol)
            except (TypeError, ValueError):
                return None, "An active admission has malformed risk context"
            if cluster is None:
                return None, f"Active admission {symbol} has no cluster mapping"
            by_position[symbol] = by_position.get(symbol, 0.0) + risk
            by_sector[sector] += risk
            by_cluster[cluster] += risk
            total += risk

        snapshot = {
            "total_open_risk": total,
            "open_risk_percent": total / portfolio.account.equity,
            "risk_by_sector": dict(by_sector),
            "risk_by_cluster": dict(by_cluster),
            "risk_by_position": by_position,
        }
        self.database.record_risk_snapshot(account_equity=portfolio.account.equity, **{key: snapshot[key] for key in ("total_open_risk", "risk_by_sector", "risk_by_cluster", "risk_by_position")})
        return snapshot, None

    @staticmethod
    def _activity_boundaries(now: datetime) -> tuple[str, str]:
        now_et = now.astimezone(ZoneInfo("America/New_York"))
        day_start = now_et.replace(hour=0, minute=0, second=0, microsecond=0)
        week_start = day_start - timedelta(days=day_start.weekday())
        return day_start.astimezone(timezone.utc).isoformat(), week_start.astimezone(timezone.utc).isoformat()

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
        day_start, week_start = self._activity_boundaries(now)
        computed: dict[str, Any] = {"price": quote.last, "equity": portfolio.account.equity}

        open_risk, mismatch = self._open_risk(portfolio)
        if mismatch:
            snapshot_symbols = {position.symbol.upper() for position in portfolio.positions if position.quantity != 0}
            if symbol in snapshot_symbols:
                return self.reject("no_adding", "Adding to an existing position is prohibited", proposal, intent_id, candidate=candidate, computed_values=computed)
            if len(snapshot_symbols) >= settings.max_open_positions:
                return self.reject("max_positions", "Maximum simultaneous positions reached", proposal, intent_id, candidate=candidate, computed_values=computed)
            return self.reject("broker_mismatch", mismatch, proposal, intent_id, candidate=candidate, computed_values=computed)
        assert open_risk is not None
        computed.update(open_risk)

        if proposal.decision != Decision.BUY:
            return self.reject("long_only", "Only BUY proposals are valid entry intents", proposal, intent_id, candidate=candidate, computed_values=computed)
        if proposal.ticker != candidate.ticker or quote.symbol.upper() != symbol:
            return self.reject("symbol_mismatch", "Proposal, candidate, and quote symbols do not match", proposal, intent_id, candidate=candidate, computed_values=computed)
        if asset is not None:
            if asset.symbol.upper() != symbol:
                return self.reject("asset_symbol_mismatch", "Broker asset identity does not match the proposal", proposal, intent_id, candidate=candidate, computed_values=computed)
            if asset.asset_class not in {"us_equity", "equity"} or not asset.tradable or asset.status != "active":
                return self.reject("asset_not_tradable", f"Broker reports unsupported or inactive asset {symbol}", proposal, intent_id, candidate=candidate, computed_values=computed)
            if asset.is_leveraged_or_inverse is True:
                return self.reject("banned_instrument", f"Broker classifies {symbol} as leveraged or inverse", proposal, intent_id, candidate=candidate, computed_values=computed)
            asset_age = (datetime.now(timezone.utc) - asset.retrieved_at).total_seconds()
            if asset_age < 0 or asset_age > settings.broker_asset_freshness_seconds:
                return self.reject("stale_asset_state", "Broker asset state is stale or future-dated", proposal, intent_id, candidate=candidate, computed_values=computed)

        if bool(self.database.get_state("kill_switch", False)):
            return self.reject("kill_switch", "Global kill switch is active", proposal, intent_id, candidate=candidate, computed_values=computed)
        if bool(self.database.get_state("reconciliation_halt", False)):
            return self.reject("reconciliation_halt", "New entries are halted until broker/database discrepancies are reconciled", proposal, intent_id, candidate=candidate, computed_values=computed)
        if bool(self.database.get_state("loss_streak_halt", False)):
            return self.reject("consecutive_loss_halt", "The consecutive-loss halt is latched pending explicit human review", proposal, intent_id, candidate=candidate, computed_values=computed)
        weekly_halt = self.database.get_state("weekly_drawdown_halt", False)
        if bool(weekly_halt.get("active")) if isinstance(weekly_halt, dict) else bool(weekly_halt):
            return self.reject("weekly_drawdown_halt", "The weekly drawdown halt is latched pending explicit human review", proposal, intent_id, candidate=candidate, computed_values=computed)
        if settings.execution_mode == "live":
            if not settings.trading_enabled:
                return self.reject("trading_disabled", "TRADING_ENABLED is false", proposal, intent_id, candidate=candidate, computed_values=computed)
            if portfolio.account.is_paper:
                return self.reject("account_mode_mismatch", "Live mode cannot use a paper account", proposal, intent_id, candidate=candidate, computed_values=computed)
            if portfolio.account.is_margin_enabled:
                return self.reject("margin_forbidden", "Margin-enabled execution is prohibited", proposal, intent_id, candidate=candidate, computed_values=computed)
            if portfolio.account.dedicated_agentic_account is not True:
                return self.reject("account_not_verified", "Dedicated agentic account identity was not verified", proposal, intent_id, candidate=candidate, computed_values=computed)
        elif settings.execution_mode == "paper" and not portfolio.account.is_paper:
            return self.reject("account_mode_mismatch", "Paper mode requires a verified paper account", proposal, intent_id, candidate=candidate, computed_values=computed)

        if self.database.intent_was_submitted(intent_id):
            return self.reject("duplicate_intent", f"Intent {intent_id} was already submitted", proposal, intent_id, candidate=candidate, computed_values=computed)
        if symbol in self.blacklist:
            return self.reject("long_term_blacklist", f"{symbol} is on the do-not-trade list", proposal, intent_id, candidate=candidate, computed_values=computed)
        if candidate.is_leveraged_or_inverse or symbol in LEVERAGED_OR_INVERSE_ETFS:
            return self.reject("banned_instrument", f"{symbol} is leveraged, inverse, or volatility-linked", proposal, intent_id, candidate=candidate, computed_values=computed)
        cluster = self.clusters.get(symbol)
        if cluster is None:
            return self.reject("no_cluster_mapping", f"{symbol} has no maintained exposure-cluster mapping", proposal, intent_id, candidate=candidate, computed_values=computed)
        computed["cluster"] = cluster
        computed["sector"] = candidate.sector
        if candidate.is_halted:
            return self.reject("trading_halt", f"{symbol} is halted", proposal, intent_id, candidate=candidate, computed_values=computed)
        if candidate.price <= settings.minimum_price or quote.last <= settings.minimum_price:
            return self.reject("minimum_price", f"Price must be greater than ${settings.minimum_price:.2f}", proposal, intent_id, candidate=candidate, computed_values=computed)
        if candidate.average_daily_volume <= settings.minimum_average_volume:
            return self.reject("minimum_liquidity", "Average daily volume does not exceed the configured floor", proposal, intent_id, candidate=candidate, computed_values=computed)
        if quote.spread_pct is None:
            return self.reject("spread_unavailable", "A current bid/ask spread is required", proposal, intent_id, candidate=candidate, computed_values=computed)
        if quote.spread_pct > settings.maximum_spread_pct:
            return self.reject("spread_too_wide", f"Spread {quote.spread_pct:.3%} exceeds limit", proposal, intent_id, candidate=candidate, computed_values=computed)
        if quote.market_timestamp is None or candidate.data.market_timestamp is None:
            return self.reject("market_data_missing", "Quote and completed daily-bar market timestamps are required", proposal, intent_id, candidate=candidate, computed_values=computed)
        if not candidate.completed_daily_bar:
            return self.reject("incomplete_daily_bar", "A completed daily bar is required for this swing setup", proposal, intent_id, candidate=candidate, computed_values=computed)

        future_tolerance = 5.0
        timestamps = {
            "quote": quote.retrieved_at,
            "market_data": quote.market_timestamp or quote.retrieved_at,
            "candidate": candidate.data.market_timestamp or candidate.data.retrieved_at,
            "account": portfolio.account.retrieved_at,
        }
        for name, timestamp in timestamps.items():
            if (timestamp - now).total_seconds() > future_tolerance:
                return self.reject(f"future_{name}", f"{name} timestamp is in the future", proposal, intent_id, candidate=candidate, computed_values=computed)
        if (now - timestamps["quote"]).total_seconds() > settings.quote_freshness_seconds:
            return self.reject("stale_quote", "Quote retrieval timestamp is stale", proposal, intent_id, candidate=candidate, computed_values=computed)
        if (now - timestamps["market_data"]).total_seconds() > settings.quote_freshness_seconds:
            return self.reject("stale_market_data", "Quote market timestamp is stale", proposal, intent_id, candidate=candidate, computed_values=computed)
        if (now - timestamps["candidate"]).total_seconds() > 86_400 * settings.strategy.maximum_bar_age_calendar_days:
            return self.reject("stale_candidate", "Candidate analysis is stale", proposal, intent_id, candidate=candidate, computed_values=computed)
        if (now - timestamps["account"]).total_seconds() > settings.quote_freshness_seconds:
            return self.reject("stale_account", "Account snapshot is stale", proposal, intent_id, candidate=candidate, computed_values=computed)

        if candidate.major_event_status != "clear":
            return self.reject("event_data_invalid", f"Major corporate-event status is {candidate.major_event_status}; structured coverage must be clear", proposal, intent_id, candidate=candidate, computed_values=computed)
        if candidate.major_event_retrieved_at is None:
            return self.reject("stale_event_data", "Corporate-event freshness timestamp is unavailable", proposal, intent_id, candidate=candidate, computed_values=computed)
        event_age = (now - candidate.major_event_retrieved_at).total_seconds()
        if event_age < 0 or event_age > settings.corporate_event_freshness_seconds:
            return self.reject("stale_event_data", "Corporate-event data is stale or future-dated", proposal, intent_id, candidate=candidate, computed_values=computed)
        if not candidate.is_etf:
            if candidate.earnings_data_status != "clear":
                return self.reject("earnings_unknown", f"Earnings data status is {candidate.earnings_data_status}", proposal, intent_id, candidate=candidate, computed_values=computed)
            if candidate.earnings_trading_days is None or candidate.earnings_trading_days < 0:
                return self.reject("earnings_unknown", "Earnings data is unavailable or malformed", proposal, intent_id, candidate=candidate, computed_values=computed)
            if candidate.earnings_trading_days <= settings.earnings_exclusion_trading_days:
                return self.reject("earnings_exclusion", f"Earnings are expected within {settings.earnings_exclusion_trading_days} trading days", proposal, intent_id, candidate=candidate, computed_values=computed)
            if candidate.earnings_retrieved_at is None:
                return self.reject("stale_earnings_data", "Earnings freshness timestamp is unavailable", proposal, intent_id, candidate=candidate, computed_values=computed)
            earnings_age = (now - candidate.earnings_retrieved_at).total_seconds()
            if earnings_age < 0 or earnings_age > settings.earnings_freshness_seconds:
                return self.reject("stale_earnings_data", "Earnings data is stale or future-dated", proposal, intent_id, candidate=candidate, computed_values=computed)

        if proposal.setup_type != candidate.setup_type or candidate.validator_failures:
            return self.reject("setup_mismatch", "Proposal does not match a hard-valid deterministic setup", proposal, intent_id, candidate=candidate, computed_values=computed)
        if proposal.candidate_score != candidate.score or proposal.market_regime != candidate.market_regime:
            return self.reject("score_mismatch", "Proposal score/regime does not match deterministic candidate data", proposal, intent_id, candidate=candidate, computed_values=computed)
        if candidate.score < settings.buy_score_threshold:
            return self.reject("candidate_score", f"Candidate score {candidate.score:.1f} is below {settings.buy_score_threshold:.1f}", proposal, intent_id, candidate=candidate, computed_values=computed)
        if candidate.market_regime in {MarketRegime.BEAR, MarketRegime.HIGH_VOLATILITY_RISK_OFF}:
            return self.reject("unsuitable_regime", f"New longs are disabled in {candidate.market_regime.value}", proposal, intent_id, candidate=candidate, computed_values=computed)

        stop, stop_rule, stop_values = self._authoritative_stop(candidate, quote.last)
        computed.update(stop_values)
        if stop_rule:
            return self.reject(stop_rule, f"Structural stop fails ATR sanity bounds ({stop_values['stop_distance_atr']:.2f} ATR)", proposal, intent_id, candidate=candidate, computed_values=computed)
        assert stop is not None
        target = candidate.resistance
        if not stop < quote.last < target:
            return self.reject("invalid_stop", "Quote must remain between the Python-owned stop and target", proposal, intent_id, candidate=candidate, computed_values=computed)
        risk_per_share = quote.last - stop
        current_rr = (target - quote.last) / risk_per_share
        computed.update(authoritative_stop=stop, authoritative_target=target, risk_per_share=risk_per_share, current_rr=current_rr)
        if current_rr < settings.minimum_rr:
            return self.reject("minimum_rr", f"Current structural reward/risk {current_rr:.2f} is below {settings.minimum_rr:.2f}", proposal, intent_id, candidate=candidate, computed_values=computed)
        if quote.last > candidate.price + settings.maximum_entry_slippage_r * (candidate.price - stop):
            return self.reject("chasing_entry", f"Current quote has moved more than {settings.maximum_entry_slippage_r:.2f}R above the deterministic entry", proposal, intent_id, candidate=candidate, computed_values=computed)

        existing_symbols = {position.symbol.upper() for position in portfolio.positions if position.quantity != 0}
        existing_symbols.update(open_risk["risk_by_position"])
        if symbol in existing_symbols or any(str(order.get("symbol", "")).upper() == symbol for order in portfolio.open_orders):
            return self.reject("no_adding", "Adding to an existing position is prohibited", proposal, intent_id, candidate=candidate, computed_values=computed)

        durable_day_count = self.database.new_position_count(day_start)
        durable_week_count = self.database.new_position_count(week_start)
        durable_losses = self.database.consecutive_losses()
        if len(existing_symbols) >= settings.max_open_positions:
            return self.reject("max_positions", "Maximum simultaneous positions reached", proposal, intent_id, candidate=candidate, computed_values=computed)
        if max(portfolio.new_positions_today, durable_day_count) >= settings.max_new_positions_day:
            return self.reject("daily_entry_limit", "Maximum new positions per day reached", proposal, intent_id, candidate=candidate, computed_values=computed)
        if max(portfolio.new_positions_this_week, durable_week_count) >= settings.max_new_positions_week:
            return self.reject("weekly_entry_limit", "Maximum new positions per week reached", proposal, intent_id, candidate=candidate, computed_values=computed)

        durable_high = self.database.equity_high(portfolio.account.equity)
        portfolio_drawdown = max(portfolio.drawdown_pct, (durable_high - portfolio.account.equity) / durable_high if durable_high else 0.0)
        weekly_high = self.database.equity_high_since(week_start, portfolio.account.equity)
        weekly_drawdown = max(portfolio.weekly_drawdown_pct, (weekly_high - portfolio.account.equity) / weekly_high if weekly_high else 0.0)
        computed.update(portfolio_drawdown=portfolio_drawdown, weekly_drawdown=weekly_drawdown)
        if weekly_drawdown >= settings.weekly_drawdown_halt_pct:
            self.database.activate_halt("weekly_drawdown_halt", f"Weekly drawdown reached {weekly_drawdown:.2%}", "risk_manager")
            return self.reject("weekly_drawdown_halt", "Weekly drawdown halt requires human review", proposal, intent_id, candidate=candidate, computed_values=computed)
        if portfolio_drawdown >= settings.halt_drawdown_pct:
            self.database.activate_halt("drawdown_halt", f"Portfolio drawdown reached {portfolio_drawdown:.2%}", "risk_manager")
            return self.reject("drawdown_halt", f"Portfolio drawdown {portfolio_drawdown:.2%} requires human review", proposal, intent_id, candidate=candidate, computed_values=computed)
        if bool(self.database.get_state("drawdown_halt", False)):
            return self.reject("drawdown_halt", "The portfolio drawdown halt is latched pending human review", proposal, intent_id, candidate=candidate, computed_values=computed)
        if max(portfolio.consecutive_losses, durable_losses) >= settings.consecutive_loss_halt:
            self.database.activate_halt("loss_streak_halt", f"Loss streak reached {max(portfolio.consecutive_losses, durable_losses)}", "risk_manager")
            return self.reject("consecutive_loss_halt", "Consecutive completed losses require review", proposal, intent_id, candidate=candidate, computed_values=computed)

        prior_loss = self.database.last_losing_trade(symbol)
        if prior_loss and prior_loss.get("exit_datetime"):
            exit_date = datetime.fromisoformat(prior_loss["exit_datetime"]).date()
            trading_days = sum(1 for offset in range(1, max(1, (now.date() - exit_date).days + 1)) if (exit_date + timedelta(days=offset)).weekday() < 5)
            if trading_days <= settings.cooldown_trading_days:
                return self.reject("loss_cooldown", f"{symbol} is in a post-loss cooldown", proposal, intent_id, candidate=candidate, computed_values=computed)
            required = {"what_failed", "what_changed", "failure_resolved", "new_setup"}
            if not proposal.reentry_context or not required.issubset(proposal.reentry_context) or not all(proposal.reentry_context[key].strip() for key in required):
                return self.reject("reentry_review_missing", "Post-loss re-entry requires four explicit review answers", proposal, intent_id, candidate=candidate, computed_values=computed)

        regime_multiplier = {
            MarketRegime.STRONG_BULL: 1.0,
            MarketRegime.BULL: 1.0,
            MarketRegime.NEUTRAL: settings.neutral_risk_multiplier,
            MarketRegime.CHOPPY: settings.choppy_risk_multiplier,
            MarketRegime.BEAR: settings.hostile_risk_multiplier,
            MarketRegime.HIGH_VOLATILITY_RISK_OFF: 0.0,
        }[candidate.market_regime]
        base_risk = settings.reduced_risk_pct if portfolio_drawdown >= settings.reduce_risk_drawdown_pct else settings.normal_risk_pct
        applied_risk_pct = min(base_risk * regime_multiplier, settings.absolute_max_risk_pct)
        allowed_dollar_risk = portfolio.account.equity * applied_risk_pct
        risk_quantity = math.floor(allowed_dollar_risk / risk_per_share)
        computed.update(applied_risk_pct=applied_risk_pct, allowed_dollar_risk=allowed_dollar_risk, risk_quantity=risk_quantity)
        if risk_quantity < settings.broker_min_quantity:
            return self.reject("position_too_small", "Risk allocation floors to zero whole shares", proposal, intent_id, candidate=candidate, computed_values=computed)

        available_cash = min(portfolio.account.cash, portfolio.account.buying_power)
        cash_quantity = math.floor(available_cash / quote.last)
        if cash_quantity < settings.broker_min_quantity:
            computed["available_cash"] = available_cash
            return self.reject("insufficient_cash", "Available cash cannot fund one whole share", proposal, intent_id, candidate=candidate, computed_values=computed)

        portfolio_headroom = portfolio.account.equity * settings.max_combined_open_risk_pct - open_risk["total_open_risk"]
        sector_headroom = portfolio.account.equity * settings.max_sector_open_risk_pct - open_risk["risk_by_sector"].get(candidate.sector, 0.0)
        cluster_headroom = portfolio.account.equity * settings.max_cluster_open_risk_pct - open_risk["risk_by_cluster"].get(cluster, 0.0)
        constraints = {
            "risk": risk_quantity,
            "cash": cash_quantity,
            "position_allocation": math.floor(portfolio.account.equity * settings.max_position_exposure_pct / quote.last),
            "portfolio_open_risk": max(0, math.floor(portfolio_headroom / risk_per_share)),
            "sector_open_risk": max(0, math.floor(sector_headroom / risk_per_share)),
            "cluster_open_risk": max(0, math.floor(cluster_headroom / risk_per_share)),
            "liquidity": math.floor(candidate.average_daily_volume * settings.maximum_adv_fraction),
        }
        computed.update(risk_constraints=constraints, available_cash=available_cash)
        quantity = min(constraints.values())
        if quantity < settings.broker_min_quantity:
            binding = min(constraints, key=lambda name: constraints[name])
            rule = {
                "cash": "insufficient_cash",
                "portfolio_open_risk": "max_combined_open_risk",
                "sector_open_risk": "sector_risk",
                "cluster_open_risk": "cluster_risk",
                "liquidity": "liquidity_limit",
            }.get(binding, "position_too_small")
            return self.reject(rule, f"{binding} constraint leaves no valid whole-share order", proposal, intent_id, candidate=candidate, computed_values=computed)

        planned_risk = quantity * risk_per_share
        account_risk_pct = planned_risk / portfolio.account.equity
        if account_risk_pct > settings.absolute_max_risk_pct + 1e-12:
            return self.reject("absolute_risk_max", "Planned trade risk exceeds the 1% hard maximum", proposal, intent_id, candidate=candidate, computed_values=computed)
        return RiskDecision(
            approved=True,
            reason="All deterministic swing risk rules passed",
            quantity=quantity,
            allowed_dollar_risk=round(allowed_dollar_risk, 2),
            planned_dollar_risk=round(planned_risk, 2),
            planned_account_risk_pct=account_risk_pct,
            planned_rr=current_rr,
            applied_risk_pct=applied_risk_pct,
            authoritative_stop=stop,
            risk_constraints=constraints,
        )

    def existing_position_event_action(self, candidate: SwingCandidate) -> str:
        """Prompt 04 consumes this deterministic policy; normal swings do not cross known binary events."""
        if candidate.major_event_status != "clear" or (not candidate.is_etf and (candidate.earnings_data_status != "clear" or candidate.earnings_trading_days is None)):
            return "EXIT_REVIEW_REQUIRED_EVENT_DATA_UNSAFE"
        earnings_days = candidate.earnings_trading_days
        if not candidate.is_etf and earnings_days is not None and earnings_days <= self.settings.earnings_exclusion_trading_days:
            return "EXIT_BEFORE_EARNINGS"
        return "HOLD_EVENT_CLEAR"

    def validate_stop_change(self, ticker: str, current_stop: float, proposed_stop: float) -> RiskDecision:
        """For a long position the stop may stay unchanged or rise, never fall."""
        if current_stop <= 0 or proposed_stop <= 0:
            return self.reject("invalid_stop", f"Invalid stop for {ticker}")
        if proposed_stop < current_stop:
            return self.reject("stop_widening", f"Stop cannot widen from {current_stop:.2f} to {proposed_stop:.2f}")
        return RiskDecision(approved=True, reason="Stop is unchanged or tighter")
