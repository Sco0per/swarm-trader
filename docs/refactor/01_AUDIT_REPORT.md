# 01 — Audit, Gap Report, and Static Cleanup

Branch: `refactor/01-audit-cleanup`
Date: 2026-08-11
Scope: read-only audit + static cleanup only. **No strategy, risk, sizing, execution, or schema logic was changed.**

---

## 0. Executive summary

The README's claims about `src/swing/` are **substantially true**. The swing package is
a genuinely deterministic, fail-closed risk engine — better than I expected from the
prompt's framing. `src/swing/market.py` really does classify setup geometry; it does
not accept an LLM's label. `src/swing/risk.py` really is the sole authority for
quantity, and `SwingSettings.__post_init__` really does refuse to start on a weakened
ceiling.

The problem is everything **outside** `src/swing/`. Three legacy modules still hold
live, unguarded `POST /orders` calls against Alpaca, and one of them
(`rebalance.py --execute`) has no disable guard at all. The swing engine is not
bypassed by the swing path; it is bypassed by *sibling* code that shares the same
credentials.

Tally across the 32 work areas: **11 DONE, 13 PARTIAL, 8 MISSING.**

---

## 1. Work-area gap table

Verdicts are against the target end state, not against the previous refactor's own goals.

| # | Work area | Verdict | Evidence |
|---|---|---|---|
| 1 | Dual-mode concept removed from production paths | **PARTIAL** | `src/swing/` is clean — a word-boundary sweep of `src/swing/**` for `day_trade\|TRADING_MODE\|short\|leveraged\|margin\|options\|crypto\|flatten` returns only rejection logic ([risk.py:97-98](../../src/swing/risk.py#L97-L98), [alpaca.py:59](../../src/swing/brokers/alpaca.py#L59)) and prose. But `src/config.py:64` still reads a `TRADING_MODE` env var, and `risk_manager.py:446`, `gather_data.py:446` retain live `if mode == "day"` branches. |
| 2 | `trading_mode.json`, `TRADING_MODE`, `--mode`, `ALPACA_DAY_*`, day routing eliminated | **PARTIAL** | Was **MISSING** at audit start: `trading_mode.json` existed at repo root and `src/accounts.py:57-65` still loaded `ALPACA_DAY_API_KEY`/`ALPACA_DAY_API_SECRET`. Both removed in this prompt (see §7). Remaining: `src/config.py:64` `os.environ.get("TRADING_MODE")`; `execute_trades.py:322` `os.environ.get("TRADING_MODE", "swing")`. Both are gated by `resolve_mode()` raising on non-swing ([src/config.py:63-67](../../src/config.py#L63-L67)), so they are inert, but the surface remains. |
| 3 | Only the three approved setup types reachable | **DONE** | `SetupType` has exactly three members ([models.py:17-20](../../src/swing/models.py#L17-L20)); it is a `str, Enum` on a pydantic model with `extra="forbid"`, so an LLM cannot introduce a fourth ([agents.py:66-70](../../src/swing/agents.py#L66-L70)). `market.py:249-262` emits only these three and returns `None` otherwise. |
| 4 | Deterministic validator that validates setup **geometry** | **DONE** | [market.py:239-262](../../src/swing/market.py#L239-L262) computes recent high/low, pullback distance in ATR units, MA50 slope, breakout-then-retest confirmation, and 5-day range compression *before* assigning a setup. `risk.py:134-135` then rejects any proposal whose `setup_type` disagrees with the deterministic candidate. This is real geometry validation, not label acceptance. |
| 5 | Transparent, configurable score with reject/watchlist/review thresholds | **PARTIAL** | Score is fully transparent and component-wise persisted (`WEIGHTS`, [market.py:151-161](../../src/swing/market.py#L151-L161); `score_components` stored at [database.py:458-479](../../src/swing/database.py#L458)). Three thresholds exist and are env-configurable — `buy_score_threshold` 80, `preferred_score_threshold` 85, `a_plus_score_threshold` 90 ([config.py:65-67](../../src/swing/config.py#L65-L67)). **But** the nine `WEIGHTS` are a hardcoded class attribute, not configurable; and `preferred_score_threshold` is defined and validated yet **never read anywhere** — it is dead config. There is no watchlist/review tier: sub-threshold candidates are simply dropped at [agents.py:139](../../src/swing/agents.py#L139). |
| 6 | Broad configurable universe with liquidity/price/history/blacklist filters | **PARTIAL** | Filters exist and are enforced: history ≥200 bars ([market.py:194](../../src/swing/market.py#L194)), price and ADV ([market.py:199](../../src/swing/market.py#L199), re-checked [risk.py:101-104](../../src/swing/risk.py#L101-L104)), spread ([market.py:204](../../src/swing/market.py#L204)), blacklist ([risk.py:95-96](../../src/swing/risk.py#L95-L96)). **But** the universe itself is a hardcoded ~200-symbol Python literal ([universe.py:26-76](../../src/swing/universe.py#L26-L76)) with no config hook, and the blacklist is applied only at risk time — blacklisted symbols are still scanned, scored, and sent to the LLM. |
| 7 | IPO / minimum-trading-history filter | **PARTIAL** | Effectively enforced by `if len(frame) < 200: continue` ([market.py:194](../../src/swing/market.py#L194)) — an IPO cannot produce 200 daily bars. But this is incidental to the MA200 requirement, is not named, is not configurable, and emits no reason code, so a scanner refactor could silently remove it. |
| 8 | Deterministic earnings/event gate that fails closed | **PARTIAL** | Fails closed correctly at the risk boundary: unknown earnings distance for a non-ETF is an outright reject ([risk.py:128-132](../../src/swing/risk.py#L128-L132)). **But** the only event checked is earnings. There is no gate for FDA dates, M&A, index rebalance, or investor days. The LLM's `FundamentalEventAnalysis.has_blocking_event_risk` ([agents.py:31](../../src/swing/agents.py#L31)) is the only broader coverage, and an LLM flag is not a deterministic gate. Earnings data itself degrades to `None` on lookup failure ([data_feed.py:285](../../src/swing/data_feed.py#L285)) — correct, because `None` then rejects. |
| 9 | Volatility-aware structural stops | **MISSING** | **The single largest strategy gap.** The stop is chosen entirely by the LLM portfolio manager ([agents.py:72](../../src/swing/agents.py#L72) → [agents.py:189](../../src/swing/agents.py#L189)). `risk.py` only checks ordering (`stop < quote.last < target`, [risk.py:145](../../src/swing/risk.py#L145)) and resulting R:R. `candidate.atr` and `candidate.support` are computed ([market.py:264](../../src/swing/market.py#L264)) and stored, but **nothing ever compares `proposal.stop` against them.** An LLM may legally propose a 0.2×ATR stop directly under the current price; risk.py will accept it and size a large position off the tiny risk-per-share. The max-position ceiling (35%) is the only backstop. |
| 10 | Risk-based sizing from capital at risk | **DONE** | [risk.py:195-211](../../src/swing/risk.py#L195-L211): `quantity = floor(equity × applied_risk_pct / risk_per_share)`, then `min()` against cash/buying-power/exposure. Whole shares via `math.floor`. Correctly matches `REFACTOR_RULES` §1 "risk-based size is authoritative; max-position-percent can only reduce". |
| 11 | Portfolio open-risk accounting | **DONE** | Enforced twice: in-memory at [risk.py:217-218](../../src/swing/risk.py#L217-L218), then re-checked inside a `BEGIN IMMEDIATE` transaction that also counts unlinked pending reservations ([database.py:589-600](../../src/swing/database.py#L589-L600)). This is the strongest control in the repository. |
| 12 | Correlation / cluster risk limits | **MISSING** | Zero occurrences of `cluster`/`correlation`/`correlated` under `src/swing/`. `candidate.sector` is recorded ([execution.py:292](../../src/swing/execution.py#L292)) but never constrains anything. Three positions in three semiconductor names would pass every check. No sector→cluster mapping data source exists in the repo. |
| 13 | Regime used only as a selectivity throttle | **PARTIAL** | It is used as *both* a throttle and a hard veto. Throttle: 15 of 100 score points ([market.py:152](../../src/swing/market.py#L152), `regime_fraction` [market.py:267-270](../../src/swing/market.py#L267-L270)). Veto: BEAR and HIGH_VOLATILITY_RISK_OFF are outright rejected at [risk.py:142-143](../../src/swing/risk.py#L142-L143). The hard veto is stricter than "throttle only" and I have **not** relaxed it (§2.5 forbids weakening controls) — flagging it as a scope question for prompt 03. |
| 14 | Pre-execution freshness, tradability, spread, chase checks | **DONE** | All four present and fail-closed: freshness on quote, market timestamp, candidate, and account with explicit future-timestamp rejection ([risk.py:109-127](../../src/swing/risk.py#L109-L127)); broker asset tradability/class/leverage ([risk.py:56-64](../../src/swing/risk.py#L56-L64)); spread with `None` → reject ([risk.py:105-108](../../src/swing/risk.py#L105-L108)); 0.25R chase limit ([risk.py:155-157](../../src/swing/risk.py#L155-L157)). State is refreshed immediately before validation ([execution.py:98-106](../../src/swing/execution.py#L98-L106)). |
| 15 | Explicit position lifecycle / exit state machine | **PARTIAL** | A `TradeStatus` enum exists ([models.py:40-47](../../src/swing/models.py#L40-L47)) and statuses are written, but there is **no state machine**: transitions are ad-hoc `update_trade(status=...)` calls scattered through [reconciliation.py:157](../../src/swing/reconciliation.py#L157), `:241`, `:247`, and `postmortem.py:80`. No table of legal transitions and no rejection of an illegal one — contrast `transition_lesson` ([database.py:810-829](../../src/swing/database.py#L810-L829)), which *does* have one. Exits are entirely delegated to broker bracket legs; there is no managed exit logic. |
| 16 | Time-stop mechanism | **MISSING** | No `max_hold_days`, `expected_hold_days`, or `time_stop` anywhere in `src/`. `holding_period_days` is computed only *after* close, for analytics ([postmortem.py:79](../../src/swing/postmortem.py#L79)). A GTC bracket with no time stop can hold a dead position indefinitely. |
| 17 | Immutable per-trade thesis contract | **PARTIAL** | The thesis is captured — `bull_thesis`, `bear_thesis`, `pm_reasoning`, `invalidation`, `initial_stop`, `planned_rr` are all written at entry ([execution.py:283-311](../../src/swing/execution.py#L283-L311)). **But nothing makes it immutable**: `update_trade()` builds `UPDATE trades SET {any column}` from `**values` with no column allowlist ([database.py:655-662](../../src/swing/database.py#L655-L662)), so `initial_stop` or `bull_thesis` can be silently rewritten. There is also no invalidation *check* against the thesis while the position is open. |
| 18 | R-multiple journaling with MAE/MFE | **DONE** | [postmortem.py:74-91](../../src/swing/postmortem.py#L74-L91) computes `realized_r`, `mfe`, `mae`, `mfe_r`, `mae_r` against the **initial** stop (correct denominator) and persists all five ([database.py:110-115](../../src/swing/database.py#L110-L115)). MFE/MAE derive from `trade_snapshots` extremes ([database.py:676-684](../../src/swing/database.py#L676-L684)). Caveat: snapshots are only written during `reconcile`, so intraday extremes between reconciliations are lost — MFE/MAE are close-to-close approximations. |
| 19 | Expectancy analytics segmented by setup/regime/sector/score bucket | **DONE** | All four segments plus stock, holding period, and R:R bucket ([analytics.py:36-46](../../src/swing/analytics.py#L36-L46)), each carrying `sample_size` and a `statistically_meaningful` flag against `minimum_statistical_sample` ([analytics.py:61-64](../../src/swing/analytics.py#L61-L64)). Unavailable segments return an explicit `not_collected_or_insufficient` placeholder rather than a fabricated zero ([analytics.py:47-49](../../src/swing/analytics.py#L47-L49)) — exactly the right pattern. |
| 20 | Process-quality postmortems separate from outcome | **DONE** | `PostmortemAssessment` forces explicit per-question process answers ([postmortem.py:24-39](../../src/swing/postmortem.py#L24-L39)); `followed_strategy=False` overrides any classification to `RULE_VIOLATION` ([postmortem.py:107-109](../../src/swing/postmortem.py#L107-L109)); a winner defaults to `UNKNOWN`, not "good" ([postmortem.py:115-116](../../src/swing/postmortem.py#L115-L116)). One defect noted in §4.6. |
| 21 | Broker reconciliation, fail-closed on mismatch | **DONE** | Mandatory before every non-dry submission ([execution.py:84-97](../../src/swing/execution.py#L84-L97)). `_finish()` latches `reconciliation_halt` on **any** discrepancy ([reconciliation.py:283-284](../../src/swing/reconciliation.py#L283-L284)), and a broker snapshot failure is itself a discrepancy ([reconciliation.py:80-82](../../src/swing/reconciliation.py#L80-L82)). Untracked broker positions and untracked open orders both halt ([reconciliation.py:252-269](../../src/swing/reconciliation.py#L252-L269)). |
| 22 | Broker-native protective orders and idempotent execution | **DONE** | GTC bracket with both legs in one atomic order ([alpaca.py:89-98](../../src/swing/brokers/alpaca.py#L89-L98)). Idempotency is layered: deterministic `uuid5` intent key ([execution.py:39-42](../../src/swing/execution.py#L39-L42)), `client_order_id` sent to the broker ([alpaca.py:95](../../src/swing/brokers/alpaca.py#L95)), a `PRIMARY KEY` on `entry_admissions.intent_id` ([database.py:278](../../src/swing/database.py#L278)), and partial unique indexes on `ORDER_SUBMITTED`/`ORDER_RESERVED` ([database.py:331-334](../../src/swing/database.py#L331-L334)). A post-reservation broker failure latches a halt and forbids retry ([execution.py:183-195](../../src/swing/execution.py#L183-L195)). |
| 23 | Stale-data protection before every order | **DONE** | See #14. Covers quote retrieval time, quote market time, candidate data (4-day ceiling), and account snapshot, each with a symmetric future-timestamp rejection ([risk.py:109-127](../../src/swing/risk.py#L109-L127)). `market_timestamp` is enforced timezone-aware at the schema level ([models.py:79-84](../../src/swing/models.py#L79-L84)). |
| 24 | Durable state persistence and **safe schema migrations** | **PARTIAL** | Persistence is solid (SQLite + Turso, FKs, WAL, busy_timeout — [database.py:363-381](../../src/swing/database.py#L363-L381)). **Migrations do not exist.** `initialize()` runs `CREATE TABLE IF NOT EXISTS` then `INSERT OR IGNORE INTO schema_migrations VALUES(3, ...)` ([database.py:383-389](../../src/swing/database.py#L383-L389)). There is no v1→v2→v3 path, no version read, and no `ALTER TABLE`. A database created at schema 1 will be **stamped version 3 while missing every column added since** — silent, and the mismatch surfaces as a runtime SQL error mid-trade. |
| 25 | Decision versioning per trade | **PARTIAL** | `strategy_version` is on every trade and FK-constrained ([database.py:96](../../src/swing/database.py#L96)); `model_name` and `schema_version` are on every agent decision ([database.py:168-170](../../src/swing/database.py#L168-L170)). **Missing:** config version/hash, scanner version, and prompt version. The prompt text lives inline in [llm_backend.py:69-73](../../src/swing/llm_backend.py#L69-L73) and [agents.py:157-161](../../src/swing/agents.py#L157-L161) and is unversioned — a prompt edit is invisible in the trade record, which breaks the expectancy comparison the whole experiment depends on. |
| 26 | Structured logging with machine-readable reason codes | **PARTIAL** | Reason codes are excellent and machine-readable: every rejection carries a stable `rule` slug and is persisted to `rule_violations` ([risk.py:28-37](../../src/swing/risk.py#L28-L37)), with ~45 distinct codes. **But there is no logging at all** — zero `import logging` under `src/swing/`. Observability is entirely "read the database", and CLI output is `print(json.dumps(...))`. A cron routine's stdout is the only trace of a failure path that did not reach the database. |
| 27 | Scheduling: frequent deterministic scans, sparse LLM evaluation | **PARTIAL** | The LLM is correctly sparse and gated — analysis runs only above the score threshold, capped at 20 candidates and 5 PM finalists ([agents.py:139-141](../../src/swing/agents.py#L139-L141), `:171`). Roles are split cheap/expensive ([.env.example:26-31](../../.env.example)). **But scans are not frequent**: exactly one `scan` at 9:35am and one `run` at 10:03am ([docs/CRON_AGENTS.md](../CRON_AGENTS.md) routine table). There is no intraday re-scan, so a setup that triggers at 2pm is invisible until the next day. |
| 28 | Notification architecture | **MISSING** | Nothing under `src/swing/`. The only notification surface is the cloud routine's own push notification of its stdout ([docs/CRON_AGENTS.md](../CRON_AGENTS.md), "Known limitations"). The legacy `--telegram` flags in `check_portfolio.py:43` and `run_hedge_fund.py:136` are output formatters, not a notification channel. A latched drawdown halt notifies no one. |
| 29 | Small-account ($2,000) support and edge cases | **PARTIAL** | Deliberately correct on the core: whole shares only ([risk.py:205](../../src/swing/risk.py#L205)), sizing from live equity not a constant, and `position_too_small` rejects rather than rounding up ([risk.py:210-211](../../src/swing/risk.py#L210-L211)). Test fixtures use $2,000 ([conftest.py:65](../../tests/swing/conftest.py)). **But** at $2,000 with 0.50% risk = $10 of risk, a $150 stock with a $3 structural stop yields 3 shares ($450, 22% of equity) — the 35% exposure ceiling and 2% open-risk cap will bind constantly, and there is no `min_price`-vs-account-size sanity rule, no cash reserve, and no warning when the account is too small for the universe. Untested against a real small account. |
| 30 | AutoResearch isolated from production | **DONE** (as of this prompt) | **Confirmed by dependency analysis: no module under `src/swing/` imports anything from `autoresearch/`.** The only importer is `src/agents/autoresearch_agent.py:44`, reached via `src/utils/analysts.py:23` → the legacy LangGraph app, which the swing path never calls. `src/autoresearch_swing/` is a separate package that shares no code with `autoresearch/` (it imports only `src.swing.config`, `src.swing.database`, and its own `fitness`). Quarantine headers added in this prompt (§7). |
| 31 | Test coverage per area | **PARTIAL** | 55 swing tests cover risk gates, sizing, halts, admissions serialization, unknown-outcome recovery, stop widening, postmortems, scanner filtering, universe, and data feed — genuinely good. **Uncovered:** stops proposed vs. structure (#9, nothing to test), lifecycle transitions (#15), migrations (#24), correlation (#12), time stop (#16). Three legacy tests assert forbidden behaviour — see §3.8. **I could not execute the suite** (see §8). |
| 32 | Secret handling and logging hygiene | **DONE** | Keys are read from env and placed directly into request headers, never logged, never persisted, never put in a prompt ([alpaca.py:19-23](../../src/swing/brokers/alpaca.py#L19-L23), [data_feed.py:73-78](../../src/swing/data_feed.py#L73-L78), [llm_backend.py:52-63](../../src/swing/llm_backend.py#L52-L63)). `.env` is gitignored; `.env.example` holds placeholders only. `config/do_not_trade.yaml` explicitly warns against PII. One residual risk, not a defect: `docs/CRON_AGENTS.md` documents that cloud-routine secrets live in a plaintext environment box. |

**Tally: 11 DONE · 13 PARTIAL · 8 MISSING**

MISSING: #9 structural stops, #12 correlation/cluster, #16 time stop, #28 notifications, and — counting the sub-item that carries the most operational risk — #24's migration path. Plus #2 as it stood at audit start (now partially closed).

---

## 2. Active swing components — what actually runs in a production cycle

Traced from the five cron routines ([docs/CRON_AGENTS.md](../CRON_AGENTS.md)) through `src/swing/cli.py`.

| Component | File | Role in the cycle |
|---|---|---|
| CLI entrypoint | `src/swing/cli.py` | The only production entrypoint; `swing-trader` in `pyproject.toml:64` |
| Settings | `src/swing/config.py` | Loaded first by every command; refuses startup on a weakened ceiling |
| Universe | `src/swing/universe.py` | ~200 hardcoded symbols + 28 ETFs |
| Data feed | `src/swing/data_feed.py` | Alpaca daily bars (batched, disk-cached), yfinance earnings (threaded, 8s deadline, Turso-cached) |
| Scanner + regime | `src/swing/market.py` | `DeterministicSwingScanner`, `MarketRegimeEngine` — no LLM |
| LLM pipeline | `src/swing/agents.py` + `llm_backend.py` | technical → fundamental → bull → bear red-team → PM verdict, all schema-constrained |
| Risk authority | `src/swing/risk.py` | ~45 independent gates; the only producer of an approved quantity |
| Execution | `src/swing/execution.py` | reconcile → refresh → risk → broker review → serialized admission → place |
| Admission ledger | `src/swing/database.py` `reserve_entry_admission` | `BEGIN IMMEDIATE` serialization of day/week/position/open-risk limits |
| Broker | `src/swing/brokers/alpaca.py` | Paper-only; GTC bracket |
| Reconciler | `src/swing/reconciliation.py` | Runs pre-submission and as its own routine; fails closed |
| Stops | `src/swing/stops.py` | Only supported amendment path; tighten-only |
| Postmortem | `src/swing/postmortem.py` | Close metrics, R/MFE/MAE, observation creation, loss-streak latch |
| Analytics / reporting | `src/swing/analytics.py`, `reporting.py` | Expectancy segments, graduation criteria |
| Lessons | `src/swing/lessons_review.py` → `src/autoresearch_swing/research.py` | Sunday hypothesis aggregation |
| Models | `src/swing/models.py` | Every boundary schema; `extra="forbid"` throughout |

Everything else in the repository is inactive with respect to a production cycle.

---

## 3. Enumerated findings

### 3.1 Legacy components — dead, orphaned, or superseded

| File | Status | Still imported by? |
|---|---|---|
| `trading_mode.json` | **Dead — deleted this prompt** | Nothing. Zero code references (only README prose and docstrings). |
| `src/accounts.get_all_accounts()` | **Dead — deleted this prompt** | Zero callers repo-wide. |
| `performance_tracker.py` | Dead (a 2-line tombstone comment, no code) | Nothing |
| `src/tools/api_original.py` | Superseded by `api_free.py`/`api.py` | Nothing; referenced only in a `.env.example` comment |
| `src/swing/brokers/robinhood_mcp.py` | Intentionally nonfunctional skeleton; every method raises | Nothing — not even `cli.py` |
| `src/backtester.py` | Superseded by `src/backtesting/` | Nothing |
| `portfolio_monitor.py` | Neutered fail-closed shim; `run_monitor()` does no I/O | Only `tests/swing/test_safety_remediation.py:58` |
| `src/agents/*` (20 personality agents) | Legacy; not on the swing path | `src/main.py`, `src/utils/analysts.py`, `app/backend/services/graph.py` |
| `app/` (FastAPI + React) | Legacy research UI for the personality swarm | Self-contained |
| `src/backtesting/` | Legacy long/short backtester | `pyproject.toml:63` still declares a `backtester` console script |
| `autoresearch/` | Quarantined intraday experiment | `src/agents/autoresearch_agent.py` only |
| `check_moves.py`, `check_portfolio.py`, `gather_data.py`, `intel_exchange.py`, `run_analysis.py`, `scan_market.py`, `trade_alerts.py`, `trade_journal.py`, `performance_tracker_v2.py` | Legacy operational scripts | Each other, loosely; nothing in `src/swing` |
| `src/config.py` | Legacy compatibility shim | `src/accounts.py`, `execute_trades.py`, `rebalance.py`, `run_hedge_fund.py`, `src/agents/mordecai.py` |
| `risk_manager.py` | Superseded by `src/swing/risk.py` | `execute_trades.py:66`, `src/alpaca_integration.py:331` (both in already-blocked branches) |

I did **not** delete the large legacy subtrees. `pyproject.toml` still declares
`backtester` as a console script, `app/` is a declared package, and I cannot run the
test suite to confirm nothing breaks (§8). Deletion belongs in a prompt where the
suite can be executed.

### 3.2 Duplicated implementations

| Responsibility | Swing owner | Root-level duplicate | Which wins |
|---|---|---|---|
| Risk validation / sizing | `src/swing/risk.py` | `risk_manager.py` (20KB, its own `validate_trade`, thresholds, circuit breaker) | Swing — but the duplicate is still imported and called by `execute_trades.py` |
| Order placement | `src/swing/execution.py` + `brokers/alpaca.py` | `execute_trades.py::place_order`, `src/alpaca_integration.py::_place_*` (6 variants), `rebalance.py::place_sell_order` | Swing on the swing path; the duplicates are independently reachable |
| Market scanning | `src/swing/market.py` | `scan_market.py` (Alpaca movers/actives, intraday) | Swing; `scan_market.py` is not called by anything |
| Stop monitoring | `src/swing/stops.py` + broker bracket legs | `portfolio_monitor.py` | Swing; the duplicate is neutered |
| Trade journal | `trades` table | `trade_journal.py` → `data/trade_journal.jsonl` | Swing; the JSONL journal is still written by `execute_trades.py:519` |
| Performance tracking | `src/swing/analytics.py` | `performance_tracker_v2.py` → `data/performance.json` | Swing; still invoked at `execute_trades.py:527` |
| Universe definition | `src/swing/universe.py` (~228 symbols) | `src/config.py::SWING_UNIVERSE` (18 symbols) | Swing; `rebalance.py` uses the *other* one to decide what to sell |

The last row is a live hazard: `rebalance.py --execute` would market-sell every
position not in `src/config.py`'s 18-symbol list — which is most of the actual swing
universe.

### 3.3 Contradictory configuration

| Setting | Value A | Value B | Notes |
|---|---|---|---|
| Max trades per day | `1` — `src/config.py:47` `max_trades_per_day` | `1` — `src/swing/config.py:59` | Agree with each other; **both contradict `REFACTOR_RULES` §1, which resolves this to 2.** See §5. |
| Daily loss limit | `0.08` — `src/config.py:44` | No daily limit in swing; nearest is `halt_drawdown_pct=0.08` (peak-to-trough, not daily) — `src/swing/config.py:55` | Different semantics under similar names |
| Weekly loss limit | `0.08` — `src/config.py:45` | **Does not exist** in swing | `REFACTOR_RULES` §1 requires 4–5% |
| Max position pct | `0.35` — `src/config.py:41` and `:98` | `0.35` — `src/swing/config.py:57` | Agree |
| Max sector pct | `0.35` — `src/config.py:42`, `:99` | **Not enforced anywhere** | Advisory-only in legacy, absent in swing |
| Risk per trade | `0.005` — `src/config.py:53` | `0.005` — `src/swing/config.py:50` | Agree; **both contradict `REFACTOR_RULES` §1 (0.75%)** |
| Stop loss pct | `0.0` — `src/config.py:43`, `:100` | Structural stop, no percentage — `src/swing/risk.py` | Legacy zeroed out deliberately (see `portfolio_monitor.py` docstring) |
| Cooldown / earnings window | `10` / `5` trading days — `src/swing/config.py:71-73` | Not present in legacy | Swing-only |
| Score threshold | `80` — `.env.example:18` and `src/swing/config.py:65` | — | Agree |

### 3.4 Contradictory documentation

| Claim | Source | Reality |
|---|---|---|
| "Full short selling support", "Dual-mode: swing + day with autonomous switching", "`trading_mode.json` — agent decides or human overrides" | README (pre-rewrite) lines 172–176 | `resolve_mode()` raises on non-swing; `Decision` has no SHORT; `trading_mode.json` was referenced by no code. **Fixed by the README rewrite.** |
| "Mandatory EOD flatten (12:45 PM Mon-Fri)" cron, "Day Trading Agent Crons" | README (pre-rewrite) lines 688–735 | No day path exists. **Removed.** |
| "Code-level remediation is complete" | README (pre-rewrite) line 50 | True for the audited remediation scope; misleading as a general statement given #9/#12/#16/#24. **Replaced with an explicit gap list.** |
| "`src/swing/database.py`: … WAL mode, **migrations**, and permanent audit records" | `docs/ADAPTIVE_SWING_ARCHITECTURE.md`, "New component boundaries" | There is no migration mechanism (#24). **Not yet corrected** — that doc is otherwise accurate and I did not want to edit it piecemeal in an audit prompt. |
| "AutoResearch loop — AI evolves strategy.py overnight" listed as a feature | README (pre-rewrite) line 180 | `autoresearch/` is intraday-derived and quarantined. **Removed; replaced by the Historical Note.** |
| Risk model doc: "one new position per day" | `docs/RISK_MODEL.md` | Matches the code, **contradicts `REFACTOR_RULES` §1**. Left as-is; the code is the thing that must change (prompt 03), not the doc describing it. |
| `docs/REMEDIATION_STATUS.md`: "tests/swing: 55 passed, full repository: 99 passed" | that file | Plausible but **unverified in this session** — no Python runtime available (§8). |

### 3.5 Unsafe fallbacks — permissive result from missing/stale/invalid data

Every one of these is treated as a defect regardless of likelihood, per the prompt.

| # | Location | Fallback | Why it is unsafe |
|---|---|---|---|
| U1 | [market.py:235](../../src/swing/market.py#L235) | `sector_rs = … if sector_frame is not None else 0.0` | Missing sector-ETF bars silently become "sector is exactly neutral". `RELATIVE_STRENGTH_CONTINUATION` requires `sector_rs >= 0` ([market.py:257](../../src/swing/market.py#L257)) — **the missing-data default satisfies the gate.** A whole setup type can trigger on absent data. |
| U2 | [market.py:237](../../src/swing/market.py#L237) | `sector_trend = "UP" if … else "UNKNOWN"` | Persisted to the trade record as context; "UNKNOWN" is later grouped in analytics as if it were a real value. |
| U3 | [market.py:236](../../src/swing/market.py#L236) | `stock_sector_rs = … else None` | Stored as `None`; no consumer distinguishes "no sector data" from "measured zero". |
| U4 | [data_feed.py:312-320](../../src/swing/data_feed.py#L312-L320) | `is_tradable=True, is_halted=False, is_leveraged_or_inverse=False` hardcoded | Three safety-critical asset attributes are **assumed favourable** for the entire universe. Only the broker `get_asset()` call at [risk.py:56-64](../../src/swing/risk.py#L56-L64) actually verifies them, and only for the one symbol being submitted. A halted stock is scanned, scored, LLM-analysed, and only stopped at the last gate. Documented as a known limitation in the module docstring, which does not make it safe. |
| U5 | [risk.py:196](../../src/swing/risk.py#L196) | `requested = proposal.requested_risk_pct or risk_pct` | `requested_risk_pct` originates outside the deterministic path (`TradeProposal` is `model_validate`d straight from operator JSON in `paper-review`/`live-ticket`, [cli.py:120](../../src/swing/cli.py#L120)). It is capped by `min()` on the next line, so it can only reduce — **but a value of `0.0` would be falsy and silently fall through to the full default instead of sizing to zero.** `Field(gt=0)` currently blocks that; the pattern is fragile. |
| U6 | [risk.py:152](../../src/swing/risk.py#L152) | `exceptional_rr_evidence` boolean lets 1.5R pass a 2.0R floor | This is a **plain LLM/operator-supplied boolean with no evidence attached and no verification**. Any proposal that sets it true buys a 25% relaxation of the minimum R:R. `docs/RISK_MODEL.md` claims it "requires previously validated exact-context evidence" — no code checks that. |
| U7 | [postmortem.py:107](../../src/swing/postmortem.py#L107) | `followed_strategy = bool(assessment.get("followed_strategy", True))` | When the LLM postmortem call fails, the trade is recorded as having **followed strategy**. The optimistic default is the one that hides rule violations from the `zero_major_risk_failures` graduation criterion ([reporting.py:89](../../src/swing/reporting.py#L89)). |
| U8 | [postmortem.py:103-106](../../src/swing/postmortem.py#L103-L106) | Bare `except Exception: pass` around the LLM assessment | The failure is swallowed with no reason code and no `rule_violations` row. Combined with U7, a persistently broken postmortem model produces a clean-looking journal. |
| U9 | [data_feed.py:100-104](../../src/swing/data_feed.py#L100-L104) | `_cache_get` returns `None` on any exception | Correct direction (falls through to fetch), but the bare `except Exception` also swallows corrupt-cache signals. Low severity. |
| U10 | [reconciliation.py:245-248](../../src/swing/reconciliation.py#L245-L248) | An order in `ACTIVE_ORDER_STATUSES` with zero fills is set to `SUBMITTED` and **counted as reconciled** | Legitimate for a genuinely pending entry, but there is no age bound. An order stuck "accepted" for a week reconciles clean forever and keeps consuming a position slot and open-risk budget. |
| U11 | [market.py:86](../../src/swing/market.py#L86) | `rsi()` returns `50.0` when the computation is `NaN` | A neutral-looking number substituted for "not computable". Currently `rsi` is only recorded, not gated on — but it is stored on the trade and segmented in analytics. |
| U12 | [risk.py:90-91](../../src/swing/risk.py#L90-L91) | Paper-mode check is `not portfolio.account.is_paper` | `AlpacaPaperProvider.get_account()` **hardcodes `is_paper=True`** ([alpaca.py:38](../../src/swing/brokers/alpaca.py#L38)) rather than reading it from the broker. The check can therefore never fail for Alpaca — it is a tautology, not a verification. Mitigated by the hardcoded `paper-api` base URL, but the control is not doing what it appears to do. |

### 3.6 Bypass paths — code that can reach a broker without `src/swing/risk.py` and `src/swing/execution.py`

**This is the most important section of the report.** All three paths use the same
`ALPACA_API_KEY` / `ALPACA_API_SECRET` as the swing system, against the same paper
account, so they act on the same positions the swing engine believes it controls.

#### B1 — `rebalance.py --execute` · **no guard whatsoever** · HIGHEST SEVERITY

- Order call: [`rebalance.py:55`](../../rebalance.py#L55) `requests.post(f"{ALPACA_BASE_URL}/orders", …)`
- Reachability: `poetry run python rebalance.py --execute`. Nothing else required.
- Guards: **none.** No `TRADING_ENABLED` check, no kill-switch check, no
  `reconciliation_halt` check, no dry-run-only gate, no disabled-legacy-path error.
  Contrast `execute_trades.py:321-326` and `src/alpaca_integration.py:318-321`, which
  both *do* have such guards — `rebalance.py` was simply missed.
- Blast radius: it market-sells **every position not in `src/config.py`'s 18-symbol
  `SWING_UNIVERSE`** ([rebalance.py:96-99](../../rebalance.py#L96-L99)). The real swing
  universe is `src/swing/universe.py`'s ~228 symbols. A swing position in, for example,
  `PANW` or `VRTX` is out-of-universe by this script's definition and would be
  liquidated at market — cancelling its protective bracket, invalidating the trade
  journal, and tripping `reconciliation_halt` on the next reconcile.
- It also still accepted `--mode day` until this prompt (choice removed; `resolve_mode`
  would have raised anyway).

#### B2 — `execute_trades.py --flatten` · guarded for entries, open for exits

- Order calls: [`execute_trades.py:124`](../../execute_trades.py#L124) (flatten), [`:249`](../../execute_trades.py#L249) (`place_order`).
- The entry guard works: `if not args.dry_run and not args.flatten: return 2`
  ([`:321-326`](../../execute_trades.py#L321)) and `buy`/`short` are explicitly blocked
  ([`:397-406`](../../execute_trades.py#L397)).
- **But `--flatten` is an explicit exemption from that guard.** `execute_trades.py --flatten`
  with no other flag places live market sell orders for every open position, with no
  kill-switch, halt, or reconciliation check. `flatten_all()` also emits `side="buy"` for
  negative quantities ([`:110`](../../execute_trades.py#L110)) — i.e. it would cover a short,
  a position type that must never exist.
- Reachable indirectly: `run_hedge_fund.py --execute` shells out to this script
  ([`run_hedge_fund.py:83-111`](../../run_hedge_fund.py#L83-L111)).

#### B3 — `src/alpaca_integration.py` module-level order helpers · unguarded functions

- `execute_decisions()` is correctly guarded — it raises on `dry_run=False`
  ([`:318-321`](../../src/alpaca_integration.py#L318-L321)) and blocks `buy`/`short`.
- **However the guard is on the orchestrator, not the primitives.** Nine module-level
  functions still reach Alpaca directly with no check: `_place_alpaca_order` (`:143`),
  `_place_bracket_order` (`:195`), `flatten_positions` (`:265`), `cancel_order` (`:495`),
  `cancel_all_orders` (`:512`), `_place_limit_order` (`:542`), `_place_stop_order` (`:577`),
  `_place_trailing_stop` (`:614`), `_place_oco_order` (`:652`).
- `cancel_all_orders()` is the sharpest edge: one call cancels every open order on the
  account, **including the protective stop legs of every live swing bracket**, leaving
  positions naked with no notification and no reconciliation trigger until 3:55pm.

#### Not bypasses (verified)

- `portfolio_monitor.py` — `run_monitor()` returns a static dict; no network, no orders ([`:52-63`](../../portfolio_monitor.py#L52)).
- `src/swing/brokers/human_supervised.py` — `place_order`/`cancel_order`/`replace_stop`/`close_position` are all bound to `_blocked` ([`:129-132`](../../src/swing/brokers/human_supervised.py#L129)).
- `src/swing/brokers/robinhood_mcp.py` — every method raises.
- `src/swing/stops.py` — reaches the broker, but only after `SwingRiskManager.validate_stop_change` ([`:26-29`](../../src/swing/stops.py#L26)), and the Alpaca provider independently refuses widening ([alpaca.py:113-124](../../src/swing/brokers/alpaca.py#L113-L124)). Correct by design.
- `app/backend/` — no Alpaca calls; it drives the LangGraph research app only.
- `src/backtesting/` — simulation only.

#### The structural point

`src/swing/execution.py` is the only supported path, but it is not the only *possible*
path. Nothing at the credential layer enforces the boundary: `ALPACA_API_KEY` is read
directly from the environment by four different modules
(`src/swing/brokers/alpaca.py:19`, `src/swing/data_feed.py:74`, `src/accounts.py:43`,
`rebalance.py:27`). Until legacy order code is deleted outright — not merely
guarded — the swing risk engine is advisory with respect to the account, not
authoritative over it.

### 3.7 Anything that could still execute a day trade, short, leveraged ETF, margin, options, or crypto order

| Instrument / behaviour | Reachable? | Detail |
|---|---|---|
| **Day trade** | **No** (as an intended path) | `resolve_mode()` raises on non-swing ([src/config.py:65-66](../../src/config.py#L65)); `MODES` contains only `"swing"`. However **B1/B2/B3 all place `time_in_force: "day"` market orders**, and a same-day flatten of a same-day entry *is* a day trade under PDT rules. Not an intraday strategy, but capable of producing the pattern. |
| **Short** | **Not via swing.** `Decision` has no SHORT ([models.py:32-37](../../src/swing/models.py#L32)); `risk.py:52-53` rejects any non-BUY; `reconciliation.py:88-89` treats a negative broker quantity as a discrepancy and halts. **Legacy code retains short machinery**: `execute_trades.py:183` `if action == "short": side = "sell"` and `:174` computes a short stop above entry. These sit behind the `buy`/`short` block at `:397`. `flatten_all` at `:110` and `flatten_positions` at [alpaca_integration.py:246](../../src/alpaca_integration.py#L246) both compute a buy-to-cover for negative quantities — dead in practice, but present. |
| **Leveraged / inverse ETF** | **No** | Blocked in four independent places: the 30-symbol `LEVERAGED_OR_INVERSE_ETFS` set ([risk.py:13-17](../../src/swing/risk.py#L13-L17)), the scanner's pre-filter ([market.py:185](../../src/swing/market.py#L185)), the broker's own `attributes` classification ([alpaca.py:57-60](../../src/swing/brokers/alpaca.py#L57), enforced [risk.py:63-64](../../src/swing/risk.py#L63)), and the curated universe which contains none. `risk_manager.py:43` still carries a legacy "allowed in day mode" comment — dead. |
| **Margin** | **No for live; unverified for paper** | `risk.py:86-87` rejects `is_margin_enabled` — but **only when `execution_mode == "live"`**. In paper mode a margin-enabled (2x multiplier) Alpaca account is accepted, and `buying_power` from such an account exceeds cash. `risk.py:207` guards this with `min(account.cash, account.buying_power)`, which is the correct mitigation. Worth an explicit paper-mode check. |
| **Options** | **No** | `asset_class not in {"us_equity", "equity"}` → reject ([risk.py:59-60](../../src/swing/risk.py#L59)). No options code anywhere. |
| **Crypto** | **No** | Same `asset_class` gate; the curated universe contains no crypto; no crypto endpoint is called. |

### 3.8 Legacy tests asserting forbidden behaviour

| Test file | Asserts | Disposition |
|---|---|---|
| `tests/backtesting/integration/test_integration_short_only.py` | A short-only strategy opens, holds, and closes short positions | Tests `src/backtesting/`, not the swing path. Forbidden behaviour, but deleting it without deleting the engine and its `backtester` console script would leave untested legacy code. **Left in place; flagged for the prompt that deletes `src/backtesting/`.** Per §5 I did not delete a meaningful test to make anything green. |
| `tests/backtesting/integration/test_integration_long_short.py` | Mixed long/short portfolio transitions | Same. |
| `tests/backtesting/test_portfolio.py`, `test_execution.py` | Short-position accounting in the legacy engine | Same. |
| `tests/swing/test_risk.py:115` `test_short_decision_is_not_in_schema` | That SHORT is **absent** | Correct and desirable — keep. |

No test under `tests/swing/` asserts forbidden behaviour.

---

## 4. Risk constant reconciliation

Every risk-relevant constant in the repository. "Enforced" = a code path rejects on it.
"Advisory" = read but never blocks. "Dead" = defined and never read.

### 4.1 Swing production constants — `src/swing/config.py`

| Constant | Value | Line | Env override | Status | Conflict with `REFACTOR_RULES` §1 |
|---|---|---|---|---|---|
| `normal_risk_pct` | 0.005 (0.50%) | `:50` | `NORMAL_RISK_PCT` | **Enforced** — `risk.py:195` | **CONFLICT** — §1 resolves risk per trade to **0.75%** |
| `a_plus_risk_pct` | 0.0075 (0.75%) | `:51` | `A_PLUS_RISK_PCT` | **Enforced** — `risk.py:202` | Matches §1's number but only as an A+ exception, not the default |
| `absolute_max_risk_pct` | 0.01 (1.00%) | `:52` | `ABSOLUTE_MAX_RISK_PCT` | **Enforced** — `risk.py:203`, `:215` | — |
| `reduced_risk_pct` | 0.003 (0.30%) | `:53` | `REDUCED_RISK_PCT` | **Enforced** — `risk.py:195` | — |
| `reduce_risk_drawdown_pct` | 0.05 (5%) | `:54` | `REDUCE_RISK_DRAWDOWN_PCT` | **Enforced** — `risk.py:195`, `:202` | — |
| `halt_drawdown_pct` | 0.08 (8%) | `:55` | `HALT_DRAWDOWN_PCT` | **Enforced** — `risk.py:188-190` | Matches §1 (8–10%) |
| `max_combined_open_risk_pct` | 0.02 (2%) | `:56` | `MAX_COMBINED_OPEN_RISK_PCT` | **Enforced twice** — `risk.py:217`, `database.py:599` | Matches §1 |
| `max_position_exposure_pct` | 0.35 (35%) | `:57` | `MAX_POSITION_EXPOSURE_PCT` | **Enforced** — `risk.py:208-209` (reduce-only) | Matches §1 (30–35%, ceiling only) |
| `max_open_positions` | 3 | `:58` | *(no env hook)* | **Enforced twice** — `risk.py:180`, `database.py:595` | Matches §1 (secondary ceiling) |
| `max_new_positions_day` | **1** | `:59` | *(no env hook)* | **Enforced twice** — `risk.py:182`, `database.py:585` | **CONFLICT** — §1 resolves this to **2** |
| `max_new_positions_week` | 3 | `:60` | *(no env hook)* | **Enforced twice** — `risk.py:184`, `database.py:588` | — |
| `consecutive_loss_halt` | 3 | `:61` | *(no env hook)* | **Enforced** — `risk.py:191-193`, `postmortem.py:155` | — |
| `minimum_rr` | 2.0 | `:63` | `MINIMUM_RR` | **Enforced** — `risk.py:151` | Matches §1 |
| `exceptional_minimum_rr` | 1.5 | `:64` | `EXCEPTIONAL_MINIMUM_RR` | **Enforced but bypassable** — `risk.py:152` gated only on an unverified boolean (U6) | §1 says min R:R is 2.0; this is an undocumented escape hatch |
| `buy_score_threshold` | 80.0 | `:65` | `BUY_SCORE_THRESHOLD` | **Enforced** — `risk.py:138`, `agents.py:139` | — |
| `preferred_score_threshold` | 85.0 | `:66` | `PREFERRED_SCORE_THRESHOLD` | **DEAD** — validated at `:107`, read nowhere | — |
| `a_plus_score_threshold` | 90.0 | `:67` | `A_PLUS_SCORE_THRESHOLD` | **Enforced** — `risk.py:198` | — |
| `minimum_price` | 5.0 | `:68` | `MINIMUM_PRICE` | **Enforced twice** — `market.py:199`, `risk.py:101` | — |
| `minimum_average_volume` | 1,000,000 | `:69` | `MINIMUM_AVERAGE_VOLUME` | **Enforced twice** — `market.py:199`, `risk.py:103` | — |
| `maximum_spread_pct` | 0.005 (0.50%) | `:70` | `MAXIMUM_SPREAD_PCT` | **Enforced** — `risk.py:107`, `market.py:204` | — |
| `earnings_exclusion_trading_days` | 5 | `:71` | `EARNINGS_EXCLUSION_TRADING_DAYS` | **Enforced** — `risk.py:131` | — |
| `quote_freshness_seconds` | 300 | `:72` | `QUOTE_FRESHNESS_SECONDS` | **Enforced ×3** — `risk.py:112`, `:117`, `:126` | — |
| `cooldown_trading_days` | 10 | `:73` | `COOLDOWN_TRADING_DAYS` | **Enforced** — `risk.py:173` | — |
| `minimum_statistical_sample` | 30 | `:74` | `MINIMUM_STATISTICAL_SAMPLE` | **Enforced** — `analytics.py:63`, `lessons_review.py:79` | — |
| `maximum_scanner_candidates` | 20 | `:75` | `MAXIMUM_SCANNER_CANDIDATES` | **Enforced** — `market.py:210`, `agents.py:141` | — |
| `maximum_pm_candidates` | 5 | `:76` | `MAXIMUM_PM_CANDIDATES` | **Enforced** — `agents.py:171` | — |
| Entry-chase limit | **0.25R** | `risk.py:156` | none | **Enforced** — hardcoded literal | Should be config; not in §1 |
| Candidate staleness | **4 calendar days** | `risk.py:122` | none | **Enforced** — hardcoded literal | Should be config |
| Future-timestamp tolerance | **5.0 s** | `risk.py:109` | none | **Enforced** — hardcoded literal | — |
| Minimum bar history | **200** | `market.py:194`, `:113` | none | **Enforced** — hardcoded literal | The de-facto IPO filter (#7) |
| Liquidity score ceiling | **5,000,000 ADV** | `market.py:279` | none | Advisory (scoring only) | — |
| Regime volatility trigger | **0.35 annualized** | `market.py:130` | none | **Enforced** (regime → veto at `risk.py:142`) | Hardcoded |
| Regime crash trigger | **−7% 5-day ROC** | `market.py:130` | none | **Enforced** (same) | Hardcoded |
| `LEVERAGED_OR_INVERSE_ETFS` | 30 symbols | `risk.py:13-17` | none | **Enforced** — `risk.py:97`, `market.py:185` | — |
| Score weights (9) | 15/15/20/10/10/10/10/5/5 | `market.py:151-161` | none | **Enforced** (determines the score gated at 80) | Not configurable (#5) |
| Blacklist | empty list | `config/do_not_trade.yaml` | `DO_NOT_TRADE_PATH` | **Enforced** — `risk.py:95` | **Ships empty — must be populated before use** |

### 4.2 Immutable floors — `SwingSettings.__post_init__`

These make the above non-weakenable. Any violation raises at startup
([config.py:79-127](../../src/swing/config.py#L79-L127)).

| Guard | Line | Effect |
|---|---|---|
| `normal_risk_pct <= 0.005` | `:84` | **Caps normal risk at 0.50% — makes §1's 0.75% default unreachable without editing this line** |
| `a_plus_risk_pct <= 0.0075` | `:86` | Caps A+ at 0.75% |
| `absolute_max_risk_pct <= 0.01` | `:88` | Hard 1% ceiling |
| `0.0025 <= reduced_risk_pct <= 0.0035` | `:90` | Band on drawdown risk |
| `reduce_risk_drawdown_pct <= 0.05`, `halt_drawdown_pct <= 0.08` | `:92` | Drawdown halts may only tighten |
| `max_combined_open_risk_pct <= 0.02` | `:96` | 2% aggregate ceiling |
| `max_position_exposure_pct <= 0.35` | `:98` | 35% ceiling |
| `1 <= max_open_positions <= 3` | `:100` | — |
| **`1 <= max_new_positions_day <= 1`** | `:102` | **Hard-codes one entry per day — directly blocks §1's resolved value of 2** |
| `1 <= max_new_positions_week <= 3` | `:102` | — |
| `consecutive_loss_halt <= 3` | `:104` | — |
| `buy_score_threshold >= 80`, ordered ≤ preferred ≤ a_plus ≤ 100 | `:106-108` | — |
| `minimum_rr >= 2.0`, `exceptional_minimum_rr >= 1.5` | `:110` | — |
| `minimum_price >= 5`, `minimum_average_volume >= 1_000_000` | `:114` | — |
| `maximum_spread_pct <= 0.005` | `:116` | — |
| `1 <= quote_freshness_seconds <= 300` | `:118` | — |
| `earnings_exclusion_trading_days >= 5`, `cooldown_trading_days >= 10` | `:120` | — |
| `minimum_statistical_sample >= 30` | `:122` | — |
| Live mode requires `LIVE_TRADING_ACK` | `:126` | — |

### 4.3 Legacy constants — `src/config.py`

| Constant | Value | Line | Status |
|---|---|---|---|
| `max_position_pct` / `MAX_POSITION_PCT` | 0.35 | `:41`, `:98` | Advisory — read by `execute_trades.py:361` in an already-blocked branch |
| `max_sector_pct` / `MAX_SECTOR_PCT` | 0.35 | `:42`, `:99` | **Advisory / effectively dead** — no sector cap is enforced anywhere |
| `stop_loss_pct` / `STOP_LOSS_PCT` | **0.0** | `:43`, `:100` | Deliberately zeroed; `portfolio_monitor.check_hard_stop` treats ≤0 as disabled |
| `trailing_stop_pct` / `TRAILING_STOP_PCT` | **0.0** | `:43`, `:101` | Same |
| `daily_loss_limit` / `DAILY_LOSS_CIRCUIT_BREAKER` | 0.08 | `:44`, `:102` | Advisory — logs a warning at `execute_trades.py:364`, blocks nothing |
| `weekly_loss_limit` / `WEEKLY_LOSS_CIRCUIT_BREAKER` | 0.08 | `:45`, `:103` | **Dead** — no reader |
| `no_buy_if_down_pct` / `NO_BUY_IF_DOWN_PCT` | 0.05 | `:46`, `:108` | Dead |
| `max_trades_per_day` / `MAX_TRADES_PER_DAY` | 1 | `:47`, `:104` | Advisory — slices `trades[:max_trades]` at `execute_trades.py:379` |
| `max_open_positions` / `MAX_OPEN_POSITIONS` | 3 | `:48`, `:105` | Dead in legacy |
| `min_cash_pct` / `MIN_CASH_PCT` | 0.0 | `:49`, `:106` | Dead — **and this is the repo's only cash-reserve constant, set to zero** |
| `max_tactical_pct` / `MAX_MOONSHOT_PCT` | 0.0 | `:50`, `:107` | Dead |
| `flatten_eod` | `False` | `:51` | Dead |
| `allow_leveraged_etfs` | `False` | `:52` | Dead |
| `normal_risk_per_trade` | 0.005 | `:53` | Dead mirror of swing |
| `a_plus_risk_per_trade` | 0.0075 | `:54` | Dead mirror |
| `absolute_max_risk_per_trade` | 0.01 | `:55` | Dead mirror |
| `max_combined_open_risk` | 0.02 | `:56` | Dead mirror |
| `max_new_positions_week` | 3 | `:57` | Dead mirror |
| `MAX_RISK_PER_TRADE` | 0.005 | `:109` | Read by `risk_manager.py` |
| `MAX_PORTFOLIO_HEAT` | 0.02 | `:110` | Read by `risk_manager.py` |
| `DEFAULT_STOP_PCT` | 0.0 | `:112` | Dead |
| `DEFAULT_TARGET_MULTIPLIER` | 2.0 | `:113` | **Live** — used by `execute_trades.py:177`, `:186` |
| `FLATTEN_BY` | `"disabled"` | `:114` | Dead |

### 4.4 `.env.example` values

`NORMAL_RISK_PCT=0.005`, `A_PLUS_RISK_PCT=0.0075`, `ABSOLUTE_MAX_RISK_PCT=0.01`,
`REDUCED_RISK_PCT=0.003`, `MAX_COMBINED_OPEN_RISK_PCT=0.02`, `BUY_SCORE_THRESHOLD=80`,
`QUOTE_FRESHNESS_SECONDS=300` — all identical to the code defaults. No conflict.

### 4.5 Missing constants required by `REFACTOR_RULES` §1

| Required | Present? |
|---|---|
| Risk per trade **0.75%** default | **No** — 0.50%, and `__post_init__:84` forbids raising it |
| Max new positions per day **2** | **No** — 1, and `__post_init__:102` forbids raising it |
| Weekly drawdown halt **4–5%** | **No** — does not exist in any form |
| Max total concurrent open risk 2.00% | Yes |
| Whole shares only | Yes |
| Risk-based size authoritative, max-position reduce-only | Yes |
| Max individual position 30–35% | Yes (35%) |
| Minimum initial R:R 2.0 | Yes (with the U6 escape hatch) |
| Portfolio drawdown halt 8–10% | Yes (8%) |
| Averaging down prohibited | Yes — `risk.py:160-161` |

### 4.6 Additional defects found while reconciling constants

- **D1** — `preferred_score_threshold` is fully plumbed (config, env, validation) and read by nothing. Dead config invites false confidence.
- **D2** — `update_trade()` accepts arbitrary columns ([database.py:655-662](../../src/swing/database.py#L655-L662)), so `initial_stop` and `planned_dollar_risk` — the denominators of every R calculation — are mutable after the fact. Directly undermines #17 and #18.
- **D3** — `reserve_entry_admission` counts today's trades with `entry_datetime >= day_start AND status NOT IN ('REJECTED','CANCELED')` ([database.py:575-578](../../src/swing/database.py#L575)). A trade whose entry order was later cancelled but whose status has not yet been reconciled still consumes the day's single slot. Conservative (fails closed), so not urgent — but it means a broker rejection can silently cost a trading day.
- **D4** — `equity_high` is `MAX(equity)` over all `equity_snapshots` ([database.py:743-746](../../src/swing/database.py#L743)) with no time bound and no reset on deposit. A cash deposit permanently raises the high-water mark and manufactures an artificial drawdown.
- **D5** — `consecutive_losses()` orders by `exit_datetime DESC LIMIT 100` ([database.py:708-719](../../src/swing/database.py#L708)); rows with a `NULL` `exit_datetime` sort unpredictably in SQLite. Low likelihood, wrong-answer-capable.

---

## 5. Assumptions in `REFACTOR_RULES` §1 that the code contradicts

Three, and two of them are hard blocks that prompt 03 must resolve with a human decision.

1. **Risk per trade 0.75% vs. code's 0.50%.** Not merely a different default —
   `SwingSettings.__post_init__:84` raises `ValueError` if `normal_risk_pct > 0.005`.
   Setting §1's value requires editing an immutable floor, which `REFACTOR_RULES` §2.5
   forbids ("do not raise any configured ceiling… if a safety limit genuinely blocks
   correct behavior, stop and report it instead of changing it"). **I am reporting it
   rather than changing it.** §1 and §2.5 are in direct tension here; a human must
   decide which governs. Note that 0.75% is already reachable for A+ candidates, so the
   *ceiling* is not the issue — the *default* is.

2. **Max new positions per day 2 vs. code's 1.** Same shape, worse: `__post_init__:102`
   is `if not (1 <= self.max_new_positions_day <= 1)`, a hard-coded equality. Two
   enforcement sites (`risk.py:182`, `database.py:585`) and `docs/RISK_MODEL.md` all
   assume 1. Raising it to 2 also interacts with `max_new_positions_week = 3` — two
   entries on Monday and Tuesday would exhaust the week by Tuesday.

3. **"`portfolio_monitor.py` is a fail-closed shim" — TRUE, but understated.** §0 is
   correct that it cannot read an account or place orders. What §0 does **not** say,
   and what matters far more, is that `rebalance.py`, `execute_trades.py --flatten`,
   and nine functions in `src/alpaca_integration.py` *can*. §0's list of "reported
   complete" items reads as though the broker surface is closed; it is not.

Additionally, §0's claim "**nothing calls a live order endpoint**" is **true only for
the live/Robinhood path**. Three legacy modules call Alpaca's real (paper) order
endpoint. Paper is not live, but it is a real broker connection acting on the same
account the swing engine reconciles against.

---

## 6. Migration plan for prompts 02–06

### 6.1 Prerequisites and correct ordering

```
P0  Close the bypass paths            ← must come first; nothing else is trustworthy until it does
     │
     ├─► P1  Structural stops (#9)    ← prerequisite for P2, P3, P4
     │        │
     │        ├─► P2  Risk constants + weekly halt + cluster limits (#12, §5 conflicts)
     │        │
     │        └─► P3  Lifecycle state machine + time stop (#15, #16)
     │
     └─► P4  Migrations + immutability + versioning (#24, #17, #25)
              │
              └─► P5  Observability: logging, notifications (#26, #28)
```

**P0 must precede everything.** Deleting `rebalance.py`, `execute_trades.py`,
and the order primitives in `src/alpaca_integration.py` is mechanical, has no
dependencies, and until it is done every downstream guarantee is conditional on
"nobody ran the other script". It is also the only item that reduces risk without
adding code.

**#9 (structural stops) is the true keystone.** It is a prerequisite for more than it
looks:
- **Position sizing (#10) is already correct but currently meaningless.** Sizing is
  `allowed_risk / (quote − stop)`. If the stop is an unvalidated LLM number, the
  denominator is arbitrary and the "risk-based sizing" guarantee is nominal. Fixing
  the constants (P2) before fixing the stop tightens a bound on a quantity that is
  still wrong.
- **Expectancy analytics (#19) cannot be interpreted** until stop methodology is
  consistent. Note that `analytics.py:48` already lists `stop_methodology` as a
  `not_collected` breakdown — the design anticipated this.
- **Time stops (#16) and R-multiples (#18)** are both denominated in initial R.

### 6.2 Where the planned sequence is wrong

- **Do not treat the risk constants (prompt 03) as independent of #9.** They are
  currently listed as separate work. Changing 0.50%→0.75% while the stop remains
  LLM-chosen increases position size by 50% on an unvalidated denominator. If the
  constants must move first for other reasons, at minimum land a *floor* on stop
  distance (e.g. `stop <= price − k×ATR`, fail-closed) in the same change.

- **#24 (migrations) is larger and more urgent than a "durability" framing suggests.**
  Every subsequent prompt that adds a column — cluster ID, time-stop deadline,
  lifecycle state, config hash — will hit the missing migration path. Building it
  *once*, early, is much cheaper than five ad-hoc `CREATE TABLE IF NOT EXISTS`
  workarounds. It is currently sequenced late; it should move earlier. It is also a
  **live data-loss risk today**: any existing database predating schema 3 is already
  mis-stamped.

- **#26 (structured logging) is sequenced as polish; it is not.** With no logging
  whatsoever, every subsequent prompt is debugged by reading SQLite. The cheap version
  (a `logging` call alongside each existing `record_violation`) is a few hours and pays
  for itself immediately.

- **Notifications (#28) can be deferred**, with one exception: a latched
  `drawdown_halt` / `loss_streak_halt` / `reconciliation_halt` currently notifies
  nobody, and the routines run unattended. A single push on latch is worth doing
  early, cheaply.

### 6.3 Gaps larger than assumed

- **#9 structural stops.** Framed as "not a universal fixed percentage". The real
  finding is that **there is no deterministic stop derivation at all** — the LLM
  supplies it. This is the single place where the "LLM is never the risk authority"
  claim is not true today: the model chooses the number that determines both risk per
  share and position size. This is the most important correctness gap in the repository.

- **#12 correlation/cluster.** Framed as adding a limit. There is **no sector→cluster
  mapping data source in the repository**, and `SECTOR_ETFS` covers 11 GICS sectors
  with `sector="Unknown"` for all 28 ETFs in the universe. Per `REFACTOR_RULES` §4,
  this must ship as a fail-closed gate with an explicit `TODO` and reason code, not a
  stub returning "pass". Expect this to be a data problem more than a code problem.

- **#24 migrations.** Framed as "safe schema migrations". There is no migration
  *framework* to make safe — it must be built, and it must handle a database that is
  already lying about its version.

- **#15 lifecycle.** Framed as adding a state machine. There is also **no exit logic at
  all** — exits are 100% delegated to broker bracket legs, detected after the fact by
  `reconcile`. A time stop (#16) or thesis-invalidation exit has nothing to hook into.

- **#6 universe.** Framed as "broad configurable universe". Making it configurable is
  easy; the harder issue is that the blacklist is applied only at risk time, so
  blacklisted symbols are scanned, scored, and **sent to a paid LLM** before being
  rejected.

### 6.4 Recommended prompt scoping

| Prompt | Scope | Rationale |
|---|---|---|
| **02** | Delete `rebalance.py`, `execute_trades.py`, `risk_manager.py`, the order primitives in `src/alpaca_integration.py`, `run_hedge_fund.py`, `run_analysis.py`, and the associated legacy tests. Add a repo-wide guard test asserting exactly one module may POST to a broker. | Highest risk reduction per line changed; no dependencies |
| **03** | Deterministic structural stops (#9) **plus** the §5 constant conflicts, together. Add `stop_methodology` to the trade record. | Must be one change — see 6.2 |
| **04** | Migrations framework (#24), trade-record immutability (#17/D2), decision versioning (#25) | All schema-adjacent; do them in one schema change |
| **05** | Lifecycle state machine (#15), time stop (#16), cluster/correlation limits (#12), weekly drawdown halt | Depends on 03 and 04 |
| **06** | Structured logging (#26), halt notifications (#28), the unsafe fallbacks U1–U12, universe configurability (#6) | Cleanup and observability |

---

## 7. Static cleanup performed in this prompt

Documentation, help text, and provably dead code only. **No strategy, risk, sizing,
execution, or schema logic was modified.** Every deletion is backed by the dependency
analysis in §3.1.

| Change | Justification |
|---|---|
| **Deleted `trading_mode.json`** | Zero code references repo-wide (grep across `*.py`, `*.md`, `*.toml`, `*.yml`, `*.json`). Only README prose and docstrings mentioned it. |
| **Removed `ALPACA_DAY_API_KEY`/`ALPACA_DAY_API_SECRET`/`ALPACA_DAY_ACCOUNT_ID` loading** from `src/accounts.py` | The `"day"` registry entry was unreachable: `get_account_for_mode` raises on any non-swing mode (`:77-78`). Removes the last place day credentials could enter the process. |
| **Removed `src/accounts.get_all_accounts()`** | Zero callers repo-wide; it was the only remaining reader of the day registry entry. |
| **Removed `"day"` from `--mode` choices** in `check_moves.py`, `check_portfolio.py`, `gather_data.py`, `intel_exchange.py`, `rebalance.py`, `risk_manager.py`, `trade_alerts.py` | CLI-flag removal for an unsupported feature. `resolve_mode()` already raised on `"day"`, so these choices advertised a path that could only error. |
| **Removed `--mode day` usage lines** from the docstrings of the same scripts plus `execute_trades.py`, `run_hedge_fund.py`, `scan_market.py` | Help text for an unsupported feature. |
| **Rewrote `execute_trades.py`'s module docstring** | It advertised `short` entries and buy examples that `main()` blocks. Now states plainly that this is a legacy bypass path, entries are refused, and it must never be used for entries. |
| **Rewrote `src/alpaca_integration.py`'s module docstring** | It claimed `risk_manager.py` was "the single source of truth" — false. Now names the nine unguarded order primitives explicitly and points at this report. |
| **Rewrote `README.md`** | Removed ~750 lines documenting dual-mode operation, `trading_mode.json` steering, day-trading crons, EOD flatten, short selling, separate day accounts, and the personality swarm as if operational. Replaced with the current framework only, an explicit **"Known gaps — not yet implemented"** list, and a short **Historical Note**. The gaps list names #9, #12, #15, #16, #24, #26, #28, the hardcoded universe, and the bypass paths as *not done* — prompts 02–06 are not described as complete. |
| **Archived `PLAYBOOK.md`, `DESIGN.md`, `MILESTONES.md`, `INTEL_EXCHANGE.md` to `docs/archive/`** | Each instructs a user to run day trading, mode switching, or the personality swarm. Each now opens with a bold **"ARCHIVED — NOT OPERATING INSTRUCTIONS"** banner. Not deleted: they are provenance for a system that traded real (paper) capital. |
| **Added `autoresearch/README.md`** | States that the directory is intraday-derived, not swing-validated, must never feed production configuration, and that `evolve.py` self-commits and must not be run. |
| **Added quarantine headers** to `autoresearch/analyze.py`, `backtest_fast.py`, `evolve.py`, `strategy.py`, `strategy_backup.py`, `BRIDGE-DESIGN.md`, `CRON-ARCHITECTURE.md`, `program.md` | Boundary marking per Task 5. Verified `evolve.py` parses `EXPERIMENT_NAME` by regex, not by line offset, so the prepended header breaks nothing. |
| **Added a quarantine note** to `src/agents/autoresearch_agent.py` | It is the sole bridge out of `autoresearch/`; the note records that it belongs to the legacy app and that no `src/swing` module reaches it. |
| **Updated `.env.example`** | Retitled from "AI Hedge Fund", added a header stating no day/mode/short/margin/options/crypto settings exist, removed the dangling day-credentials comment. **No values changed** (§5 reserves that for prompt 03). |

### AutoResearch isolation — explicit confirmation

**No production module imports from `autoresearch/`.** Verified by
`grep -rn "from autoresearch\|import autoresearch\|autoresearch\." --include=*.py .`
excluding `autoresearch_swing`: exactly two hits, both in
`src/agents/autoresearch_agent.py`, reached only via `src/utils/analysts.py:23` →
`src/main.py` → the legacy LangGraph app. No file under `src/swing/` appears in the
result set. `src/autoresearch_swing/` is a separate package importing only
`src.swing.config`, `src.swing.database`, and its own `fitness` module.

### Final bypass re-check

Re-ran the repo-wide order-call sweep after all edits
(`requests.post|requests.delete|requests.patch|requests.request` against `/orders` or
`/positions`). The set of files that can reach a broker is **unchanged** from the
pre-cleanup sweep: `src/swing/brokers/alpaca.py` (the sanctioned path),
`execute_trades.py`, `rebalance.py`, `src/alpaca_integration.py`. **I introduced no
new bypass path.** I also closed none — closing them is prompt 02's job and requires
a runnable test suite.

---

## 8. Definition-of-done status (`REFACTOR_RULES` §7)

| # | Requirement | Status |
|---|---|---|
| 1 | `poetry run pytest` passes, nothing skipped | **NOT VERIFIED — blocked.** No Python runtime is available in this environment: `poetry` is not on `PATH`, and `python`/`python3` resolve to the non-functional Windows Store alias stub. No interpreter and no virtualenv were found. I could not execute the suite. I therefore restricted all code changes to docstrings, help text, CLI choice lists, and code proven dead by grep, so the blast radius of an unrun test is as small as I could make it. **A human must run `poetry run pytest` before merging.** |
| 2 | Lint / format / type check passes | **NOT VERIFIED — same blocker.** `pyrightconfig.json` scopes checking to `src/swing`, `src/autoresearch_swing`, `tests/swing`; I changed no file in any of those three directories, so the type-check surface is untouched. |
| 3 | Branch committed, working tree clean | See the commit list below |
| 4 | Report at `docs/refactor/01_AUDIT_REPORT.md` | This file |
| 5 | Final repo-wide grep confirming no new bypass path | **Done** — see §7 "Final bypass re-check" |

I cannot report success on items 1 and 2. Per §7, I am saying so plainly rather than
claiming a green suite I did not observe.

---

## 9. Assumptions a human should verify

1. **The test suite still passes.** I could not run it (§8). The changes most likely to
   matter: removing `get_all_accounts()` from `src/accounts.py` (grep found zero
   callers, but a dynamic `getattr` would not show up), and narrowing seven argparse
   `choices` lists to `["swing"]` (a test passing `--mode day` would now fail at
   argparse rather than at `resolve_mode`).
2. **`docs/REMEDIATION_STATUS.md`'s "55 passed / 99 passed / Pyright 0 errors"** is
   inherited from a previous session and unverified here.
3. **`autoresearch/evolve.py` tolerates a prepended comment block in `strategy.py`.** I
   confirmed it rewrites `EXPERIMENT_NAME` by regex rather than reading line 0, but the
   script is quarantined and I did not run it.
4. **Archiving `PLAYBOOK.md`/`DESIGN.md`/`MILESTONES.md`/`INTEL_EXCHANGE.md` to
   `docs/archive/`** breaks any external link to those root paths. I judged the risk of
   an agent following day-trading instructions to outweigh link stability.
5. **The `docs/archive/*` banner's relative links** (`../../README.md`,
   `../RISK_MODEL.md`) assume the files stay one level deep in `docs/archive/`.
6. **`.gitignore` lists `data/` and `snapshots/`**, yet `data/performance.json` and
   `snapshots/2026-03-17.json` are tracked. Pre-existing; not touched.

## 10. Fail-closed TODOs left in place

I introduced no new fail-closed stubs — this prompt added no logic. The following
**pre-existing** permissive fallbacks are left exactly as found and are itemized in
§3.5 for the prompt that owns them:

- **U1/U2/U3** — missing sector data defaults to `sector_rs = 0.0`, which *satisfies*
  the `RELATIVE_STRENGTH_CONTINUATION` gate. Highest-severity fallback. → prompt 03/06
- **U4** — `is_tradable=True, is_halted=False, is_leveraged_or_inverse=False` assumed
  for the whole universe at `data_feed.py:312-320`. → prompt 06
- **U6** — `exceptional_rr_evidence` is an unverified boolean that relaxes the 2.0R
  floor to 1.5R. → prompt 03
- **U7/U8** — postmortem defaults to `followed_strategy=True` and swallows the LLM
  failure silently. → prompt 06
- **U10** — an order stuck in an active status reconciles clean forever with no age
  bound. → prompt 05
- **U12** — the paper-mode account check is tautological because
  `AlpacaPaperProvider` hardcodes `is_paper=True`. → prompt 02 or 06
- **D2** — `update_trade()` has no column allowlist, so `initial_stop` is mutable. →
  prompt 04
- **D4** — `equity_high` never resets on deposit, manufacturing artificial drawdown. →
  prompt 04

## 11. Could not verify without credentials or a live broker

- Whether Alpaca GTC bracket child legs are actually created, survive overnight, and
  resize correctly on a partial take-profit fill.
- Whether `AlpacaPaperProvider.replace_stop`'s `PATCH` returns a `stop_price` field on
  a bracket child leg — [`alpaca.py:117-119`](../../src/swing/brokers/alpaca.py#L117) refuses
  the replacement if it does not, which is correct but untested against the real API.
- Whether Alpaca's `assets` endpoint reliably populates the `attributes` array that
  `is_leveraged_or_inverse` depends on ([`alpaca.py:57-60`](../../src/swing/brokers/alpaca.py#L57)).
  If it is empty, that check silently returns `None` and the only remaining defence is
  the 30-symbol static list.
- Whether yfinance earnings lookups succeed from the cloud routine environment at all
  (`docs/CRON_AGENTS.md` reports Yahoo blocks that IP range for bulk price history).
  If they always fail, every individual stock is rejected as `earnings_unknown` and the
  system trades only ETFs — fail-closed, but a silent strategy change.
- Real Turso behaviour under `BEGIN IMMEDIATE`. The serialized-admission guarantee at
  [`database.py:568`](../../src/swing/database.py#L568) assumes SQLite write-lock semantics;
  whether `turso_serverless` provides the same isolation over HTTP is **not verified**
  and is load-bearing for the concurrency guarantee.
- Behaviour against a real ~$2,000 account, including whether `position_too_small`
  fires often enough to make the experiment untradeable.

---

*This report describes an audit. Nothing in this repository has been validated against
a live broker, and no part of it should be described as production-ready.*
