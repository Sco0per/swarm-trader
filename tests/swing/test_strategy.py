from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from src.swing.config import StrategySettings, SwingSettings
from src.swing.market import DeterministicSwingScanner, UniverseAsset
from src.swing.models import MarketRegime, SetupType
from src.swing.strategy import (
    EntryScoreInput,
    regime_throttle,
    render_entry_score,
    score_entry,
    validate_breakout_retest,
    validate_relative_strength_continuation,
    validate_setup,
    validate_trend_pullback,
)

AS_OF = datetime(2026, 8, 11, tzinfo=timezone.utc)


def _frame(close, volume=None, *, width=2.0, end=AS_OF):
    close = np.asarray(close, dtype=float)
    volume = np.full(len(close), 2_000_000.0) if volume is None else np.asarray(volume, dtype=float)
    index = pd.date_range(end=end, periods=len(close), freq="B")
    return pd.DataFrame(
        {
            "open": close - 0.2,
            "high": close + width / 2,
            "low": close - width / 2,
            "close": close,
            "volume": volume,
        },
        index=index,
    )


def _trend_inputs():
    close = np.linspace(60, 108, 220)
    close[-10:] = [107, 106.5, 106, 105.5, 105, 104.8, 105, 105.2, 105.5, 106.2]
    bars = _frame(close)
    spy = _frame(np.linspace(60, 90, 220))
    return bars, spy


def _breakout_inputs():
    close = np.linspace(70, 100, 220)
    close[-30:-10] = np.resize(np.array([97.0, 99.0, 101.0, 103.0]), 20)
    close[-10:-1] = [105.0, 103.5, 102.8, 103.0, 103.2, 103.4, 103.1, 103.3, 103.5]
    close[-1] = 104.2
    volume = np.full(220, 2_000_000.0)
    volume[-10] = 4_000_000.0
    bars = _frame(close, volume, width=1.0)
    spy = _frame(np.linspace(70, 92, 220))
    return bars, spy


def _rs_inputs():
    close = np.linspace(50, 110, 220)
    close[-11:-1] = [108, 109, 110, 109, 108.5, 109, 110, 109.5, 110, 110.5]
    close[-1] = 111.3
    volume = np.full(220, 2_000_000.0)
    volume[-11:-1] = 1_200_000.0
    volume[-1] = 3_000_000.0
    bars = _frame(close, volume, width=1.5)
    spy = _frame(np.linspace(60, 90, 220), width=1.5)
    sector = _frame(np.linspace(60, 100, 220), width=1.5)
    return bars, spy, sector


def _trend_result(settings: SwingSettings, bars=None, spy=None, sector=None, **changes):
    default_bars, default_spy = _trend_inputs()
    return validate_trend_pullback(
        bars if bars is not None else default_bars,
        spy if spy is not None else default_spy,
        sector,
        config=settings.strategy,
        minimum_history_sessions=settings.minimum_history_sessions,
        minimum_reward_risk=changes.pop("minimum_reward_risk", settings.minimum_rr),
        as_of=changes.pop("as_of", AS_OF),
        event_risk_prohibited=changes.pop("event_risk_prohibited", False),
        liquidity_acceptable=changes.pop("liquidity_acceptable", True),
    )


def test_valid_trend_pullback_passes(settings):
    result = _trend_result(settings)
    assert result.passed, result.failed_conditions
    assert all(result.conditions.values())
    assert result.invalidation_level is not None and result.target is not None
    assert result.invalidation_level < float(result.features["price"] or 0) < result.target


@pytest.mark.parametrize(
    ("reason", "mutation"),
    [
        ("price_not_above_medium_ma", "price_below_ma"),
        ("medium_trend_not_positive", "negative_trend"),
        ("short_trend_not_above_medium", "short_below_medium"),
        ("medium_trend_not_rising", "slope_threshold"),
        ("long_trend_not_rising", "slope_threshold"),
        ("relative_strength_spy_weak", "weak_rs"),
        ("not_near_pullback_support", "far_from_support"),
        ("structural_breakdown", "break_support"),
        ("bullish_confirmation_missing", "no_trigger"),
        ("volatility_out_of_bounds", "high_volatility"),
        ("insufficient_reward_risk", "rr"),
        ("event_risk_unknown", "event_unknown"),
        ("prohibited_event_risk", "event_prohibited"),
        ("liquidity_unacceptable", "liquidity"),
    ],
)
def test_each_trend_pullback_mandatory_condition_fails_closed(settings, reason, mutation):
    bars, spy = _trend_inputs()
    kwargs = {}
    local_settings = settings
    if mutation == "price_below_ma":
        bars.loc[bars.index[-1], ["open", "high", "low", "close"]] = [89.5, 91, 89, 90]
    elif mutation == "negative_trend":
        bars = _frame(np.linspace(120, 100, 220))
    elif mutation == "short_below_medium":
        bars.loc[bars.index[-20:], "close"] = np.linspace(106, 98, 20)
        bars.loc[bars.index[-20:], "open"] = bars.loc[bars.index[-20:], "close"] - 0.2
        bars.loc[bars.index[-20:], "high"] = bars.loc[bars.index[-20:], "close"] + 1
        bars.loc[bars.index[-20:], "low"] = bars.loc[bars.index[-20:], "close"] - 1
    elif mutation == "slope_threshold":
        local_settings = replace(settings, strategy=replace(settings.strategy, minimum_trend_slope=0.049))
    elif mutation == "weak_rs":
        spy = bars.copy()
    elif mutation == "far_from_support":
        bars.loc[bars.index[-1], ["open", "high", "low", "close"]] = [114, 116, 113, 115]
    elif mutation == "break_support":
        bars.loc[bars.index[-1], "low"] = 80
    elif mutation == "no_trigger":
        prior = float(bars["close"].iloc[-2])
        bars.loc[bars.index[-1], ["open", "high", "low", "close"]] = [prior + 0.2, prior + 1, prior - 1, prior - 0.2]
    elif mutation == "high_volatility":
        bars["high"] = bars["close"] + 10
        bars["low"] = bars["close"] - 10
    elif mutation == "rr":
        kwargs["minimum_reward_risk"] = 50
    elif mutation == "event_unknown":
        kwargs["event_risk_prohibited"] = None
    elif mutation == "event_prohibited":
        kwargs["event_risk_prohibited"] = True
    elif mutation == "liquidity":
        kwargs["liquidity_acceptable"] = False
    result = _trend_result(local_settings, bars=bars, spy=spy, **kwargs)
    assert not result.passed
    assert reason in result.failed_conditions


def test_valid_breakout_retest_passes_and_failed_or_extended_breakouts_fail(settings):
    bars, spy = _breakout_inputs()

    def validate(candidate):
        return validate_breakout_retest(
            candidate,
            spy,
            None,
            config=settings.strategy,
            minimum_history_sessions=settings.minimum_history_sessions,
            minimum_reward_risk=settings.minimum_rr,
            as_of=AS_OF,
            event_risk_prohibited=False,
            liquidity_acceptable=True,
        )

    valid = validate(bars)
    assert valid.passed, valid.failed_conditions

    failed = bars.copy()
    failed.loc[failed.index[-10], ["open", "high", "low", "close"]] = [103.1, 103.6, 102.8, 103.4]
    result = validate(failed)
    assert not result.passed and "breakout_not_confirmed" in result.failed_conditions

    extended = bars.copy()
    resistance = valid.features["resistance"]
    atr_value = valid.features["atr"]
    assert isinstance(resistance, (int, float)) and isinstance(atr_value, (int, float))
    extended_price = float(resistance) + settings.strategy.maximum_breakout_extension_atr * float(atr_value) + 2
    extended.loc[extended.index[-1], ["open", "high", "low", "close"]] = [extended_price - 0.5, extended_price + 0.5, extended_price - 1, extended_price]
    result = validate(extended)
    assert not result.passed and "entry_extended_chasing" in result.failed_conditions


def test_valid_rs_continuation_passes_and_weak_rs_or_missing_sector_fails(settings):
    bars, spy, sector = _rs_inputs()

    def validate(candidate_spy, candidate_sector):
        return validate_relative_strength_continuation(
            bars,
            candidate_spy,
            candidate_sector,
            config=settings.strategy,
            minimum_history_sessions=settings.minimum_history_sessions,
            minimum_reward_risk=settings.minimum_rr,
            as_of=AS_OF,
            event_risk_prohibited=False,
            liquidity_acceptable=True,
            market_regime=MarketRegime.BULL,
        )

    valid = validate(spy, sector)
    assert valid.passed, valid.failed_conditions
    weak = validate(bars, sector)
    assert not weak.passed and "relative_strength_spy_weak" in weak.failed_conditions
    missing = validate(spy, None)
    assert not missing.passed and missing.primary_reason == "sector_relative_strength_unavailable"


def test_insufficient_rr_and_stale_or_incomplete_bars_fail_closed(settings):
    bars, spy = _trend_inputs()
    rr = _trend_result(settings, bars=bars, spy=spy, minimum_reward_risk=50)
    assert "insufficient_reward_risk" in rr.failed_conditions

    stale = _trend_result(settings, bars=bars, spy=spy, as_of=AS_OF + timedelta(days=8))
    assert stale.primary_reason == "bar_data_stale"

    incomplete = bars.copy()
    incomplete.loc[incomplete.index[-1], "volume"] = np.nan
    result = _trend_result(settings, bars=incomplete, spy=spy)
    assert result.primary_reason == "bar_data_incomplete"


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        ("price", "minimum_price"),
        ("volume", "minimum_average_volume"),
        ("spread", "maximum_spread"),
        ("history", "insufficient_history"),
    ],
)
def test_liquidity_price_spread_and_history_boundaries_reject(settings, mutation, reason):
    bars, spy = _trend_inputs()
    asset = UniverseAsset("BOUND", earnings_trading_days=30, prohibited_event_risk=False, bid=99.75, ask=100.25)
    if mutation == "price":
        bars[["open", "high", "low", "close"]] = settings.minimum_price
    elif mutation == "volume":
        bars["volume"] = settings.minimum_average_volume
    elif mutation == "spread":
        midpoint = 100.0
        half = settings.maximum_spread_pct * midpoint / 2
        asset = replace(asset, bid=midpoint - half, ask=midpoint + half)
    elif mutation == "history":
        bars = bars.tail(settings.minimum_history_sessions - 1)
    scanner = DeterministicSwingScanner(settings)
    _, report = scanner.scan_with_report([asset], {"BOUND": bars}, spy_bars=spy, qqq_bars=spy, retrieved_at=AS_OF)
    assert report.rejection_counts[reason] == 1


def test_real_average_volume_override_rejects_below_floor_even_when_bars_alone_would_pass(settings):
    """Alpaca's IEX-only feed understates volume; a fresh real-volume cache
    reading below the floor must still reject even though the bars alone
    (flat 2M-share volume) comfortably clear it -- the override isn't a
    one-way ratchet that can only raise the effective liquidity floor."""
    bars, spy = _trend_inputs()
    asset = UniverseAsset(
        "BOUND",
        earnings_trading_days=30,
        prohibited_event_risk=False,
        bid=99.75,
        ask=100.25,
        real_average_volume=settings.minimum_average_volume - 1,
        real_average_dollar_volume=settings.minimum_average_dollar_volume * 10,
    )
    scanner = DeterministicSwingScanner(settings)
    _, report = scanner.scan_with_report([asset], {"BOUND": bars}, spy_bars=spy, qqq_bars=spy, retrieved_at=AS_OF)
    assert report.rejection_counts["minimum_average_volume"] == 1


def test_blacklist_leveraged_etf_and_unloadable_blacklist_fail_closed(settings, tmp_path):
    blacklist = tmp_path / "blacklist.yaml"
    blacklist.write_text("symbols:\n  - BLOCK\nsubstantially_identical: []\n", encoding="utf-8")
    local = replace(settings, do_not_trade_path=blacklist)
    bars, spy = _trend_inputs()
    scanner = DeterministicSwingScanner(local)
    assets = [
        UniverseAsset("BLOCK", prohibited_event_risk=False, bid=99.9, ask=100.1),
        UniverseAsset("TQQQ", is_etf=True, security_type="etf", prohibited_event_risk=False, bid=99.9, ask=100.1),
    ]
    _, report = scanner.scan_with_report(assets, {asset.symbol: bars for asset in assets}, spy_bars=spy, qqq_bars=spy, retrieved_at=AS_OF)
    assert report.rejection_counts["blacklisted_symbol"] == 1
    assert report.rejection_counts["leveraged_or_inverse_etf"] == 1
    with pytest.raises(RuntimeError, match="blacklist"):
        DeterministicSwingScanner(replace(settings, do_not_trade_path=tmp_path / "missing.yaml"))


def test_entry_score_is_deterministic_and_cannot_rescue_hard_failure(settings):
    inputs = EntryScoreInput(0.9, 0.9, 0.9, 0.8, 0.85, 0.8, 1.0)
    first = score_entry(inputs, weights=settings.score_weights, settings=settings, validator_passed=True, regime=MarketRegime.BULL)
    second = score_entry(inputs, weights=settings.score_weights, settings=settings, validator_passed=True, regime=MarketRegime.BULL)
    assert first == second
    rendered = render_entry_score("xyz", SetupType.TREND_PULLBACK, first, settings.score_weights)
    assert "Ticker: XYZ" in rendered and f"Total: {first.total:g}/100" in rendered
    failed = score_entry(
        EntryScoreInput(1, 1, 1, 1, 1, 1, 1),
        weights=settings.score_weights,
        settings=settings,
        validator_passed=False,
        regime=MarketRegime.STRONG_BULL,
    )
    assert failed.total == 100 and failed.route == "reject"


def test_no_regime_can_loosen_threshold_or_increase_risk(settings):
    for regime in MarketRegime:
        throttle = regime_throttle(regime, settings)
        assert throttle.score_threshold >= settings.buy_score_threshold
        assert 0 < throttle.risk_multiplier <= 1


def test_out_of_range_strategy_configuration_fails_at_startup():
    with pytest.raises(ValueError, match="pullback_max_atr_distance"):
        StrategySettings(pullback_max_atr_distance=2.01)
    with pytest.raises(ValueError, match="MINIMUM_HISTORY_SESSIONS"):
        SwingSettings(minimum_history_sessions=119)


def test_validate_setup_dispatches_only_the_three_approved_families(settings):
    bars, spy = _trend_inputs()
    result = validate_setup(
        SetupType.TREND_PULLBACK,
        bars,
        spy,
        sector_bars=None,
        settings=settings,
        as_of=AS_OF,
        event_risk_prohibited=False,
        liquidity_acceptable=True,
        market_regime=MarketRegime.BULL,
    )
    assert result.setup_type is SetupType.TREND_PULLBACK
