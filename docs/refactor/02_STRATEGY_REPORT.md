# 02 — Deterministic Strategy Validator, Universe, and Entry Score

Branch: `refactor/02-strategy-validator`  
Date: 2026-08-11

## Outcome

The scan path now has one pure deterministic authority, `src/swing/strategy.py`, for all three approved setup families. It returns a structured verdict containing every mandatory condition, all failed reason codes, computed features, a provisional structural stop, and a structural target. The scanner cannot turn a failed verdict into a candidate, and `AgentPipeline` only sends hard-valid `strong` or `very_strong` candidates to an LLM.

The universe is no longer a Python ticker literal. `config/universe/us_liquid_2026-08-11.csv` is a 439-row snapshot (422 S&P 500/Nasdaq-100-selected common stocks and 17 non-leveraged benchmark/sector ETFs), loaded through a configurable path. The report emitted by each scan records `us-liquid-2026-08-11-v1`.

No dependency was added. No sizing, position-risk, execution, broker-order, stop-management, or database-schema behavior was changed.

## Pipeline and funnel

`DeterministicSwingScanner.scan_with_report()` now performs:

1. Versioned broad universe load.
2. Blacklist, leveraged/inverse, security type, broker tradability/restriction, halt, and existing-holding exclusions.
3. Complete/fresh daily bars, configurable history, price, average share volume, average dollar volume, fresh spread, and broker tradability filters.
4. Coarse trend/relative-strength eligibility.
5. All three deterministic validators, with no partial-credit admission.
6. Configurable seven-component entry score and deterministic ranking.
7. LLM routing for only `strong` and `very_strong` candidates.

Every rejection has a stable `reason_code`, stage, ticker, optional validator details, and a counted funnel total. Empty strong output is `result: NO_TRADE` and remains a successful CLI result.

The blacklist loader now raises on missing, unreadable, or malformed files. It no longer interprets a missing safety file as an empty list.

## Validator contracts

### `TREND_PULLBACK`

Requires price above the medium average, positive medium-term ROC, short EMA above the medium average, rising medium and long averages, positive SPY-relative strength, price within the configured ATR distance of short/medium support, no low through structural support, a bullish continuation bar, bounded ATR/price, minimum provisional R:R, known/clear deterministic event status, and acceptable liquidity. Contracting pullback volume is computed and retained as a preference feature but is not a mandatory gate.

### `BREAKOUT_RETEST`

Requires a sufficiently long/tight prior consolidation, a close above calculated resistance by the breakout buffer, breakout volume expansion, a controlled retest without collapse, the level still held, bullish confirmation, positive trend, bounded volatility, liquidity, event clearance, and minimum provisional R:R. `entry_extended_chasing` is a mandatory rejection once price exceeds the configured ATR extension from resistance.

### `RELATIVE_STRENGTH_CONTINUATION`

Requires strong stock-vs-SPY RS, nonnegative stock-vs-sector and sector-vs-SPY RS, aligned short/medium/long trends, price above major averages, a tight consolidation, pre-trigger volume contraction, price/volume confirmation, minimum provisional R:R, bounded volatility/liquidity, known event status, and a non-hostile regime. Missing sector ETF bars reject with `sector_relative_strength_unavailable`.

## Entry score

The exact requested seven components are used: setup quality 30, relative strength 20, trend quality 15, volume confirmation 10, market regime 10, sector strength 5, and R:R 10. Weights are configurable, individually bounded, and must sum to 100. `render_entry_score()` produces the per-component `earned/max`, total, and route display.

Routes are: below 70 `reject`; 70–below the regime-adjusted strong threshold `watchlist`; at/above the adjusted strong threshold `strong`; and at/above 85 `very_strong` when the regime threshold is also met. The existing 90 A+ threshold remains the separate risk-engine A+ gate. A hard validator failure always routes to `reject`, including a numeric score of 100.

## Regime throttle invariant

`regime_throttle()` can only add to the baseline score threshold and multiply baseline risk by a value no greater than 1. Strong-bull/bull leave the baseline unchanged; neutral/choppy/hostile regimes progressively tighten it. High-volatility risk-off returns `no_trade=True`. Startup validation and tests enforce ordered, nonnegative additions and non-increasing risk multipliers.

This prompt does not apply those risk multipliers to position sizing; prompt 03 owns that integration. Existing bearish risk vetoes were not relaxed.

## Parameters introduced or made authoritative

All defaults are research starting points, not claims of permanent edge.

### Universe, liquidity, routes, and regime

| Parameter | Default | Startup range/invariant | Rationale |
|---|---:|---|---|
| `universe_path` | dated CSV | required loadable/malformed fails | Reproducible constituent snapshot |
| `do_not_trade_path` | `config/do_not_trade.yaml` | required loadable and `symbols:` key | Fail closed on exclusion data |
| `minimum_average_dollar_volume` | $20M | $20M–$5B | Research starting point for executable liquidity |
| `minimum_history_sessions` | 160 | 120–200 and ≥ long MA | Excludes recent IPOs while supporting long trend |
| `borderline_score_threshold` | 70 | 0–below strong threshold | Watchlist route without LLM review |
| `neutral_score_threshold_addition` | 5 | 0–30, ordered | Tighten neutral selection |
| `choppy_score_threshold_addition` | 10 | 0–30, ordered | Tighten choppy selection further |
| `hostile_score_threshold_addition` | 20 | 0–30, ordered | Very high hostile threshold |
| `neutral_risk_multiplier` | 0.75 | 0–1, ordered downward | Exposes prompt-03 tightening only |
| `choppy_risk_multiplier` | 0.50 | 0–1, ordered downward | Exposes prompt-03 tightening only |
| `hostile_risk_multiplier` | 0.25 | 0–1, ordered downward | Minimal hostile exposure if ever allowed |

Existing authoritative filter defaults remain price > $5, 20-session ADV > 1,000,000 shares, spread < 0.5%, tradable true, halted false, and earnings beyond five trading days.

### Setup geometry

| Parameter | Default | Bounded range | Rationale |
|---|---:|---:|---|
| `short_ema_period` | 20 | 10–30 | Short swing trend and liquidity window |
| `atr_period` | 14 | 10–30 | Conventional swing-volatility horizon |
| `medium_ma_period` | 50 | 40–75 | Medium trend structure |
| `long_ma_period` | 150 | 100–200 | Long trend within configurable history floor |
| `trend_slope_lookback` | 10 | 5–30 | Avoid one-bar slope noise |
| `minimum_trend_slope` | 0 | 0–5% | Requires non-declining trend by default |
| `relative_strength_lookback` | 63 | 40–126 | Roughly one quarter of sessions |
| `minimum_spy_relative_strength` | 0 | 0–20% | Pullbacks must outperform SPY |
| `strong_spy_relative_strength` | 3% | 1–30% | Higher bar for RS continuation |
| `minimum_sector_relative_strength` | 0 | 0–20% | Stock and sector cannot lag |
| `pullback_max_atr_distance` | 1.25 ATR | 0.25–2 ATR | Defines “near support” without chasing |
| `structural_break_atr` | 0.5 ATR | 0–2 ATR | Provisional noise allowance below structure |
| `minimum_atr_pct` | 0.5% | 0–3% | Rejects unusably inert series |
| `maximum_atr_pct` | 8% | 3–15% | Rejects excessive volatility |
| `consolidation_sessions` | 20 | 10–40 | Meaningful breakout base |
| `consolidation_max_range_pct` | 12% | 3–25% | Rejects loose “consolidations” |
| `breakout_buffer_pct` | 0.3% | 0–3% | Avoids treating a touch as breakout |
| `breakout_volume_ratio` | 1.20× | 1–3× | Requires breakout participation |
| `retest_hold_tolerance_pct` | 1% | 0–5% | Allows a controlled level test |
| `retest_collapse_tolerance_pct` | 3% | 0–10% | Separates retest from failed breakout |
| `maximum_breakout_extension_atr` | 1.25 ATR | 0.25–3 ATR | Mandatory anti-chase gate |
| `rs_consolidation_sessions` | 10 | 5–30 | Short constructive pause |
| `rs_consolidation_max_atr` | 4 ATR | 1.5–8 ATR | Bounds RS continuation base width |
| `contraction_volume_ratio` | 0.90× | 0.5–1.1× | Detects pre-expansion contraction |
| `confirmation_volume_ratio` | 1.10× | 0.8–3× | Confirms RS expansion |
| `trend_target_atr_extension` | 3 ATR | 0.5–5 ATR | Provisional target beyond prior swing high |
| `measured_move_multiple` | 2.5× | 0.5–3× | Provisional breakout/RS measured move |
| `maximum_bar_age_calendar_days` | 7 | 1–10 | Weekend-tolerant stale daily-bar gate |

Moving-average periods must be strictly ordered; ATR bounds must be ordered; strong RS cannot be weaker than minimum RS.

### Score weights

| Weight | Default | Bounded range/invariant |
|---|---:|---|
| Setup quality | 30 | 0–50 |
| Relative strength | 20 | 0–50 |
| Trend quality | 15 | 0–50 |
| Volume confirmation | 10 | 0–50 |
| Market regime | 10 | 0–50 |
| Sector strength | 5 | 0–50 |
| Risk/reward | 10 | 0–50 |

All seven must total exactly 100.

## Tests and verification

`tests/swing/test_strategy.py` adds synthetic, network-free coverage for valid and broken trend pullbacks (every mandatory reason code), valid/failed/extended breakouts, valid/weak/missing-sector RS continuation, insufficient R:R, stale/incomplete data, exact filter boundaries, blacklist/leveraged exclusions, unloadable blacklist, deterministic scoring, hard-failure precedence, regime tightening, render output, setup dispatch, and out-of-range startup configuration.

Verification performed:

- `poetry run pytest` — **passed: 157 tests, 0 failures, 0 skipped** (15 dependency/deprecation warnings).
- Black `--check` on all 14 Python files changed by this refactor — passed.
- isort `--check-only` on all 14 Python files changed by this refactor — passed.
- flake8 on all 14 changed Python files with the repository's 420-column Black policy and Black-compatible `E203` exclusion — passed.
- `npx --yes pyright` — **passed: 0 errors, 0 warnings**.
- `git diff --check` — passed.
- Universe CSV validation — 439 unique, complete rows; no duplicate symbol.

Poetry 2.4.1 is installed outside the active PowerShell `PATH` and was invoked directly. The host only has Python 3.14.7; the locked NumPy 1.26.4 and lxml 5.4.0 releases do not provide compatible Windows wheels and attempted source builds without an installed C++ compiler. Tests were therefore executed in Poetry's project virtual environment with a local, environment-only NumPy 2.5.2 wheel. No dependency file was changed. A Python 3.11–3.13 environment is still required to reproduce the lock exactly.

Repository-wide Black/isort checks also expose a large pre-existing formatting baseline outside this prompt (Black would reformat 143 files). Those unrelated files were deliberately not rewritten; all files owned or touched by this refactor pass their scoped checks.

## Prompt 03 integration

Each validator exposes `provisional_stop` and marks its calculation with `TODO(prompt-03)`. Prompt 03 must replace or validate these levels through the central structural-stop authority before sizing; it must not allow the LLM to move a stop closer to manufacture R:R. `RegimeThrottle.risk_multiplier` is also an integration input for prompt 03 and may only reduce baseline risk.

## Assumptions I made

- The 2026-08-11 static constituent selection is broad enough for this account after deterministic liquidity filtering; a human should verify every membership label against the licensed constituent files before live use.
- Non-leveraged SPY/QQQ/sector ETFs remain supported because the existing regime and sector system depends on them; every leveraged/inverse/volatility-linked ETF remains prohibited.
- A 7-calendar-day daily-bar freshness limit is necessary to tolerate weekends/holidays; quote freshness remains the existing 300 seconds.
- Volume contraction is a scored preference for trend pullbacks, but a mandatory gate for RS continuation.

## Fail-closed TODOs

- `TODO(prompt-03)`: replace/validate all provisional structural stops through the central stop authority before sizing.
- Production assets currently reject with `halt_status_unknown`; Alpaca `active/tradable` metadata is not treated as proof that a symbol is not halted. Integrate a reliable real-time halt feed.
- Production assets currently reject broader event status as unknown. Integrate deterministic FDA, M&A, index-rebalance, and investor-day calendars; earnings alone is insufficient.
- RS continuation rejects with `sector_relative_strength_unavailable` whenever its mapped sector ETF bars are missing.

## Anything I could not verify

- Exact locked-dependency behavior on Python 3.11–3.13; this host only provides Python 3.14.7, which is incompatible with the locked NumPy/lxml wheels.
- A clean repository-wide Black/isort baseline without a separate, scope-expansive formatting cleanup of legacy files.
- Live broker asset/quote payload compatibility, halt status, restrictions, or real spread behavior without credentials; no credentialed request was made.
- Real-data pass rates or expectancy; synthetic validation proves rules, not strategy edge.
- Licensed/authoritative point-in-time constituent accuracy for every row in the static universe snapshot.
