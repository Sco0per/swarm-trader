from __future__ import annotations

import json
import types
from datetime import datetime, timezone

from src.swing import cli as cli_module
from src.swing.brokers.fake import FakeBrokerProvider
from src.swing.models import (
    MarketRegime,
    Quote,
    SetupType,
    SwingCandidate,
    TimestampedData,
)
from src.swing.notification_channels import FakeNotificationChannel


def _make_candidate(ticker: str) -> SwingCandidate:
    now = datetime.now(timezone.utc)
    return SwingCandidate(
        ticker=ticker,
        setup_type=SetupType.TREND_PULLBACK,
        score=88,
        score_components={"market_regime": 15, "trend_quality": 15, "setup_quality": 18, "relative_strength": 9, "volume_confirmation": 8, "entry_quality": 8, "risk_reward": 8, "liquidity": 4, "event_risk": 3},
        market_regime=MarketRegime.BULL,
        sector="Technology",
        sector_etf="XLK",
        sector_trend="UP",
        sector_relative_strength=0.03,
        stock_relative_strength_spy=0.08,
        stock_relative_strength_sector=0.05,
        price=100,
        average_daily_volume=2_000_000,
        spread_pct=0.001,
        rsi=55,
        macd=1.2,
        atr=3,
        ma20=98,
        ma50=92,
        ma200=80,
        volume_ratio=0.9,
        support=96,
        resistance=110,
        structural_invalidation=96,
        earnings_trading_days=30,
        earnings_data_status="clear",
        earnings_retrieved_at=now,
        major_event_status="clear",
        major_event_retrieved_at=now,
        data=TimestampedData(source="test", retrieved_at=now, market_timestamp=now),
    )


class _StubScanner:
    def __init__(self, settings, candidates):
        self.settings = settings
        self._candidates = candidates

    def scan(self, *args, **kwargs):
        return list(self._candidates)


class _StubBackend:
    """Always proposes BUY for whatever candidate it is given."""

    def complete(self, *, role, model_name, payload, schema):
        candidate = payload["candidate"]
        if role == "technical":
            kwargs = dict(
                trend_structure="steady uptrend",
                setup_valid=True,
                support_resistance="holding above support",
                volume_analysis="in line",
                invalidation="close below support",
            )
        elif role == "fundamental":
            kwargs = dict(
                earnings_trading_days=candidate.get("earnings_trading_days") or 30,
                has_blocking_event_risk=False,
                fundamental_context="no blocking events",
            )
        elif role == "bear":
            kwargs = dict(
                kill_trade=False,
                is_extended_from_support=False,
                is_chasing_price=False,
                regime_is_weak=False,
                sector_is_weakening=False,
                volume_is_unconvincing=False,
                support_is_weak=False,
                target_is_unrealistic=False,
                earnings_too_close=False,
                repeats_known_bad_pattern=False,
                weaknesses=[],
                thesis="risk is manageable",
                confidence=75,
            )
        elif role == "portfolio_manager":
            kwargs = dict(
                ticker=candidate["ticker"],
                decision="APPROVE",
                setup_type=candidate["setup_type"],
                confidence_score=85,
                market_regime=candidate["market_regime"],
                bull_case="trend",
                bear_case="manageable risk",
                invalidation="close below support",
                event_risk="none nearby",
                reasoning="setup qualifies",
            )
        else:
            raise AssertionError(f"unexpected role {role}")
        return schema(**kwargs)


def _fake_broker() -> FakeBrokerProvider:
    now = datetime.now(timezone.utc)
    broker = FakeBrokerProvider(
        equity=2_000,
        cash=2_000,
        quotes={
            "AAA": Quote(symbol="AAA", last=100, bid=99.95, ask=100.05, source="test", retrieved_at=now, market_timestamp=now),
            "BBB": Quote(symbol="BBB", last=100, bid=99.95, ask=100.05, source="test", retrieved_at=now, market_timestamp=now),
        },
    )
    return broker


def _run_cli(monkeypatch, tmp_path, capsys, *, argv, env, candidates, backend, telegram_channel=None):
    monkeypatch.setattr(
        cli_module,
        "load_scan_inputs",
        lambda **kwargs: types.SimpleNamespace(
            assets=[],
            bars_by_symbol={},
            spy_bars=None,
            qqq_bars=None,
            sector_bars={},
        ),
    )
    monkeypatch.setattr(cli_module, "DeterministicSwingScanner", lambda settings: _StubScanner(settings, candidates))
    broker = _fake_broker()
    monkeypatch.setattr(cli_module, "AlpacaPaperProvider", lambda key, secret: broker)
    monkeypatch.setattr(cli_module, "AnthropicStructuredBackend", lambda database: backend)
    monkeypatch.setenv("SWING_DATABASE_PATH", str(tmp_path / "swing.db"))
    if telegram_channel is not None:
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "test-chat")
        monkeypatch.setattr(cli_module, "TelegramChannel", lambda *args, **kwargs: telegram_channel)
    else:
        # Guard against a developer's real TELEGRAM_* env vars leaking into
        # these tests and causing main()'s notification drain to attempt a
        # real network call.
        monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
        monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr("sys.argv", ["swing-trader", *argv])
    cli_module.main()
    return json.loads(capsys.readouterr().out)


def test_run_without_configured_backend_produces_no_proposals(monkeypatch, tmp_path, capsys):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    payload = _run_cli(
        monkeypatch,
        tmp_path,
        capsys,
        argv=["run"],
        env={"TRADING_ENABLED": "false"},
        candidates=[_make_candidate("AAA")],
        backend=_StubBackend(),
    )
    assert payload["llm_backend_configured"] is False
    assert payload["proposals_generated"] == 0
    assert payload["decisions"] == []


def test_run_with_backend_respects_one_trade_per_day_limit(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    payload = _run_cli(
        monkeypatch,
        tmp_path,
        capsys,
        argv=["run"],
        env={
            "TRADING_ENABLED": "true",
            "ANTHROPIC_API_KEY": "test-key",
            "ANALYST_MODEL": "claude-sonnet-5",
            "PORTFOLIO_MANAGER_MODEL": "claude-opus-5",
        },
        candidates=[_make_candidate("AAA"), _make_candidate("BBB")],
        backend=_StubBackend(),
    )
    assert payload["llm_backend_configured"] is True
    assert payload["proposals_generated"] == 2
    statuses = {decision["ticker"]: decision["status"] for decision in payload["decisions"]}
    assert list(statuses.values()).count("SUBMITTED") == 1
    assert "REJECTED" in statuses.values()


def test_run_with_no_proposals_notifies_no_trade_found(monkeypatch, tmp_path, capsys):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    fake_channel = FakeNotificationChannel()
    _run_cli(
        monkeypatch,
        tmp_path,
        capsys,
        argv=["run"],
        env={"TRADING_ENABLED": "false"},
        candidates=[_make_candidate("AAA")],
        backend=_StubBackend(),
        telegram_channel=fake_channel,
    )
    assert any("No trade found" in message for message in fake_channel.sent)


def test_run_with_submitted_trade_notifies_trade_entered(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    fake_channel = FakeNotificationChannel()
    payload = _run_cli(
        monkeypatch,
        tmp_path,
        capsys,
        argv=["run"],
        env={
            "TRADING_ENABLED": "true",
            "ANTHROPIC_API_KEY": "test-key",
            "ANALYST_MODEL": "claude-sonnet-5",
            "PORTFOLIO_MANAGER_MODEL": "claude-opus-5",
        },
        candidates=[_make_candidate("AAA"), _make_candidate("BBB")],
        backend=_StubBackend(),
        telegram_channel=fake_channel,
    )
    submitted_ticker = next(decision["ticker"] for decision in payload["decisions"] if decision["status"] == "SUBMITTED")
    assert any("Trade entered" in message and submitted_ticker in message for message in fake_channel.sent)


def test_scan_records_candidates_without_any_llm_call(monkeypatch, tmp_path, capsys):
    class _ExplodingBackend:
        def complete(self, **kwargs):
            raise AssertionError("scan must never call the LLM backend")

    payload = _run_cli(
        monkeypatch,
        tmp_path,
        capsys,
        argv=["scan"],
        env={},
        candidates=[_make_candidate("AAA")],
        backend=_ExplodingBackend(),
    )
    assert payload["candidates_found"] == 1
    assert payload["candidates"][0]["ticker"] == "AAA"
