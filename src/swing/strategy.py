"""Pure deterministic setup validation, entry scoring, and regime throttling.

This module is the single authority for deciding whether daily OHLCV data
mathematically satisfies one of the three approved swing setup families.  It
performs no I/O and has no broker, database, network, or LLM dependency.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal, Mapping

import numpy as np
import pandas as pd

from .config import EntryScoreWeights, StrategySettings, SwingSettings
from .models import MarketRegime, SetupType


@dataclass(frozen=True)
class ValidationResult:
    setup_type: SetupType
    passed: bool
    failed_conditions: tuple[str, ...]
    conditions: Mapping[str, bool]
    features: Mapping[str, float | int | str | bool | None]
    provisional_stop: float | None = None
    target: float | None = None

    @property
    def primary_reason(self) -> str:
        return self.failed_conditions[0] if self.failed_conditions else "setup_valid"


@dataclass(frozen=True)
class EntryScoreInput:
    setup_quality: float
    relative_strength: float
    trend_quality: float
    volume_confirmation: float
    market_regime: float
    sector_strength: float
    risk_reward: float

    def as_dict(self) -> dict[str, float]:
        return {
            "setup_quality": self.setup_quality,
            "relative_strength": self.relative_strength,
            "trend_quality": self.trend_quality,
            "volume_confirmation": self.volume_confirmation,
            "market_regime": self.market_regime,
            "sector_strength": self.sector_strength,
            "risk_reward": self.risk_reward,
        }


@dataclass(frozen=True)
class EntryScoreResult:
    total: float
    components: Mapping[str, float]
    route: Literal["reject", "watchlist", "strong", "very_strong"]
    hard_validator_passed: bool


@dataclass(frozen=True)
class RegimeThrottle:
    score_threshold: float
    risk_multiplier: float
    no_trade: bool = False


@dataclass
class _Context:
    frame: pd.DataFrame
    spy: pd.DataFrame
    sector: pd.DataFrame | None
    close: pd.Series
    price: float
    ema_short: float
    ma_medium: float
    ma_long: float
    atr: float
    atr_pct: float
    medium_slope: float
    long_slope: float
    trend_roc: float
    stock_rs_spy: float
    stock_rs_sector: float | None
    sector_rs_spy: float | None
    volume_ratio: float
    bullish_trigger: bool


class _DataFailure(ValueError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _normalize_frame(raw: pd.DataFrame, *, minimum_sessions: int, as_of: datetime, maximum_age_days: int) -> pd.DataFrame:
    if not isinstance(raw, pd.DataFrame):
        raise _DataFailure("bar_data_malformed")
    frame = raw.copy()
    frame.columns = [str(column).lower() for column in frame.columns]
    required = ("open", "high", "low", "close", "volume")
    if any(column not in frame.columns for column in required):
        raise _DataFailure("bar_data_incomplete")
    if len(frame) < minimum_sessions:
        raise _DataFailure("insufficient_history")
    if not isinstance(frame.index, pd.DatetimeIndex) or frame.index.has_duplicates or not frame.index.is_monotonic_increasing:
        raise _DataFailure("bar_timestamps_invalid")
    numeric = frame.loc[:, required].apply(pd.to_numeric, errors="coerce")
    if not np.isfinite(numeric.to_numpy()).all():
        raise _DataFailure("bar_data_incomplete")
    if (numeric[["open", "high", "low", "close"]] <= 0).any(axis=None) or (numeric["volume"] < 0).any():
        raise _DataFailure("bar_data_malformed")
    if (numeric["high"] < numeric[["open", "close", "low"]].max(axis=1)).any() or (
        numeric["low"] > numeric[["open", "close", "high"]].min(axis=1)
    ).any():
        raise _DataFailure("bar_geometry_invalid")
    last = frame.index[-1]
    last_dt = last.to_pydatetime()
    if last_dt.tzinfo is None:
        last_dt = last_dt.replace(tzinfo=timezone.utc)
    as_of_dt = as_of if as_of.tzinfo is not None else as_of.replace(tzinfo=timezone.utc)
    age_days = (as_of_dt.astimezone(timezone.utc) - last_dt.astimezone(timezone.utc)).total_seconds() / 86_400
    if age_days < -1 or age_days > maximum_age_days:
        raise _DataFailure("bar_data_stale")
    return numeric.astype(float)


def _roc(values: pd.Series, period: int) -> float:
    if len(values) <= period or values.iloc[-period - 1] <= 0:
        raise _DataFailure("indicator_history_incomplete")
    return float(values.iloc[-1] / values.iloc[-period - 1] - 1)


def _average_slope(values: pd.Series, period: int, lookback: int) -> float:
    rolling = values.rolling(period).mean()
    old = float(rolling.iloc[-lookback - 1])
    if not np.isfinite(old) or old <= 0:
        raise _DataFailure("indicator_history_incomplete")
    return float(rolling.iloc[-1] / old - 1)


def _atr(frame: pd.DataFrame, period: int) -> float:
    previous = frame["close"].shift(1)
    true_range = pd.concat(
        (frame["high"] - frame["low"], (frame["high"] - previous).abs(), (frame["low"] - previous).abs()),
        axis=1,
    ).max(axis=1)
    value = float(true_range.rolling(period).mean().iloc[-1])
    if not np.isfinite(value) or value <= 0:
        raise _DataFailure("atr_unavailable")
    return value


def _build_context(
    bars: pd.DataFrame,
    spy_bars: pd.DataFrame,
    sector_bars: pd.DataFrame | None,
    config: StrategySettings,
    minimum_sessions: int,
    as_of: datetime,
) -> _Context:
    frame = _normalize_frame(
        bars, minimum_sessions=minimum_sessions, as_of=as_of, maximum_age_days=config.maximum_bar_age_calendar_days
    )
    spy = _normalize_frame(
        spy_bars, minimum_sessions=minimum_sessions, as_of=as_of, maximum_age_days=config.maximum_bar_age_calendar_days
    )
    sector = None
    if sector_bars is not None:
        sector = _normalize_frame(
            sector_bars, minimum_sessions=minimum_sessions, as_of=as_of, maximum_age_days=config.maximum_bar_age_calendar_days
        )
    close = frame["close"]
    price = float(close.iloc[-1])
    ema_short = float(close.ewm(span=config.short_ema_period, adjust=False).mean().iloc[-1])
    ma_medium = float(close.rolling(config.medium_ma_period).mean().iloc[-1])
    ma_long = float(close.rolling(config.long_ma_period).mean().iloc[-1])
    atr_value = _atr(frame, config.atr_period)
    volume_average = float(frame["volume"].iloc[-config.short_ema_period - 1 : -1].mean())
    if volume_average <= 0:
        raise _DataFailure("volume_history_invalid")
    return _Context(
        frame=frame,
        spy=spy,
        sector=sector,
        close=close,
        price=price,
        ema_short=ema_short,
        ma_medium=ma_medium,
        ma_long=ma_long,
        atr=atr_value,
        atr_pct=atr_value / price,
        medium_slope=_average_slope(close, config.medium_ma_period, config.trend_slope_lookback),
        long_slope=_average_slope(close, config.long_ma_period, config.trend_slope_lookback),
        trend_roc=_roc(close, config.relative_strength_lookback),
        stock_rs_spy=_roc(close, config.relative_strength_lookback) - _roc(spy["close"], config.relative_strength_lookback),
        stock_rs_sector=(
            _roc(close, config.relative_strength_lookback) - _roc(sector["close"], config.relative_strength_lookback)
            if sector is not None
            else None
        ),
        sector_rs_spy=(
            _roc(sector["close"], config.relative_strength_lookback) - _roc(spy["close"], config.relative_strength_lookback)
            if sector is not None
            else None
        ),
        volume_ratio=float(frame["volume"].iloc[-1] / volume_average),
        bullish_trigger=bool(close.iloc[-1] > close.iloc[-2] and close.iloc[-1] > frame["open"].iloc[-1]),
    )


def _base_features(context: _Context, *, reward_risk: float | None = None) -> dict[str, float | int | str | bool | None]:
    return {
        "price": context.price,
        "ema_short": context.ema_short,
        "ma_medium": context.ma_medium,
        "ma_long": context.ma_long,
        "medium_slope": context.medium_slope,
        "long_slope": context.long_slope,
        "trend_roc": context.trend_roc,
        "stock_rs_spy": context.stock_rs_spy,
        "stock_rs_sector": context.stock_rs_sector,
        "sector_rs_spy": context.sector_rs_spy,
        "atr": context.atr,
        "atr_pct": context.atr_pct,
        "volume_ratio": context.volume_ratio,
        "bullish_trigger": context.bullish_trigger,
        "reward_risk": reward_risk,
    }


def _result(
    setup_type: SetupType,
    conditions: dict[str, bool],
    features: dict[str, float | int | str | bool | None],
    stop: float | None,
    target: float | None,
) -> ValidationResult:
    failures = tuple(code for code, passed in conditions.items() if not passed)
    return ValidationResult(setup_type, not failures, failures, conditions, features, stop, target)


def _data_failure(setup_type: SetupType, code: str) -> ValidationResult:
    return ValidationResult(setup_type, False, (code,), {code: False}, {"data_failure": code})


def validate_trend_pullback(
    bars: pd.DataFrame,
    spy_bars: pd.DataFrame,
    *,
    config: StrategySettings,
    minimum_history_sessions: int,
    minimum_reward_risk: float,
    as_of: datetime,
    event_risk_prohibited: bool | None,
    liquidity_acceptable: bool,
) -> ValidationResult:
    try:
        context = _build_context(bars, spy_bars, None, config, minimum_history_sessions, as_of)
    except _DataFailure as exc:
        return _data_failure(SetupType.TREND_PULLBACK, exc.code)
    recent = context.frame.tail(config.trend_slope_lookback)
    pullback_distance = min(abs(context.price - context.ema_short), abs(context.price - context.ma_medium)) / context.atr
    recent_low = float(recent["low"].min())
    structural_support = max(recent_low - config.structural_break_atr * context.atr, context.ma_medium - config.structural_break_atr * context.atr)
    # TODO(prompt-03): replace this provisional structural stop with the central stop authority.
    stop = min(structural_support, context.price - config.minimum_atr_pct * context.price)
    prior_high = float(context.frame["high"].iloc[-config.relative_strength_lookback : -1].max())
    target = prior_high + config.trend_target_atr_extension * context.atr
    reward_risk = (target - context.price) / (context.price - stop) if stop < context.price < target else 0.0
    conditions = {
        "price_not_above_medium_ma": context.price > context.ma_medium,
        "medium_trend_not_positive": context.trend_roc > 0,
        "short_trend_not_above_medium": context.ema_short > context.ma_medium,
        "medium_trend_not_rising": context.medium_slope > config.minimum_trend_slope,
        "long_trend_not_rising": context.long_slope > config.minimum_trend_slope,
        "relative_strength_spy_weak": context.stock_rs_spy > config.minimum_spy_relative_strength,
        "not_near_pullback_support": pullback_distance <= config.pullback_max_atr_distance,
        "structural_breakdown": recent_low >= context.ma_medium - config.structural_break_atr * context.atr,
        "bullish_confirmation_missing": context.bullish_trigger,
        "volatility_out_of_bounds": config.minimum_atr_pct <= context.atr_pct <= config.maximum_atr_pct,
        "insufficient_reward_risk": reward_risk >= minimum_reward_risk,
        "event_risk_unknown": event_risk_prohibited is not None,
        "prohibited_event_risk": event_risk_prohibited is not True,
        "liquidity_unacceptable": liquidity_acceptable,
    }
    features = _base_features(context, reward_risk=reward_risk)
    features.update(
        pullback_atr_distance=pullback_distance,
        recent_low=recent_low,
        prior_high=prior_high,
        volume_contracting=context.volume_ratio <= config.contraction_volume_ratio,
        provisional_stop=stop,
        target=target,
    )
    return _result(SetupType.TREND_PULLBACK, conditions, features, stop, target)


def validate_breakout_retest(
    bars: pd.DataFrame,
    spy_bars: pd.DataFrame,
    *,
    config: StrategySettings,
    minimum_history_sessions: int,
    minimum_reward_risk: float,
    as_of: datetime,
    event_risk_prohibited: bool | None,
    liquidity_acceptable: bool,
) -> ValidationResult:
    try:
        context = _build_context(bars, spy_bars, None, config, minimum_history_sessions, as_of)
    except _DataFailure as exc:
        return _data_failure(SetupType.BREAKOUT_RETEST, exc.code)
    breakout_window_sessions = config.trend_slope_lookback
    consolidation = context.frame.iloc[-(config.consolidation_sessions + breakout_window_sessions) : -breakout_window_sessions]
    breakout_window = context.frame.iloc[-breakout_window_sessions:-1]
    if len(consolidation) < config.consolidation_sessions or breakout_window.empty:
        return _data_failure(SetupType.BREAKOUT_RETEST, "consolidation_history_incomplete")
    resistance = float(consolidation["high"].max())
    consolidation_low = float(consolidation["low"].min())
    consolidation_range = resistance - consolidation_low
    tightness = consolidation_range / float(consolidation["close"].mean())
    breakout_mask = breakout_window["close"] > resistance * (1 + config.breakout_buffer_pct)
    breakout_seen = bool(breakout_mask.any())
    breakout_volume_ratio = 0.0
    if breakout_seen:
        breakout_position = int(np.flatnonzero(breakout_mask.to_numpy())[0])
        breakout_index = breakout_window.index[breakout_position]
        full_position = context.frame.index.get_loc(breakout_index)
        baseline = float(context.frame["volume"].iloc[max(0, full_position - config.short_ema_period) : full_position].mean())
        breakout_volume_ratio = float(context.frame["volume"].iloc[full_position] / baseline) if baseline > 0 else 0.0
    extension_atr = (context.price - resistance) / context.atr
    retest_low = float(context.frame["low"].tail(config.trend_slope_lookback).min())
    # TODO(prompt-03): replace this provisional structural stop with the central stop authority.
    stop = min(resistance * (1 - config.retest_hold_tolerance_pct), retest_low) - config.structural_break_atr * context.atr
    target = resistance + config.measured_move_multiple * consolidation_range
    reward_risk = (target - context.price) / (context.price - stop) if stop < context.price < target else 0.0
    conditions = {
        "consolidation_too_loose": tightness <= config.consolidation_max_range_pct,
        "breakout_not_confirmed": breakout_seen,
        "breakout_volume_not_expanded": breakout_volume_ratio >= config.breakout_volume_ratio,
        "retest_collapsed": retest_low >= resistance * (1 - config.retest_collapse_tolerance_pct),
        "breakout_level_not_held": context.price >= resistance * (1 - config.retest_hold_tolerance_pct),
        "bullish_confirmation_missing": context.bullish_trigger,
        "entry_extended_chasing": extension_atr <= config.maximum_breakout_extension_atr,
        "trend_structure_invalid": context.price > context.ma_medium and context.medium_slope > config.minimum_trend_slope,
        "volatility_out_of_bounds": config.minimum_atr_pct <= context.atr_pct <= config.maximum_atr_pct,
        "insufficient_reward_risk": reward_risk >= minimum_reward_risk,
        "event_risk_unknown": event_risk_prohibited is not None,
        "prohibited_event_risk": event_risk_prohibited is not True,
        "liquidity_unacceptable": liquidity_acceptable,
    }
    features = _base_features(context, reward_risk=reward_risk)
    features.update(
        resistance=resistance,
        consolidation_low=consolidation_low,
        consolidation_range_pct=tightness,
        breakout_seen=breakout_seen,
        breakout_volume_ratio=breakout_volume_ratio,
        retest_low=retest_low,
        extension_atr=extension_atr,
        provisional_stop=stop,
        target=target,
    )
    return _result(SetupType.BREAKOUT_RETEST, conditions, features, stop, target)


def validate_relative_strength_continuation(
    bars: pd.DataFrame,
    spy_bars: pd.DataFrame,
    sector_bars: pd.DataFrame | None,
    *,
    config: StrategySettings,
    minimum_history_sessions: int,
    minimum_reward_risk: float,
    as_of: datetime,
    event_risk_prohibited: bool | None,
    liquidity_acceptable: bool,
    market_regime: MarketRegime,
) -> ValidationResult:
    if sector_bars is None:
        # TODO: add/verify a complete sector-to-ETF data feed before this gate may pass.
        return _data_failure(SetupType.RELATIVE_STRENGTH_CONTINUATION, "sector_relative_strength_unavailable")
    try:
        context = _build_context(bars, spy_bars, sector_bars, config, minimum_history_sessions, as_of)
    except _DataFailure as exc:
        return _data_failure(SetupType.RELATIVE_STRENGTH_CONTINUATION, exc.code)
    consolidation = context.frame.iloc[-config.rs_consolidation_sessions - 1 : -1]
    consolidation_high = float(consolidation["high"].max())
    consolidation_low = float(consolidation["low"].min())
    consolidation_range_atr = (consolidation_high - consolidation_low) / context.atr
    preceding_volume = float(
        context.frame["volume"].iloc[-config.rs_consolidation_sessions - config.short_ema_period : -config.rs_consolidation_sessions].mean()
    )
    consolidation_volume = float(consolidation["volume"].mean())
    contraction_ratio = consolidation_volume / preceding_volume if preceding_volume > 0 else float("inf")
    # TODO(prompt-03): replace this provisional structural stop with the central stop authority.
    stop = consolidation_low - config.structural_break_atr * context.atr
    target = consolidation_high + config.measured_move_multiple * (consolidation_high - consolidation_low)
    reward_risk = (target - context.price) / (context.price - stop) if stop < context.price < target else 0.0
    hostile = market_regime in {MarketRegime.BEAR, MarketRegime.HIGH_VOLATILITY_RISK_OFF}
    conditions = {
        "relative_strength_spy_weak": context.stock_rs_spy >= config.strong_spy_relative_strength,
        "relative_strength_sector_weak": bool(
            context.stock_rs_sector is not None and context.stock_rs_sector >= config.minimum_sector_relative_strength
        ),
        "sector_relative_strength_weak": bool(
            context.sector_rs_spy is not None and context.sector_rs_spy >= config.minimum_sector_relative_strength
        ),
        "trend_structure_invalid": context.ema_short > context.ma_medium > context.ma_long,
        "price_not_above_major_averages": context.price > context.ema_short > context.ma_medium,
        "constructive_consolidation_missing": consolidation_range_atr <= config.rs_consolidation_max_atr,
        "volume_not_contracting": contraction_ratio <= config.contraction_volume_ratio,
        "bullish_confirmation_missing": context.bullish_trigger and context.price > consolidation_high,
        "confirmation_volume_missing": context.volume_ratio >= config.confirmation_volume_ratio,
        "insufficient_reward_risk": reward_risk >= minimum_reward_risk,
        "volatility_out_of_bounds": config.minimum_atr_pct <= context.atr_pct <= config.maximum_atr_pct,
        "liquidity_unacceptable": liquidity_acceptable,
        "market_regime_hostile": not hostile,
        "event_risk_unknown": event_risk_prohibited is not None,
        "prohibited_event_risk": event_risk_prohibited is not True,
    }
    features = _base_features(context, reward_risk=reward_risk)
    features.update(
        consolidation_high=consolidation_high,
        consolidation_low=consolidation_low,
        consolidation_range_atr=consolidation_range_atr,
        contraction_volume_ratio=contraction_ratio,
        provisional_stop=stop,
        target=target,
        market_regime=market_regime.value,
    )
    return _result(SetupType.RELATIVE_STRENGTH_CONTINUATION, conditions, features, stop, target)


def validate_setup(
    setup_type: SetupType,
    bars: pd.DataFrame,
    spy_bars: pd.DataFrame,
    *,
    sector_bars: pd.DataFrame | None,
    settings: SwingSettings,
    as_of: datetime,
    event_risk_prohibited: bool | None,
    liquidity_acceptable: bool,
    market_regime: MarketRegime,
) -> ValidationResult:
    if setup_type == SetupType.TREND_PULLBACK:
        return validate_trend_pullback(
            bars,
            spy_bars,
            config=settings.strategy,
            minimum_history_sessions=settings.minimum_history_sessions,
            minimum_reward_risk=settings.minimum_rr,
            as_of=as_of,
            event_risk_prohibited=event_risk_prohibited,
            liquidity_acceptable=liquidity_acceptable,
        )
    if setup_type == SetupType.BREAKOUT_RETEST:
        return validate_breakout_retest(
            bars,
            spy_bars,
            config=settings.strategy,
            minimum_history_sessions=settings.minimum_history_sessions,
            minimum_reward_risk=settings.minimum_rr,
            as_of=as_of,
            event_risk_prohibited=event_risk_prohibited,
            liquidity_acceptable=liquidity_acceptable,
        )
    if setup_type == SetupType.RELATIVE_STRENGTH_CONTINUATION:
        return validate_relative_strength_continuation(
            bars,
            spy_bars,
            sector_bars,
            config=settings.strategy,
            minimum_history_sessions=settings.minimum_history_sessions,
            minimum_reward_risk=settings.minimum_rr,
            as_of=as_of,
            event_risk_prohibited=event_risk_prohibited,
            liquidity_acceptable=liquidity_acceptable,
            market_regime=market_regime,
        )
    raise ValueError(f"Unsupported setup type: {setup_type}")


def regime_throttle(regime: MarketRegime, settings: SwingSettings) -> RegimeThrottle:
    addition, multiplier, no_trade = {
        MarketRegime.STRONG_BULL: (0.0, 1.0, False),
        MarketRegime.BULL: (0.0, 1.0, False),
        MarketRegime.NEUTRAL: (settings.neutral_score_threshold_addition, settings.neutral_risk_multiplier, False),
        MarketRegime.CHOPPY: (settings.choppy_score_threshold_addition, settings.choppy_risk_multiplier, False),
        MarketRegime.BEAR: (settings.hostile_score_threshold_addition, settings.hostile_risk_multiplier, False),
        MarketRegime.HIGH_VOLATILITY_RISK_OFF: (settings.hostile_score_threshold_addition, settings.hostile_risk_multiplier, True),
    }[regime]
    throttle = RegimeThrottle(min(101.0, settings.buy_score_threshold + addition), multiplier, no_trade)
    if throttle.score_threshold < settings.buy_score_threshold or throttle.risk_multiplier > 1:
        raise AssertionError("Market regime may only tighten selectivity and risk")
    return throttle


def score_entry(
    inputs: EntryScoreInput,
    *,
    weights: EntryScoreWeights,
    settings: SwingSettings,
    validator_passed: bool,
    regime: MarketRegime,
) -> EntryScoreResult:
    fractions = inputs.as_dict()
    if any(not np.isfinite(value) or value < 0 or value > 1 for value in fractions.values()):
        raise ValueError("Entry score inputs must be finite fractions between zero and one")
    components = {name: round(weights.as_dict()[name] * value, 4) for name, value in fractions.items()}
    total = round(sum(components.values()), 4)
    throttle = regime_throttle(regime, settings)
    if not validator_passed:
        route: Literal["reject", "watchlist", "strong", "very_strong"] = "reject"
    elif throttle.no_trade:
        route = "reject"
    elif total >= settings.preferred_score_threshold and total >= throttle.score_threshold:
        route = "very_strong"
    elif total >= throttle.score_threshold:
        route = "strong"
    elif total >= settings.borderline_score_threshold:
        route = "watchlist"
    else:
        route = "reject"
    return EntryScoreResult(total, components, route, validator_passed)


def render_entry_score(ticker: str, setup_type: SetupType, score: EntryScoreResult, weights: EntryScoreWeights) -> str:
    """Render the exact weighted contribution behind a candidate's route."""
    labels = {
        "setup_quality": "Setup quality",
        "relative_strength": "Relative strength",
        "trend_quality": "Trend quality",
        "volume_confirmation": "Volume confirmation",
        "market_regime": "Market regime",
        "sector_strength": "Sector strength",
        "risk_reward": "Risk/reward",
    }
    maxima = weights.as_dict()
    rows = [f"Ticker: {ticker.upper()}", f"Setup: {setup_type.value}", ""]
    for name in maxima:
        rows.append(f"{labels[name]}: {score.components[name]:g}/{maxima[name]:g}")
    rows.extend(("", f"Total: {score.total:g}/100", f"Route: {score.route}"))
    return "\n".join(rows)


def score_inputs_from_validation(result: ValidationResult, regime: MarketRegime, config: StrategySettings) -> EntryScoreInput:
    features = result.features
    rr = float(features.get("reward_risk") or 0)
    rs_spy = float(features.get("stock_rs_spy") or 0)
    rs_sector = features.get("sector_rs_spy")
    medium_slope = float(features.get("medium_slope") or 0)
    long_slope = float(features.get("long_slope") or 0)
    volume_ratio = float(features.get("volume_ratio") or 0)
    setup_quality = 1.0
    if result.setup_type == SetupType.TREND_PULLBACK:
        distance = float(features.get("pullback_atr_distance") or config.pullback_max_atr_distance)
        setup_quality = 1 - 0.25 * min(1.0, distance / config.pullback_max_atr_distance)
    elif result.setup_type == SetupType.BREAKOUT_RETEST:
        extension = max(0.0, float(features.get("extension_atr") or 0))
        setup_quality = 1 - 0.25 * min(1.0, extension / config.maximum_breakout_extension_atr)
        volume_ratio = float(features.get("breakout_volume_ratio") or volume_ratio)
    elif result.setup_type == SetupType.RELATIVE_STRENGTH_CONTINUATION:
        contraction = float(features.get("contraction_volume_ratio") or 1)
        setup_quality = 1 - 0.25 * min(1.0, contraction / config.contraction_volume_ratio)
    regime_fraction = {
        MarketRegime.STRONG_BULL: 1.0,
        MarketRegime.BULL: 0.85,
        MarketRegime.NEUTRAL: 0.60,
        MarketRegime.CHOPPY: 0.35,
        MarketRegime.BEAR: 0.10,
        MarketRegime.HIGH_VOLATILITY_RISK_OFF: 0.0,
    }[regime]
    return EntryScoreInput(
        setup_quality=float(np.clip(setup_quality, 0, 1)),
        relative_strength=float(np.clip(0.5 + rs_spy * 5, 0, 1)),
        trend_quality=float(np.clip(0.5 + (medium_slope + long_slope) * 10, 0, 1)),
        volume_confirmation=float(np.clip(volume_ratio / config.breakout_volume_ratio, 0, 1)),
        market_regime=regime_fraction,
        sector_strength=float(np.clip(0.5 + float(rs_sector or 0) * 5, 0, 1)) if rs_sector is not None else 0.0,
        risk_reward=float(np.clip(rr / 3.0, 0, 1)),
    )
