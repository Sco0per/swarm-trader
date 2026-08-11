from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.swing.config import SwingSettings
from src.swing.database import SwingDatabase
from src.swing.models import (
    AccountSnapshot, Decision, MarketRegime, PortfolioRiskState, Quote, SetupType,
    SwingCandidate, TimestampedData, TradeProposal,
)


@pytest.fixture
def settings(tmp_path):
    blacklist = tmp_path / "do_not_trade.yaml"
    blacklist.write_text("symbols: []\nsubstantially_identical: []\n", encoding="utf-8")
    return SwingSettings(database_path=tmp_path / "swing.db", do_not_trade_path=blacklist)


@pytest.fixture
def database(settings):
    database = SwingDatabase(settings.database_path)
    database.initialize(settings.strategy_version)
    return database


@pytest.fixture
def candidate():
    now = datetime.now(timezone.utc)
    return SwingCandidate(
        ticker="XYZ", setup_type=SetupType.TREND_PULLBACK, score=88,
        score_components={"market_regime": 15, "trend_quality": 15, "setup_quality": 18,
                          "relative_strength": 9, "volume_confirmation": 8, "entry_quality": 8,
                          "risk_reward": 8, "liquidity": 4, "event_risk": 3},
        market_regime=MarketRegime.BULL, sector="Technology", sector_etf="XLK", sector_trend="UP",
        sector_relative_strength=0.03, stock_relative_strength_spy=0.08,
        stock_relative_strength_sector=0.05, price=100, average_daily_volume=2_000_000,
        spread_pct=0.001, rsi=55, macd=1.2, atr=3, ma20=98, ma50=92, ma200=80,
        volume_ratio=0.9, support=96, resistance=110, earnings_trading_days=10,
        data=TimestampedData(source="test", retrieved_at=now, market_timestamp=now),
    )


@pytest.fixture
def proposal(candidate, settings):
    return TradeProposal(
        ticker=candidate.ticker, decision=Decision.BUY, setup_type=candidate.setup_type,
        entry=100, stop=96, target=108, confidence_score=88, candidate_score=candidate.score,
        market_regime=candidate.market_regime, bull_case="trend and support", bear_case="market weakness",
        invalidation="close below support", event_risk="earnings beyond exclusion window",
        strategy_version=settings.strategy_version,
    )


@pytest.fixture
def quote():
    now = datetime.now(timezone.utc)
    return Quote(symbol="XYZ", last=100, bid=99.95, ask=100.05, source="test", retrieved_at=now, market_timestamp=now)


@pytest.fixture
def portfolio():
    account = AccountSnapshot(account_id="paper", equity=2_000, cash=2_000, buying_power=2_000, is_paper=True)
    return PortfolioRiskState(account=account, equity_high=2_000)
