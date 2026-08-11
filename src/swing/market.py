"""Deterministic daily scanner, regime engine, and relative-strength context."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, Mapping

import numpy as np
import pandas as pd

from .config import SwingSettings
from .models import MarketRegime, SetupType, SwingCandidate, TimestampedData
from .risk import LEVERAGED_OR_INVERSE_ETFS


SECTOR_ETFS = {
    "Communication Services": "XLC",
    "Consumer Discretionary": "XLY",
    "Consumer Staples": "XLP",
    "Energy": "XLE",
    "Financials": "XLF",
    "Health Care": "XLV",
    "Industrials": "XLI",
    "Materials": "XLB",
    "Real Estate": "XLRE",
    "Technology": "XLK",
    "Utilities": "XLU",
}


@dataclass(frozen=True)
class UniverseAsset:
    symbol: str
    sector: str = "Unknown"
    is_etf: bool = False
    is_tradable: bool = True
    is_halted: bool = False
    is_leveraged_or_inverse: bool = False
    earnings_trading_days: int | None = None
    bid: float | None = None
    ask: float | None = None


def _frame(raw: pd.DataFrame | list[dict]) -> pd.DataFrame:
    frame: pd.DataFrame
    if isinstance(raw, pd.DataFrame):
        frame = raw.copy()
    else:
        frame = pd.DataFrame(raw)
    frame.columns = [str(column).lower() for column in frame.columns]
    required = {"open", "high", "low", "close", "volume"}
    if not required.issubset(frame.columns):
        raise ValueError(f"OHLCV data missing {sorted(required - set(frame.columns))}")
    frame = frame.dropna(subset=list(required)).sort_index()
    return frame.astype({name: float for name in required})


def sma(values: pd.Series, period: int) -> float | None:
    return float(values.rolling(period).mean().iloc[-1]) if len(values) >= period else None


def ema(values: pd.Series, period: int) -> float | None:
    return float(values.ewm(span=period, adjust=False).mean().iloc[-1]) if len(values) >= period else None


def atr(frame: pd.DataFrame, period: int = 14) -> float | None:
    if len(frame) < period + 1:
        return None
    previous = frame["close"].shift(1)
    true_range = pd.concat(
        [(frame["high"] - frame["low"]), (frame["high"] - previous).abs(), (frame["low"] - previous).abs()], axis=1
    ).max(axis=1)
    return float(true_range.rolling(period).mean().iloc[-1])


def rsi(values: pd.Series, period: int = 14) -> float | None:
    if len(values) < period + 1:
        return None
    delta = values.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    ratio = gain / loss.replace(0, np.nan)
    result = 100 - (100 / (1 + ratio))
    value = result.iloc[-1]
    return 100.0 if np.isnan(value) and gain.iloc[-1] > 0 else (float(value) if not np.isnan(value) else 50.0)


def macd(values: pd.Series) -> float | None:
    if len(values) < 26:
        return None
    return float(values.ewm(span=12, adjust=False).mean().iloc[-1] - values.ewm(span=26, adjust=False).mean().iloc[-1])


def roc(values: pd.Series, period: int) -> float:
    if len(values) <= period or values.iloc[-period - 1] == 0:
        return 0.0
    return float(values.iloc[-1] / values.iloc[-period - 1] - 1)


def _slope(values: pd.Series, period: int, lookback: int = 5) -> float:
    rolling = values.rolling(period).mean()
    if len(values) < period + lookback or rolling.iloc[-lookback - 1] == 0:
        return 0.0
    return float(rolling.iloc[-1] / rolling.iloc[-lookback - 1] - 1)


class MarketRegimeEngine:
    """Classify daily market structure without relying on macro prose."""

    def classify(self, spy_raw: pd.DataFrame | list[dict], qqq_raw: pd.DataFrame | list[dict]) -> tuple[MarketRegime, dict]:
        spy, qqq = _frame(spy_raw), _frame(qqq_raw)
        if min(len(spy), len(qqq)) < 200:
            return MarketRegime.NEUTRAL, {"reason": "insufficient_200_day_history"}
        spy_close, qqq_close = spy["close"], qqq["close"]
        inputs = {
            "spy_close": float(spy_close.iloc[-1]),
            "spy_ma20": sma(spy_close, 20),
            "spy_ma50": sma(spy_close, 50),
            "spy_ma200": sma(spy_close, 200),
            "qqq_close": float(qqq_close.iloc[-1]),
            "qqq_ma20": sma(qqq_close, 20),
            "qqq_ma50": sma(qqq_close, 50),
            "qqq_ma200": sma(qqq_close, 200),
            "spy_slope20": _slope(spy_close, 20),
            "spy_slope50": _slope(spy_close, 50),
            "qqq_slope20": _slope(qqq_close, 20),
            "volatility20": float(spy_close.pct_change().tail(20).std(ddof=1) * np.sqrt(252)),
        }
        if inputs["volatility20"] >= 0.35 or roc(spy_close, 5) <= -0.07:
            regime = MarketRegime.HIGH_VOLATILITY_RISK_OFF
        elif inputs["spy_close"] < inputs["spy_ma50"] < inputs["spy_ma200"] and inputs["spy_slope50"] < 0:
            regime = MarketRegime.BEAR
        else:
            spy_aligned = inputs["spy_close"] > inputs["spy_ma20"] > inputs["spy_ma50"] > inputs["spy_ma200"]
            qqq_aligned = inputs["qqq_close"] > inputs["qqq_ma20"] > inputs["qqq_ma50"] > inputs["qqq_ma200"]
            if spy_aligned and qqq_aligned and inputs["spy_slope20"] > 0 and inputs["qqq_slope20"] > 0:
                regime = MarketRegime.STRONG_BULL
            elif inputs["spy_close"] > inputs["spy_ma50"] and inputs["qqq_close"] > inputs["qqq_ma50"] and inputs["spy_slope50"] >= 0:
                regime = MarketRegime.BULL
            elif abs(inputs["spy_slope20"]) < 0.005 and abs(roc(spy_close, 20)) < 0.03:
                regime = MarketRegime.CHOPPY
            else:
                regime = MarketRegime.NEUTRAL
        return regime, inputs


class DeterministicSwingScanner:
    """Reduce a broad liquid universe to ranked swing candidates before LLM use."""

    WEIGHTS = {
        "market_regime": 15.0,
        "trend_quality": 15.0,
        "setup_quality": 20.0,
        "relative_strength": 10.0,
        "volume_confirmation": 10.0,
        "entry_quality": 10.0,
        "risk_reward": 10.0,
        "liquidity": 5.0,
        "event_risk": 5.0,
    }

    def __init__(self, settings: SwingSettings):
        self.settings = settings
        self.regime_engine = MarketRegimeEngine()

    def scan(
        self,
        assets: Iterable[UniverseAsset],
        bars_by_symbol: Mapping[str, pd.DataFrame | list[dict]],
        *,
        spy_bars: pd.DataFrame | list[dict],
        qqq_bars: pd.DataFrame | list[dict],
        sector_bars: Mapping[str, pd.DataFrame | list[dict]] | None = None,
        source: str = "unknown",
        retrieved_at: datetime | None = None,
    ) -> list[SwingCandidate]:
        retrieved_at = retrieved_at or datetime.now(timezone.utc)
        regime, _ = self.regime_engine.classify(spy_bars, qqq_bars)
        spy = _frame(spy_bars)
        sector_bars = sector_bars or {}
        results: list[SwingCandidate] = []
        for asset in assets:
            symbol = asset.symbol.upper()
            if not asset.is_tradable or asset.is_halted or asset.is_leveraged_or_inverse or symbol in LEVERAGED_OR_INVERSE_ETFS:
                continue
            raw = bars_by_symbol.get(symbol)
            if raw is None:
                continue
            try:
                frame = _frame(raw)
            except (TypeError, ValueError):
                continue
            if len(frame) < 200:
                continue
            close = frame["close"]
            price = float(close.iloc[-1])
            adv20 = float(frame["volume"].tail(20).mean())
            if price <= self.settings.minimum_price or adv20 <= self.settings.minimum_average_volume:
                continue
            spread = None
            if asset.bid and asset.ask and asset.bid > 0:
                spread = (asset.ask - asset.bid) / ((asset.ask + asset.bid) / 2)
                if spread > self.settings.maximum_spread_pct:
                    continue
            candidate = self._score(asset, frame, spy, sector_bars, regime, source, retrieved_at, spread)
            if candidate is not None:
                results.append(candidate)
        results.sort(key=lambda item: (-item.score, item.ticker))
        return results[: self.settings.maximum_scanner_candidates]

    def _score(
        self, asset: UniverseAsset, frame: pd.DataFrame, spy: pd.DataFrame,
        sector_bars: Mapping[str, pd.DataFrame | list[dict]], regime: MarketRegime,
        source: str, retrieved_at: datetime, spread: float | None,
    ) -> SwingCandidate | None:
        close = frame["close"]
        price = float(close.iloc[-1])
        ma20_raw, ma50_raw, ma200_raw = sma(close, 20), sma(close, 50), sma(close, 200)
        atr_raw = atr(frame)
        if ma20_raw is None or ma50_raw is None or ma200_raw is None or atr_raw is None:
            return None
        ma20, ma50, ma200, atr14 = float(ma20_raw), float(ma50_raw), float(ma200_raw), float(atr_raw)
        if min(ma20, ma50, ma200, atr14) <= 0:
            return None
        volume_ratio = float(frame["volume"].iloc[-1] / frame["volume"].tail(20).mean())
        stock_rs = roc(close, 63) - roc(spy["close"], 63)
        sector_etf = SECTOR_ETFS.get(asset.sector)
        sector_frame = None
        if sector_etf and sector_etf in sector_bars:
            try:
                sector_frame = _frame(sector_bars[sector_etf])
            except ValueError:
                sector_frame = None
        sector_rs = roc(sector_frame["close"], 63) - roc(spy["close"], 63) if sector_frame is not None else 0.0
        stock_sector_rs = roc(close, 63) - roc(sector_frame["close"], 63) if sector_frame is not None else None
        sector_trend = "UP" if sector_frame is not None and sector_frame["close"].iloc[-1] > sector_frame["close"].rolling(50).mean().iloc[-1] else "UNKNOWN"

        recent_high = float(frame["high"].iloc[-26:-6].max())
        recent_low = float(frame["low"].tail(10).min())
        pullback_distance = min(abs(price - ma20), abs(price - ma50)) / atr14
        ma50_rising = _slope(close, 50) > 0
        ma200_rising = _slope(close, 200) > 0
        bullish_confirmation = close.iloc[-1] > close.iloc[-2]
        breakout_seen = bool((frame["close"].iloc[-6:-1] > recent_high).any())
        successful_retest = price >= recent_high * 0.99 and price <= recent_high + atr14 and bullish_confirmation
        five_day_range = float(frame["high"].tail(5).max() - frame["low"].tail(5).min())

        if price > ma50 and ma50_rising and pullback_distance <= 1.25 and roc(close, 5) <= 0.03 and bullish_confirmation:
            setup = SetupType.TREND_PULLBACK
            setup_quality = 0.8 + (0.2 if volume_ratio <= 1.0 else 0)
            support = max(recent_low, ma50 - 0.25 * atr14)
        elif breakout_seen and successful_retest and price > ma50:
            setup = SetupType.BREAKOUT_RETEST
            setup_quality = 0.85 + (0.15 if volume_ratio >= 1.1 else 0)
            support = min(recent_high - 0.25 * atr14, float(frame["low"].tail(3).min()))
        elif price > ma20 > ma50 and stock_rs > 0 and sector_rs >= 0 and five_day_range <= 3 * atr14 and bullish_confirmation:
            setup = SetupType.RELATIVE_STRENGTH_CONTINUATION
            setup_quality = 0.8 + (0.2 if stock_rs > 0.05 else 0)
            support = min(ma20, float(frame["low"].tail(5).min()))
        else:
            return None

        support = min(support, price - 0.25 * atr14)
        resistance = max(float(frame["high"].tail(63).max()), price + 2 * (price - support))
        rr = (resistance - price) / (price - support) if price > support else 0
        regime_fraction = {
            MarketRegime.STRONG_BULL: 1.0, MarketRegime.BULL: 0.85, MarketRegime.NEUTRAL: 0.55,
            MarketRegime.CHOPPY: 0.30, MarketRegime.BEAR: 0.0, MarketRegime.HIGH_VOLATILITY_RISK_OFF: 0.0,
        }[regime]
        trend_fraction = np.mean([
            price > ma20, price > ma50, price > ma200, ma50_rising, ma200_rising,
        ])
        rs_fraction = float(np.clip(0.5 + stock_rs * 5 + sector_rs * 2, 0, 1))
        volume_fraction = float(np.clip(1 - abs(volume_ratio - 1.25) / 1.25, 0, 1))
        entry_fraction = float(np.clip(1 - max(pullback_distance - 0.25, 0) / 2, 0, 1))
        rr_fraction = float(np.clip(rr / 2, 0, 1))
        adv = float(frame["volume"].tail(20).mean())
        liquidity_fraction = float(np.clip((adv - 1_000_000) / 4_000_000, 0, 1))
        if spread is not None:
            liquidity_fraction = min(liquidity_fraction, float(np.clip(1 - spread / self.settings.maximum_spread_pct, 0, 1)))
        event_fraction = 1.0 if asset.is_etf or (asset.earnings_trading_days is not None and asset.earnings_trading_days > 5) else 0.0
        fractions = {
            "market_regime": regime_fraction,
            "trend_quality": trend_fraction,
            "setup_quality": setup_quality,
            "relative_strength": rs_fraction,
            "volume_confirmation": volume_fraction,
            "entry_quality": entry_fraction,
            "risk_reward": rr_fraction,
            "liquidity": liquidity_fraction,
            "event_risk": event_fraction,
        }
        components = {name: round(self.WEIGHTS[name] * float(fraction), 4) for name, fraction in fractions.items()}
        score = round(sum(components.values()), 4)
        market_timestamp = None
        if isinstance(frame.index, pd.DatetimeIndex) and len(frame.index):
            stamp = frame.index[-1]
            market_timestamp = stamp.to_pydatetime()
            if market_timestamp.tzinfo is None:
                market_timestamp = market_timestamp.replace(tzinfo=timezone.utc)
        market_timestamp = market_timestamp or retrieved_at
        return SwingCandidate(
            ticker=asset.symbol,
            setup_type=setup,
            score=score,
            score_components=components,
            market_regime=regime,
            sector=asset.sector,
            sector_etf=sector_etf,
            sector_trend=sector_trend,
            sector_relative_strength=sector_rs,
            stock_relative_strength_spy=stock_rs,
            stock_relative_strength_sector=stock_sector_rs,
            price=price,
            average_daily_volume=adv,
            spread_pct=spread,
            rsi=rsi(close),
            macd=macd(close),
            atr=atr14,
            ma20=ma20,
            ma50=ma50,
            ma200=ma200,
            volume_ratio=volume_ratio,
            support=support,
            resistance=resistance,
            earnings_trading_days=asset.earnings_trading_days,
            is_etf=asset.is_etf,
            is_leveraged_or_inverse=asset.is_leveraged_or_inverse,
            is_halted=asset.is_halted,
            data=TimestampedData(source=source, retrieved_at=retrieved_at, market_timestamp=market_timestamp),
        )
