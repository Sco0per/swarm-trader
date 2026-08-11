"""Idempotent broker review, submission, and audit workflow."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import NAMESPACE_URL, uuid4, uuid5

from pydantic import BaseModel
from zoneinfo import ZoneInfo

from .brokers.base import BrokerProvider
from .config import SwingSettings
from .database import SwingDatabase
from .models import BrokerOrder, OrderIntent, PortfolioRiskState, RiskDecision, SwingCandidate, TradeProposal
from .reconciliation import BrokerReconciler
from .risk import SwingRiskManager


class ExecutionResult(BaseModel):
    status: str
    intent_id: str
    risk: RiskDecision
    broker_review: dict | None = None
    broker_order: BrokerOrder | None = None
    trade_id: str | None = None
    message: str


class SwingExecutionService:
    """The only supported new-entry path for adaptive swing trading."""

    def __init__(self, settings: SwingSettings, database: SwingDatabase, broker: BrokerProvider):
        self.settings = settings
        self.database = database
        self.broker = broker
        self.risk_manager = SwingRiskManager(settings, database)

    @staticmethod
    def intent_id(proposal: TradeProposal) -> str:
        stable = f"{proposal.strategy_version}:{proposal.decision_id}:{proposal.ticker}:BUY"
        return str(uuid5(NAMESPACE_URL, stable))

    def refresh_portfolio_state(self) -> PortfolioRiskState:
        """Refresh account, buying power, positions, and orders immediately before review."""
        account = self.broker.get_account()
        buying_power = self.broker.get_buying_power()
        account = account.model_copy(update={"buying_power": buying_power, "retrieved_at": datetime.now(timezone.utc)})
        positions = self.broker.get_positions()
        open_orders = self.broker.get_open_orders()
        equity_high = self.database.equity_high(account.equity)
        drawdown = max(0.0, (equity_high - account.equity) / equity_high) if equity_high else 0.0
        now_et = datetime.now(ZoneInfo("America/New_York"))
        day_start = now_et.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc).isoformat()
        week_start = (
            now_et.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=now_et.weekday())
        ).astimezone(timezone.utc).isoformat()
        open_trades = self.database.open_trades()
        planned_open_risk = sum(float(trade.get("planned_dollar_risk") or 0) for trade in open_trades)
        self.database.record_equity_snapshot(
            equity=account.equity, cash=account.cash, buying_power=account.buying_power,
            drawdown_pct=drawdown, execution_mode=self.settings.execution_mode,
        )
        return PortfolioRiskState(
            account=account,
            positions=positions,
            open_orders=open_orders,
            equity_high=equity_high,
            drawdown_pct=drawdown,
            planned_open_risk=planned_open_risk,
            new_positions_today=self.database.new_position_count(day_start),
            new_positions_this_week=self.database.new_position_count(week_start),
            consecutive_losses=self.database.consecutive_losses(),
            drawdown_halt_latched=bool(self.database.get_state("drawdown_halt", False)),
            loss_streak_halt_latched=bool(self.database.get_state("loss_streak_halt", False)),
            reconciliation_halt_latched=bool(self.database.get_state("reconciliation_halt", False)),
            kill_switch=bool(self.database.get_state("kill_switch", False)),
        )

    def submit(self, proposal: TradeProposal, candidate: SwingCandidate, *, dry_run: bool = True) -> ExecutionResult:
        intent_id = self.intent_id(proposal)
        self.database.record_candidate(candidate)
        if not dry_run:
            reconciliation = BrokerReconciler(self.settings, self.database, self.broker).reconcile()
            if not reconciliation.reconciled:
                risk = self.risk_manager.reject(
                    "pre_submission_reconciliation",
                    "Mandatory broker reconciliation failed: " + "; ".join(reconciliation.discrepancies),
                    proposal,
                    intent_id,
                )
                self.database.record_execution_event(
                    intent_id=intent_id, decision_id=proposal.decision_id, trade_id=None, broker_order_id=None,
                    ticker=proposal.ticker, side="buy", quantity=0, event_type="RECONCILIATION_REJECTED",
                    status="REJECTED", payload=reconciliation.model_dump(mode="json"),
                )
                return ExecutionResult(status="REJECTED", intent_id=intent_id, risk=risk, message=risk.reason)
        try:
            asset = self.broker.get_asset(proposal.ticker)
            quote = self.broker.get_quote(proposal.ticker)
            portfolio = self.refresh_portfolio_state()
        except Exception as exc:
            risk = self.risk_manager.reject("preflight_failed", f"Required broker refresh failed: {exc}", proposal, intent_id)
            return ExecutionResult(status="REJECTED", intent_id=intent_id, risk=risk, message=risk.reason)

        risk = self.risk_manager.validate_entry(proposal, candidate, quote, portfolio, intent_id, asset=asset)
        if not risk.approved:
            self.database.record_execution_event(
                intent_id=intent_id, decision_id=proposal.decision_id, trade_id=None, broker_order_id=None,
                ticker=proposal.ticker, side="buy", quantity=0, event_type="RISK_REJECTED", status="REJECTED",
                payload=risk.model_dump(mode="json"),
            )
            if risk.rule == "drawdown_halt":
                self._write_drawdown_review(portfolio, risk.reason)
            return ExecutionResult(status="REJECTED", intent_id=intent_id, risk=risk, message=risk.reason)

        intent = OrderIntent(
            intent_id=intent_id,
            decision_id=proposal.decision_id,
            ticker=proposal.ticker,
            quantity=risk.quantity,
            limit_price=quote.last,
            stop_price=proposal.stop,
            target_price=proposal.target,
            strategy_version=proposal.strategy_version,
        )
        try:
            review = self.broker.review_order(intent)
        except Exception as exc:
            return self._broker_rejection(intent, risk, "BROKER_REVIEW_FAILED", str(exc))
        if not review.get("approved", False):
            return self._broker_rejection(intent, risk, "BROKER_REVIEW_REJECTED", str(review.get("reason", "broker rejected review")), review)

        self.database.record_execution_event(
            intent_id=intent_id, decision_id=proposal.decision_id, trade_id=None, broker_order_id=None,
            ticker=proposal.ticker, side="buy", quantity=risk.quantity, event_type="ORDER_REVIEWED", status="APPROVED", payload=review,
        )
        if dry_run:
            return ExecutionResult(
                status="DRY_RUN_APPROVED", intent_id=intent_id, risk=risk, broker_review=review,
                message="Broker review and deterministic risk validation passed; no order was sent",
            )
        if not self.settings.trading_enabled:
            rejected = self.risk_manager.reject("trading_disabled", "TRADING_ENABLED=false; order was not sent", proposal, intent_id)
            return ExecutionResult(status="REJECTED", intent_id=intent_id, risk=rejected, broker_review=review, message=rejected.reason)
        trade_id = str(uuid4())
        draft = self._trade_values(
            trade_id=trade_id, proposal=proposal, candidate=candidate, intent=intent, risk=risk,
            entry_datetime=intent.created_at, entry_price=quote.last, shares=0,
            status="SUBMITTED", broker_order_id=None,
        )
        now_et = datetime.now(ZoneInfo("America/New_York"))
        day_start = now_et.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc).isoformat()
        week_start = (
            now_et.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=now_et.weekday())
        ).astimezone(timezone.utc).isoformat()
        reserved, rule, message = self.database.reserve_entry_admission(
            intent_id=intent_id,
            decision_id=proposal.decision_id,
            ticker=proposal.ticker,
            planned_dollar_risk=risk.planned_dollar_risk,
            account_equity=portfolio.account.equity,
            payload={
                "intent": intent.model_dump(mode="json"),
                "trade": draft,
                "proposal": proposal.model_dump(mode="json"),
                "candidate": candidate.model_dump(mode="json"),
            },
            day_start_iso=day_start,
            week_start_iso=week_start,
            broker_position_symbols={position.symbol.upper() for position in portfolio.positions if position.quantity != 0},
            max_positions=self.settings.max_open_positions,
            max_day=self.settings.max_new_positions_day,
            max_week=self.settings.max_new_positions_week,
            max_open_risk_pct=self.settings.max_combined_open_risk_pct,
        )
        if not reserved:
            rejected = self.risk_manager.reject(rule or "admission_rejected", message, proposal, intent_id)
            return ExecutionResult(status="REJECTED", intent_id=intent_id, risk=rejected, broker_review=review, message=rejected.reason)

        try:
            order = self.broker.place_order(intent)
        except Exception as exc:
            # Retain the reservation and globally halt admissions until broker truth is known.
            self.database.update_admission(intent_id, "UNKNOWN")
            self.database.set_state("reconciliation_halt", True, "execution_unknown_outcome")
            self.database.record_execution_event(
                intent_id=intent_id, decision_id=proposal.decision_id, trade_id=None, broker_order_id=None,
                ticker=proposal.ticker, side="buy", quantity=risk.quantity, event_type="BROKER_ERROR",
                status="UNKNOWN_REQUIRES_RECONCILIATION", payload={"error": str(exc)},
            )
            return ExecutionResult(
                status="UNKNOWN_REQUIRES_RECONCILIATION", intent_id=intent_id, risk=risk, broker_review=review,
                message="Broker call failed after intent reservation; reconcile before any retry",
            )

        broker_status = order.status.lower()
        if broker_status in {"rejected", "canceled", "cancelled", "expired", "stopped", "suspended"} and order.filled_quantity <= 0:
            admission_status = "REJECTED" if broker_status == "rejected" else "CANCELED"
            self.database.update_admission(
                intent_id, admission_status, broker_order_id=order.broker_order_id,
            )
            self.database.record_execution_event(
                intent_id=intent_id, decision_id=proposal.decision_id, trade_id=None,
                broker_order_id=order.broker_order_id, ticker=proposal.ticker, side="buy", quantity=risk.quantity,
                event_type="ORDER_TERMINAL", status=broker_status, payload=order.model_dump(mode="json"),
            )
            return ExecutionResult(
                status=admission_status, intent_id=intent_id, risk=risk, broker_review=review,
                broker_order=order, message=f"Broker returned terminal status {broker_status}; no trade was opened",
            )
        accepted_statuses = {
            "accepted", "accepted_for_bidding", "new", "pending_new", "partially_filled", "partial_fill",
            "filled", "held", "pending_cancel", "pending_replace", "replaced",
        }
        if broker_status not in accepted_statuses:
            self.database.update_admission(intent_id, "UNKNOWN", broker_order_id=order.broker_order_id)
            self.database.set_state("reconciliation_halt", True, "unexpected_broker_status")
            self.database.record_execution_event(
                intent_id=intent_id, decision_id=proposal.decision_id, trade_id=None,
                broker_order_id=order.broker_order_id, ticker=proposal.ticker, side="buy", quantity=risk.quantity,
                event_type="UNEXPECTED_BROKER_STATUS", status=broker_status, payload=order.model_dump(mode="json"),
            )
            return ExecutionResult(
                status="UNKNOWN_REQUIRES_RECONCILIATION", intent_id=intent_id, risk=risk,
                broker_review=review, broker_order=order,
                message=f"Unexpected broker status {broker_status}; reconciliation is mandatory",
            )

        status = "PARTIALLY_FILLED" if broker_status in {"partially_filled", "partial_fill"} else "SUBMITTED"
        stored_trade = self._trade_values(
            trade_id=trade_id,
            proposal=proposal, candidate=candidate, intent=intent, risk=risk,
            entry_datetime=order.submitted_at,
            entry_price=order.average_fill_price or quote.last,
            shares=order.filled_quantity,
            status=status,
            broker_order_id=order.broker_order_id,
        )
        try:
            self.database.add_trade(stored_trade)
            self.database.update_admission(
                intent_id, "SUBMITTED", trade_id=trade_id, broker_order_id=order.broker_order_id,
            )
        except Exception as exc:
            self.database.update_admission(intent_id, "UNKNOWN", broker_order_id=order.broker_order_id)
            self.database.set_state("reconciliation_halt", True, "execution_persistence_failure")
            self.database.record_execution_event(
                intent_id=intent_id, decision_id=proposal.decision_id, trade_id=None,
                broker_order_id=order.broker_order_id, ticker=proposal.ticker, side="buy", quantity=risk.quantity,
                event_type="PERSISTENCE_ERROR", status="UNKNOWN_REQUIRES_RECONCILIATION", payload={"error": str(exc)},
            )
            return ExecutionResult(
                status="UNKNOWN_REQUIRES_RECONCILIATION", intent_id=intent_id, risk=risk,
                broker_review=review, broker_order=order, message="Broker accepted the call but persistence failed; reconciliation is mandatory",
            )
        self.database.record_execution_event(
            intent_id=intent_id, decision_id=proposal.decision_id, trade_id=trade_id,
            broker_order_id=order.broker_order_id, ticker=proposal.ticker, side="buy", quantity=risk.quantity,
            event_type="ORDER_SUBMITTED", status=order.status, payload=order.model_dump(mode="json"),
        )
        return ExecutionResult(
            status=status, intent_id=intent_id, risk=risk, broker_review=review, broker_order=order,
            trade_id=trade_id, message="Order submitted once and persisted for reconciliation",
        )

    def _trade_values(
        self, *, trade_id: str, proposal: TradeProposal, candidate: SwingCandidate, intent: OrderIntent,
        risk: RiskDecision, entry_datetime: datetime, entry_price: float, shares: float,
        status: str, broker_order_id: str | None,
    ) -> dict:
        return {
            "trade_id": trade_id,
            "decision_id": proposal.decision_id,
            "intent_id": intent.intent_id,
            "candidate_id": candidate.candidate_id,
            "ticker": proposal.ticker,
            "setup_type": proposal.setup_type.value,
            "status": status,
            "strategy_version": proposal.strategy_version,
            "entry_datetime": entry_datetime.isoformat(),
            "entry_price": entry_price,
            "initial_stop": proposal.stop,
            "final_stop": proposal.stop,
            "target": proposal.target,
            "shares": shares,
            "position_value": shares * entry_price,
            "planned_dollar_risk": risk.planned_dollar_risk,
            "planned_account_risk_pct": risk.planned_account_risk_pct,
            "planned_rr": risk.planned_rr,
            "market_regime": proposal.market_regime.value,
            "sector": candidate.sector,
            "sector_trend": candidate.sector_trend,
            "sector_relative_strength": candidate.sector_relative_strength,
            "stock_relative_strength": candidate.stock_relative_strength_spy,
            "stock_relative_strength_sector": candidate.stock_relative_strength_sector,
            "rsi": candidate.rsi,
            "macd": candidate.macd,
            "atr": candidate.atr,
            "adx": candidate.adx,
            "ma20": candidate.ma20,
            "ma50": candidate.ma50,
            "ma200": candidate.ma200,
            "volume_ratio": candidate.volume_ratio,
            "support": candidate.support,
            "resistance": candidate.resistance,
            "candidate_score": candidate.score,
            "bull_thesis": proposal.bull_case,
            "bear_thesis": proposal.bear_case,
            "pm_reasoning": proposal.invalidation,
            "reason_entry": proposal.bull_case,
            "broker_provider": self.broker.name,
            "broker_order_id": broker_order_id,
            "order_type": intent.entry_type,
            "risk_validation_result": risk.model_dump_json(),
        }

    def _broker_rejection(
        self, intent: OrderIntent, risk: RiskDecision, event_type: str, message: str, payload: dict | None = None,
    ) -> ExecutionResult:
        self.database.record_execution_event(
            intent_id=intent.intent_id, decision_id=intent.decision_id, trade_id=None, broker_order_id=None,
            ticker=intent.ticker, side=intent.side, quantity=intent.quantity, event_type=event_type,
            status="REJECTED", payload=payload or {"error": message},
        )
        return ExecutionResult(status="REJECTED", intent_id=intent.intent_id, risk=risk, broker_review=payload, message=message)

    def _write_drawdown_review(self, portfolio: PortfolioRiskState, reason: str) -> Path:
        report_dir = Path(__file__).resolve().parents[2] / "reports"
        report_dir.mkdir(parents=True, exist_ok=True)
        path = report_dir / f"DRAWDOWN_REVIEW_{datetime.now().date().isoformat()}.md"
        path.write_text(
            "# Drawdown Review\n\n"
            f"Generated: {datetime.now(timezone.utc).isoformat()}\n\n"
            f"Reason: {reason}\n\n"
            f"Equity: ${portfolio.account.equity:,.2f}\n\n"
            f"Equity high: ${portfolio.equity_high:,.2f}\n\n"
            f"Drawdown: {portfolio.drawdown_pct:.2%}\n\n"
            "New trading remains latched off until an explicit human review is recorded. Existing positions may only be managed or exited.\n",
            encoding="utf-8",
        )
        return path
