"""Operational command line for the adaptive swing subsystem."""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from .agents import AgentPipeline
from .brokers.alpaca import AlpacaPaperProvider
from .brokers.human_supervised import HumanSuppliedBrokerProvider
from .config import load_settings, ROOT
from .data_feed import load_scan_inputs
from .database import SCHEMA_VERSION, SwingDatabase
from .decision_versioning import persist_scan_decisions
from .execution import SwingExecutionService
from .lessons_review import review_observations
from .lifecycle import PositionLifecycleService
from .llm_backend import AnthropicStructuredBackend
from .market import DeterministicSwingScanner
from .measurement import TradeMeasurementService
from .models import (
    RiskDecision,
    SwingCandidate,
    TechnicalThesis,
    TradeProposal,
    TradeThesis,
)
from .postmortem import PostmortemEngine
from .reconciliation import BrokerReconciler
from .reporting import ReportingService
from .risk import SwingRiskManager
from .stops import ProtectedStopService


def _backend_if_configured(database: SwingDatabase) -> AnthropicStructuredBackend | None:
    return AnthropicStructuredBackend(database) if os.getenv("ANTHROPIC_API_KEY") else None


def _database():
    settings = load_settings()
    database = SwingDatabase(settings.database_path)
    database.initialize(settings.strategy_version)
    return settings, database


def _journal_open_trade_daily_bars(database: SwingDatabase, bars_by_symbol: dict) -> None:
    measurements = TradeMeasurementService(database)
    for trade in database.open_trades():
        ticker = str(trade["ticker"]).upper()
        raw = bars_by_symbol.get(ticker)
        if raw is None:
            database.record_log(
                level="WARNING",
                event="OPEN_TRADE_MEASUREMENT_SKIPPED",
                reason_code="DAILY_BAR_MISSING",
                ticker=ticker,
                trade_id=trade["trade_id"],
                context={"granularity": "1d"},
            )
            continue
        try:
            if hasattr(raw, "iloc"):
                row = raw.iloc[-1]
                values = {str(key).lower(): value for key, value in row.items()}
                observed_at = raw.index[-1].to_pydatetime() if hasattr(raw.index[-1], "to_pydatetime") else None
            else:
                values = {str(key).lower(): value for key, value in raw[-1].items()}
                observed_at = None
            measurements.record_daily_bar(
                trade["trade_id"],
                high=float(values["high"]),
                low=float(values["low"]),
                close=float(values["close"]),
                source="deterministic_daily_bar",
                observed_at=observed_at,
            )
        except (KeyError, TypeError, ValueError, IndexError) as exc:
            database.record_log(
                level="WARNING",
                event="OPEN_TRADE_MEASUREMENT_SKIPPED",
                reason_code="DAILY_BAR_MALFORMED",
                ticker=ticker,
                trade_id=trade["trade_id"],
                context={"error": str(exc), "granularity": "1d"},
            )


def main() -> int:
    parser = argparse.ArgumentParser(description="Conservative adaptive swing trading controls")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init-db", help="Initialize/migrate the SQLite system of record")
    sub.add_parser("status", help="Show kill switches, mode, and durable record counts")
    sub.add_parser("analytics", help="Calculate expectancy-first analytics")
    report = sub.add_parser("report", help="Generate a persistent report")
    report.add_argument("type", choices=["daily", "weekly", "20-trade", "50-trade", "100-trade", "graduation"])
    kill = sub.add_parser("kill-switch", help="Turn the global new-order kill switch on or off")
    kill.add_argument("state", choices=["on", "off"])
    kill.add_argument("--approved-by", required=True)
    clear = sub.add_parser("clear-drawdown-halt", help="Record explicit human approval after drawdown review")
    clear.add_argument("--approved-by", required=True)
    clear_losses = sub.add_parser("clear-loss-streak-halt", help="Record explicit human approval after loss-streak review")
    clear_losses.add_argument("--approved-by", required=True)
    acknowledge_reconciliation = sub.add_parser("ack-reconciliation", help="Clear a latched reconciliation halt after a clean reconciliation")
    acknowledge_reconciliation.add_argument("--approved-by", required=True)
    acknowledge_reconciliation.add_argument("--reason", required=True)
    approve = sub.add_parser("approve-strategy", help="Human-only candidate promotion")
    approve.add_argument("hypothesis_id")
    approve.add_argument("--approved-by", required=True)
    paper = sub.add_parser("paper-review", help="Risk-check a structured proposal against Alpaca paper state")
    paper.add_argument("input", type=Path, help="JSON containing candidate and proposal objects")
    paper.add_argument("--submit", action="store_true", help="Submit only when TRADING_ENABLED=true")
    sub.add_parser("scan", help="Scan the market and print ranked candidates (no LLM calls, no execution)")
    sub.add_parser("run", help="Autonomous daily cycle: scan, analyze with Claude, decide, and paper-trade")
    sub.add_parser("review-observations", help="Aggregate postmortem observations into research hypotheses")
    live_ticket = sub.add_parser(
        "live-ticket",
        help="Compute an approved live Robinhood order ticket from a human-supplied account snapshot; never places an order",
    )
    live_ticket.add_argument("input", type=Path, help="JSON with candidate, proposal, and a fresh broker_snapshot")
    record_fill = sub.add_parser("record-live-fill", help="Journal a real fill you already executed through Robinhood yourself")
    record_fill.add_argument("input", type=Path, help="JSON with candidate, proposal, and the real fill details")
    record_exit = sub.add_parser("record-live-exit", help="Journal a real exit you already executed through Robinhood, and run its postmortem")
    record_exit.add_argument("trade_id")
    record_exit.add_argument("exit_price", type=float)
    record_exit.add_argument("--reason", required=True)
    sub.add_parser("reconcile", help="Reconcile Alpaca paper orders, positions, fills, and automatic closures")
    stop = sub.add_parser("tighten-stop", help="Tighten an active trade's Alpaca paper protective stop")
    stop.add_argument("trade_id")
    stop.add_argument("broker_stop_order_id")
    stop.add_argument("new_stop", type=float)
    args = parser.parse_args()

    settings, database = _database()
    reports = ReportingService(database, settings, ROOT / "reports")
    if args.command == "init-db":
        print(json.dumps({"database": str(settings.database_path), "schema_version": SCHEMA_VERSION, "counts": database.table_counts()}, indent=2))
    elif args.command == "status":
        print(
            json.dumps(
                {
                    "trading_style": settings.trading_style,
                    "execution_mode": settings.execution_mode,
                    "trading_enabled": settings.trading_enabled,
                    "kill_switch": database.get_state("kill_switch", False),
                    "drawdown_halt": database.get_state("drawdown_halt", False),
                    "loss_streak_halt": database.get_state("loss_streak_halt", False),
                    "reconciliation_halt": database.get_state("reconciliation_halt", False),
                    "last_reconciliation": database.get_state("last_reconciliation"),
                    "records": database.table_counts(),
                },
                indent=2,
            )
        )
    elif args.command == "analytics":
        print(json.dumps(reports.dashboard_payload(), indent=2, default=str))
    elif args.command == "report":
        print(reports.generate(args.type))
    elif args.command == "kill-switch":
        if args.state == "on":
            database.activate_halt("kill_switch", "Human activated global kill switch", args.approved_by)
        else:
            database.set_state("kill_switch", False, args.approved_by)
        print(f"kill_switch={args.state}")
    elif args.command == "clear-drawdown-halt":
        database.set_state("drawdown_halt", False, args.approved_by)
        database.set_state("drawdown_review_approval", {"approved_by": args.approved_by}, args.approved_by)
        print("Drawdown halt cleared by explicit human approval; retain conservative risk settings.")
    elif args.command == "clear-loss-streak-halt":
        database.set_state("loss_streak_halt", False, args.approved_by)
        database.set_state("loss_streak_review_approval", {"approved_by": args.approved_by}, args.approved_by)
        print("Consecutive-loss halt cleared by explicit human approval.")
    elif args.command == "ack-reconciliation":
        database.acknowledge_reconciliation_halt(acknowledged_by=args.approved_by, reason=args.reason)
        print("Reconciliation halt cleared after clean broker truth and explicit human acknowledgement.")
    elif args.command == "approve-strategy":
        print(database.approve_candidate_strategy(args.hypothesis_id, args.approved_by))
    elif args.command == "paper-review":
        if settings.execution_mode != "paper":
            raise ValueError("paper-review requires EXECUTION_MODE=paper")
        payload = json.loads(args.input.read_text(encoding="utf-8"))
        candidate = SwingCandidate.model_validate(payload["candidate"])
        proposal = TradeProposal.model_validate(payload["proposal"])
        broker = AlpacaPaperProvider(os.getenv("ALPACA_API_KEY", ""), os.getenv("ALPACA_API_SECRET", ""))
        result = SwingExecutionService(settings, database, broker).submit(proposal, candidate, dry_run=not args.submit)
        print(result.model_dump_json(indent=2))
    elif args.command in {"reconcile", "tighten-stop"}:
        if settings.execution_mode != "paper":
            raise ValueError(f"{args.command} currently supports EXECUTION_MODE=paper only")
        broker = AlpacaPaperProvider(os.getenv("ALPACA_API_KEY", ""), os.getenv("ALPACA_API_SECRET", ""))
        if args.command == "reconcile":
            reconciler = BrokerReconciler(settings, database, broker, postmortem_backend=_backend_if_configured(database))
            result = reconciler.reconcile()
            print(result.model_dump_json(indent=2))
            return 0 if result.reconciled else 2
        order = ProtectedStopService(database, broker, SwingRiskManager(settings, database)).tighten(
            args.trade_id,
            args.broker_stop_order_id,
            args.new_stop,
        )
        print(order.model_dump_json(indent=2))
    elif args.command == "scan":
        inputs = load_scan_inputs(database=database, universe_path=settings.universe_path)
        _journal_open_trade_daily_bars(database, inputs.bars_by_symbol)
        scanner = DeterministicSwingScanner(settings)
        candidates = scanner.scan(
            inputs.assets,
            inputs.bars_by_symbol,
            spy_bars=inputs.spy_bars,
            qqq_bars=inputs.qqq_bars,
            sector_bars=inputs.sector_bars,
            source="alpaca",
        )
        persist_scan_decisions(
            database,
            settings,
            run_id=str(uuid4()),
            candidates=candidates,
            funnel_report=getattr(scanner, "last_report", type("EmptyFunnel", (), {"rejections": []})()),
        )
        print(
            json.dumps(
                {
                    "candidates_found": len(candidates),
                    "funnel": scanner.last_report.as_dict() if hasattr(scanner, "last_report") else None,
                    "candidates": [candidate.model_dump(mode="json") for candidate in candidates],
                },
                indent=2,
                default=str,
            )
        )
    elif args.command == "run":
        if settings.execution_mode != "paper":
            raise ValueError("run currently supports EXECUTION_MODE=paper only")
        cycle_started_at = datetime.now(timezone.utc).isoformat()
        broker = AlpacaPaperProvider(os.getenv("ALPACA_API_KEY", ""), os.getenv("ALPACA_API_SECRET", ""))
        inputs = load_scan_inputs(database=database, universe_path=settings.universe_path)
        _journal_open_trade_daily_bars(database, inputs.bars_by_symbol)
        scanner = DeterministicSwingScanner(settings)
        candidates = scanner.scan(
            inputs.assets,
            inputs.bars_by_symbol,
            spy_bars=inputs.spy_bars,
            qqq_bars=inputs.qqq_bars,
            sector_bars=inputs.sector_bars,
            source="alpaca",
        )
        scan_run_id = str(uuid4())
        persist_scan_decisions(
            database,
            settings,
            run_id=scan_run_id,
            candidates=candidates,
            funnel_report=getattr(scanner, "last_report", type("EmptyFunnel", (), {"rejections": []})()),
        )
        backend = _backend_if_configured(database)
        pipeline = AgentPipeline(settings, database, backend) if backend else None
        proposals = pipeline.analyze(candidates) if pipeline else []
        candidates_by_ticker = {candidate.ticker: candidate for candidate in candidates}
        execution = SwingExecutionService(settings, database, broker)
        decisions = []
        for proposal in proposals:
            candidate = candidates_by_ticker.get(proposal.ticker)
            if candidate is None:
                continue
            result = execution.submit(proposal, candidate, dry_run=not settings.trading_enabled)
            decisions.append(
                {
                    "ticker": proposal.ticker,
                    "setup_type": proposal.setup_type.value,
                    "status": result.status,
                    "message": result.message,
                    "reason_code": result.risk.reason_code,
                    "trade_id": result.trade_id,
                }
            )
        print(
            json.dumps(
                {
                    "execution_mode": settings.execution_mode,
                    "trading_enabled": settings.trading_enabled,
                    "llm_backend_configured": backend is not None,
                    "candidates_found": len(candidates),
                    "proposals_generated": len(proposals),
                    "llm_calls": pipeline.last_cycle_metrics if pipeline else {"actual_calls": 0, "suppressed_calls": 0},
                    "funnel": {
                        **(scanner.last_report.as_dict() if hasattr(scanner, "last_report") else {}),
                        "risk_sizing": database.sizing_rejection_summary(cycle_started_at),
                    },
                    "decisions": decisions,
                },
                indent=2,
                default=str,
            )
        )
    elif args.command == "review-observations":
        results = review_observations(database, settings, ROOT / "reports")
        print(json.dumps(results, indent=2, default=str))
    elif args.command == "live-ticket":
        if settings.execution_mode != "live":
            raise ValueError("live-ticket requires EXECUTION_MODE=live, TRADING_ENABLED=true, and " "LIVE_TRADING_ACK=I_ACKNOWLEDGE_LIVE_RISK -- this computes a real-money order ticket")
        payload = json.loads(args.input.read_text(encoding="utf-8"))
        candidate = SwingCandidate.model_validate(payload["candidate"])
        proposal = TradeProposal.model_validate(payload["proposal"])
        broker = HumanSuppliedBrokerProvider(payload["broker_snapshot"])
        result = SwingExecutionService(settings, database, broker).submit(proposal, candidate, dry_run=True)
        print(result.model_dump_json(indent=2))
        if result.status == "DRY_RUN_APPROVED":
            quote = broker.get_quote(proposal.ticker)
            database.record_live_ticket_approval(
                decision_id=proposal.decision_id,
                intent_id=result.intent_id,
                candidate=candidate.model_dump(mode="json"),
                proposal=proposal.model_dump(mode="json"),
                risk=result.risk.model_dump(mode="json"),
                expires_at=(datetime.now(timezone.utc) + timedelta(seconds=settings.quote_freshness_seconds)).isoformat(),
            )
            print("\n--- APPROVED LIVE ORDER TICKET: execute this yourself. Nothing was sent to Robinhood. ---")
            print(
                json.dumps(
                    {
                        "ticker": proposal.ticker,
                        "side": "buy",
                        "quantity": result.risk.quantity,
                        "limit_price": round(quote.last, 2),
                        "stop_price": result.risk.authoritative_stop,
                        "target_price": candidate.resistance,
                        "planned_dollar_risk": result.risk.planned_dollar_risk,
                        "planned_account_risk_pct": result.risk.planned_account_risk_pct,
                    },
                    indent=2,
                )
            )
    elif args.command == "record-live-fill":
        if settings.execution_mode != "live":
            raise ValueError("record-live-fill requires EXECUTION_MODE=live")
        payload = json.loads(args.input.read_text(encoding="utf-8"))
        decision_id = str(payload["proposal"]["decision_id"])
        approval = database.consume_live_ticket_approval(decision_id)
        candidate = SwingCandidate.model_validate(approval["candidate_json"])
        proposal = TradeProposal.model_validate(approval["proposal_json"])
        risk = RiskDecision.model_validate(approval["risk_json"])
        fill = payload["fill"]
        if risk.authoritative_stop is None or not risk.approved:
            raise ValueError("Stored ticket has no approved authoritative risk decision")
        if int(fill["shares"]) != fill["shares"] or int(fill["shares"]) != risk.quantity:
            raise ValueError("Recorded fill quantity must exactly match the whole-share approved ticket")
        if fill.get("protective_status") != "active" or not fill.get("protective_stop_order_id") or not fill.get("target_order_id"):
            raise ValueError("Recorded live fill must prove active broker-native stop and target orders")
        actual_entry = float(fill["entry_price"])
        current_rr = (candidate.resistance - actual_entry) / (actual_entry - risk.authoritative_stop)
        if actual_entry > candidate.price + settings.maximum_entry_slippage_r * (candidate.price - risk.authoritative_stop) or current_rr < settings.minimum_rr:
            database.activate_halt("reconciliation_halt", "Manual live fill diverged from approved entry geometry", "record-live-fill")
            raise ValueError("Manual live fill exceeded approved slippage or no longer meets minimum R:R; trading halted")
        trade_id = str(uuid4())
        database.record_candidate(candidate)
        trade_values = {
            "trade_id": trade_id,
            "decision_id": proposal.decision_id,
            "candidate_id": candidate.candidate_id,
            "ticker": proposal.ticker,
            "setup_type": proposal.setup_type.value,
            "status": "OPEN",
            "strategy_version": proposal.strategy_version,
            "entry_datetime": fill["entry_datetime"],
            "entry_price": fill["entry_price"],
            "initial_stop": risk.authoritative_stop,
            "final_stop": risk.authoritative_stop,
            "target": candidate.resistance,
            "shares": fill["shares"],
            "position_value": fill["shares"] * fill["entry_price"],
            "planned_dollar_risk": (actual_entry - risk.authoritative_stop) * int(fill["shares"]),
            "planned_account_risk_pct": risk.planned_account_risk_pct,
            "planned_rr": current_rr,
            "market_regime": proposal.market_regime.value,
            "sector": candidate.sector,
            "candidate_score": candidate.score,
            "bull_thesis": proposal.bull_case,
            "bear_thesis": proposal.bear_case,
            "reason_entry": proposal.bull_case,
            "broker_provider": "robinhood-human-supervised",
            "broker_order_id": fill.get("broker_order_id"),
            "order_type": "manual-live",
            "risk_validation_result": risk.model_dump_json(),
        }
        thesis = TradeThesis(
            ticker=proposal.ticker,
            setup=proposal.setup_type,
            entry=actual_entry,
            initial_stop=risk.authoritative_stop,
            initial_target=candidate.resistance,
            initial_risk_dollars=(actual_entry - risk.authoritative_stop) * int(fill["shares"]),
            initial_risk_per_share=actual_entry - risk.authoritative_stop,
            initial_rr=current_rr,
            entry_score=candidate.score,
            market_regime=candidate.market_regime.value,
            expected_hold_days=settings.expected_hold_days,
            max_hold_days=settings.maximum_hold_days,
            technical_thesis=TechnicalThesis(trend=candidate.sector_trend, support=candidate.support, trigger=actual_entry),
            invalidations=(proposal.invalidation, "setup structure failure"),
        )
        database.add_trade_with_thesis(trade_values, thesis)
        lifecycle = PositionLifecycleService(settings, database)
        lifecycle.create_open(trade_id, current_stop=risk.authoritative_stop, trigger="human_recorded_live_fill")
        lifecycle.mark_protected(
            trade_id,
            evidence={
                "protective_stop_order_id": fill["protective_stop_order_id"],
                "target_order_id": fill["target_order_id"],
                "source": "human_verified",
            },
        )
        print(json.dumps({"trade_id": trade_id, "status": "recorded"}, indent=2))
    elif args.command == "record-live-exit":
        trade = database.get_trade(args.trade_id)
        if not trade:
            raise KeyError(args.trade_id)
        high, low = database.trade_price_extremes(args.trade_id, float(trade["entry_price"]))
        engine = PostmortemEngine(database, consecutive_loss_halt=settings.consecutive_loss_halt)
        lifecycle = PositionLifecycleService(settings, database)
        lifecycle.request_exit(args.trade_id, trigger="manual_safety_exit", reason_code="EXIT_MANUAL_SAFETY")
        record = engine.close_and_review(
            args.trade_id,
            exit_price=args.exit_price,
            high_during_trade=max(high, args.exit_price),
            low_during_trade=min(low, args.exit_price),
            reason_exit=args.reason,
        )
        lifecycle.mark_closed(args.trade_id, reason_code="CLOSED_MANUAL_RECONCILED", context={"exit_price": args.exit_price})
        print(record.model_dump_json(indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
