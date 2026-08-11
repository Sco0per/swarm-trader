"""Deterministic daily scanner, regime engine, and relative-strength context."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Iterable, Mapping

import numpy as np
import pandas as pd

from .config import SwingSettings
from .models import MarketRegime, SetupType, SwingCandidate, TimestampedData
from .risk import LEVERAGED_OR_INVERSE_ETFS
from .strategy import score_entry, score_inputs_from_validation, validate_setup
from .universe import UNIVERSE_VERSION


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
    asset_class: str = "us_equity"
    security_type: str = "common_stock"
    is_tradable: bool | None = True
    is_halted: bool | None = False
    halt_status_known: bool = True
    broker_restricted: bool = False
    existing_holding: bool = False
    is_leveraged_or_inverse: bool = False
    earnings_trading_days: int | None = None
    prohibited_event_risk: bool | None = False
    bid: float | None = None
    ask: float | None = None
    quote_timestamp: datetime | None = None


@dataclass(frozen=True)
class FunnelRejection:
    symbol: str
    stage: str
    reason_code: str
    details: tuple[str, ...] = ()


@dataclass
class ScanFunnelReport:
    universe_version: str = UNIVERSE_VERSION
    scanned: int = 0
    passed_liquidity_history: int = 0
    trend_rs_eligible: int = 0
    setup_matches: int = 0
    strong_candidates: int = 0
    rejection_counts: Counter[str] = field(default_factory=Counter)
    rejections: list[FunnelRejection] = field(default_factory=list)
    result: str = "NO_TRADE"

    def reject(self, symbol: str, stage: str, reason_code: str, details: tuple[str, ...] = ()) -> None:
        self.rejection_counts[reason_code] += 1
        self.rejections.append(FunnelRejection(symbol, stage, reason_code, details))

    def as_dict(self) -> dict:
        return {
            "universe_version": self.universe_version,
            "scanned": self.scanned,
            "passed_liquidity_history": self.passed_liquidity_history,
            "trend_rs_eligible": self.trend_rs_eligible,
            "setup_matches": self.setup_matches,
            "strong_candidates": self.strong_candidates,
            "rejection_counts": dict(sorted(self.rejection_counts.items())),
            "rejections": [rejection.__dict__ for rejection in self.rejections],
            "result": self.result,
        }


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

    def __init__(self, settings: SwingSettings):
        self.settings = settings
        self.regime_engine = MarketRegimeEngine()
        from .config import load_symbol_blacklist

        self.blacklist = load_symbol_blacklist(settings.do_not_trade_path)
        self.last_report = ScanFunnelReport()

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
        candidates, report = self.scan_with_report(
            assets,
            bars_by_symbol,
            spy_bars=spy_bars,
            qqq_bars=qqq_bars,
            sector_bars=sector_bars,
            source=source,
            retrieved_at=retrieved_at,
        )
        self.last_report = report
        return candidates

    def scan_with_report(
        self,
        assets: Iterable[UniverseAsset],
        bars_by_symbol: Mapping[str, pd.DataFrame | list[dict]],
        *,
        spy_bars: pd.DataFrame | list[dict],
        qqq_bars: pd.DataFrame | list[dict],
        sector_bars: Mapping[str, pd.DataFrame | list[dict]] | None = None,
        source: str = "unknown",
        retrieved_at: datetime | None = None,
    ) -> tuple[list[SwingCandidate], ScanFunnelReport]:
        retrieved_at = retrieved_at or datetime.now(timezone.utc)
        regime, _ = self.regime_engine.classify(spy_bars, qqq_bars)
        sector_bars = sector_bars or {}
        results: list[SwingCandidate] = []
        report = ScanFunnelReport()
        for asset in assets:
            symbol = asset.symbol.upper()
            report.scanned += 1
            exclusion = self._exclusion_reason(asset, symbol)
            if exclusion:
                report.reject(symbol, "exclusion", exclusion)
                continue
            raw = bars_by_symbol.get(symbol)
            if raw is None:
                report.reject(symbol, "liquidity_history", "bars_unavailable")
                continue
            try:
                frame = _frame(raw)
            except (TypeError, ValueError):
                report.reject(symbol, "liquidity_history", "bar_data_malformed")
                continue
            if frame[["open", "high", "low", "close", "volume"]].isna().any(axis=None):
                report.reject(symbol, "liquidity_history", "bar_data_incomplete")
                continue
            if not isinstance(frame.index, pd.DatetimeIndex) or frame.index.has_duplicates or not frame.index.is_monotonic_increasing:
                report.reject(symbol, "liquidity_history", "bar_timestamps_invalid")
                continue
            if len(frame) < self.settings.minimum_history_sessions:
                report.reject(symbol, "liquidity_history", "insufficient_history")
                continue
            market_timestamp = frame.index[-1].to_pydatetime()
            if market_timestamp.tzinfo is None:
                market_timestamp = market_timestamp.replace(tzinfo=timezone.utc)
            age = retrieved_at.astimezone(timezone.utc) - market_timestamp.astimezone(timezone.utc)
            if age < -timedelta(days=1) or age > timedelta(days=self.settings.strategy.maximum_bar_age_calendar_days):
                report.reject(symbol, "liquidity_history", "bar_data_stale")
                continue
            close = frame["close"]
            price = float(close.iloc[-1])
            liquidity_window = self.settings.strategy.short_ema_period
            adv20 = float(frame["volume"].tail(liquidity_window).mean())
            average_dollar_volume = float((frame["close"] * frame["volume"]).tail(liquidity_window).mean())
            if price <= self.settings.minimum_price:
                report.reject(symbol, "liquidity_history", "minimum_price")
                continue
            if adv20 <= self.settings.minimum_average_volume:
                report.reject(symbol, "liquidity_history", "minimum_average_volume")
                continue
            if average_dollar_volume <= self.settings.minimum_average_dollar_volume:
                report.reject(symbol, "liquidity_history", "minimum_average_dollar_volume")
                continue
            if asset.bid is None or asset.ask is None or asset.bid <= 0 or asset.ask <= asset.bid:
                report.reject(symbol, "liquidity_history", "spread_unavailable")
                continue
            if asset.quote_timestamp is not None:
                quote_timestamp = asset.quote_timestamp
                if quote_timestamp.tzinfo is None:
                    report.reject(symbol, "liquidity_history", "quote_timestamp_invalid")
                    continue
                quote_age = retrieved_at.astimezone(timezone.utc) - quote_timestamp.astimezone(timezone.utc)
                if quote_age.total_seconds() < 0 or quote_age > timedelta(seconds=self.settings.quote_freshness_seconds):
                    report.reject(symbol, "liquidity_history", "quote_stale")
                    continue
            spread = (asset.ask - asset.bid) / ((asset.ask + asset.bid) / 2)
            if spread >= self.settings.maximum_spread_pct:
                report.reject(symbol, "liquidity_history", "maximum_spread")
                continue
            report.passed_liquidity_history += 1
            medium = sma(close, self.settings.strategy.medium_ma_period)
            stock_rs = roc(close, self.settings.strategy.relative_strength_lookback) - roc(
                _frame(spy_bars)["close"], self.settings.strategy.relative_strength_lookback
            )
            if medium is None or not (
                price > medium
                and (
                    _slope(close, self.settings.strategy.medium_ma_period, self.settings.strategy.trend_slope_lookback)
                    > self.settings.strategy.minimum_trend_slope
                    or stock_rs >= self.settings.strategy.minimum_spy_relative_strength
                )
            ):
                report.reject(symbol, "trend_rs", "trend_rs_ineligible")
                continue
            report.trend_rs_eligible += 1
            sector_etf = SECTOR_ETFS.get(asset.sector)
            sector_raw = sector_bars.get(sector_etf) if sector_etf else None
            if asset.prohibited_event_risk is None:
                event_risk = None
            elif asset.prohibited_event_risk:
                event_risk = True
            else:
                event_risk = (
                    False
                    if asset.is_etf
                    else None
                    if asset.earnings_trading_days is None
                    else asset.earnings_trading_days <= self.settings.earnings_exclusion_trading_days
                )
            validations = [
                validate_setup(
                    setup_type,
                    frame,
                    _frame(spy_bars),
                    sector_bars=_frame(sector_raw) if sector_raw is not None else None,
                    settings=self.settings,
                    as_of=retrieved_at,
                    event_risk_prohibited=event_risk,
                    liquidity_acceptable=True,
                    market_regime=regime,
                )
                for setup_type in SetupType
            ]
            passing = [item for item in validations if item.passed]
            if not passing:
                report.reject(
                    symbol,
                    "setup",
                    "setup_no_match",
                    tuple(f"{item.setup_type.value}:{item.primary_reason}" for item in validations),
                )
                continue
            report.setup_matches += 1
            scored_validations = [
                (
                    item,
                    score_entry(
                        score_inputs_from_validation(item, regime, self.settings.strategy),
                        weights=self.settings.score_weights,
                        settings=self.settings,
                        validator_passed=True,
                        regime=regime,
                    ),
                )
                for item in passing
            ]
            validation, score = max(scored_validations, key=lambda pair: (pair[1].total, pair[0].setup_type.value))
            candidate = self._candidate(
                asset,
                frame,
                sector_raw,
                regime,
                source,
                retrieved_at,
                spread,
                validation,
                score,
            )
            results.append(candidate)
            if score.route in {"strong", "very_strong"}:
                report.strong_candidates += 1
        results.sort(key=lambda item: (-item.score, item.ticker))
        results = results[: self.settings.maximum_scanner_candidates]
        report.result = "CANDIDATES_AVAILABLE" if report.strong_candidates else "NO_TRADE"
        self.last_report = report
        return results, report

    def _exclusion_reason(self, asset: UniverseAsset, symbol: str) -> str | None:
        if symbol in self.blacklist:
            return "blacklisted_symbol"
        if asset.is_leveraged_or_inverse or symbol in LEVERAGED_OR_INVERSE_ETFS:
            return "leveraged_or_inverse_etf"
        if asset.asset_class != "us_equity" or asset.security_type not in {"common_stock", "etf"}:
            return "unsupported_security_type"
        if asset.is_tradable is not True or asset.broker_restricted:
            return "broker_restricted_or_untradable"
        if not asset.halt_status_known:
            return "halt_status_unknown"
        if asset.is_halted is not False:
            return "trading_halt"
        if asset.existing_holding:
            return "existing_holding"
        return None

    def _candidate(
        self,
        asset: UniverseAsset,
        frame: pd.DataFrame,
        sector_raw: pd.DataFrame | list[dict] | None,
        regime: MarketRegime,
        source: str,
        retrieved_at: datetime,
        spread: float,
        validation,
        score,
    ) -> SwingCandidate:
        close = frame["close"]
        price = float(close.iloc[-1])
        ma20_raw = ema(close, self.settings.strategy.short_ema_period)
        ma50_raw = sma(close, self.settings.strategy.medium_ma_period)
        ma200_raw = sma(close, self.settings.strategy.long_ma_period)
        atr_raw = atr(frame, self.settings.strategy.atr_period)
        assert ma20_raw is not None and ma50_raw is not None and ma200_raw is not None and atr_raw is not None
        ma20, ma50, ma200, atr14 = float(ma20_raw), float(ma50_raw), float(ma200_raw), float(atr_raw)
        volume_ratio = float(frame["volume"].iloc[-1] / frame["volume"].tail(self.settings.strategy.short_ema_period).mean())
        sector_etf = SECTOR_ETFS.get(asset.sector)
        sector_frame = _frame(sector_raw) if sector_raw is not None else None
        sector_rs = float(validation.features.get("sector_rs_spy") or 0)
        stock_rs = float(validation.features.get("stock_rs_spy") or 0)
        stock_sector_rs = validation.features.get("stock_rs_sector")
        sector_trend = "UP" if sector_frame is not None and sector_frame["close"].iloc[-1] > sector_frame["close"].rolling(self.settings.strategy.medium_ma_period).mean().iloc[-1] else "UNKNOWN"
        adv = float(frame["volume"].tail(self.settings.strategy.short_ema_period).mean())
        market_timestamp = None
        if isinstance(frame.index, pd.DatetimeIndex) and len(frame.index):
            stamp = frame.index[-1]
            market_timestamp = stamp.to_pydatetime()
            if market_timestamp.tzinfo is None:
                market_timestamp = market_timestamp.replace(tzinfo=timezone.utc)
        market_timestamp = market_timestamp or retrieved_at
        return SwingCandidate(
            ticker=asset.symbol,
            setup_type=validation.setup_type,
            score=score.total,
            score_components=dict(score.components),
            score_route=score.route,
            validator_features=dict(validation.features),
            validator_failures=list(validation.failed_conditions),
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
            support=float(validation.provisional_stop),
            resistance=float(validation.target),
            earnings_trading_days=asset.earnings_trading_days,
            is_etf=asset.is_etf,
            is_leveraged_or_inverse=asset.is_leveraged_or_inverse,
            is_halted=bool(asset.is_halted),
            data=TimestampedData(source=source, retrieved_at=retrieved_at, market_timestamp=market_timestamp),
        )
