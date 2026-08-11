# AutoResearch — QUARANTINED, INTRADAY-DERIVED, NOT SWING-VALIDATED

> **This directory is not part of the production system and must never feed
> production configuration.**

Everything in `autoresearch/` was built for the repository's earlier **intraday
day-trading** experiment. Its parameters, fitness function, backtester, and
recorded results describe 5-minute-bar intraday behaviour with an end-of-day
flatten. None of it has been validated against the current long-only swing
strategy, and its numbers (win rates, Sharpe, fitness scores) say nothing about
swing expectancy.

## Hard boundary

- **No module under `src/swing/` imports anything from this directory.** Verified
  by dependency analysis in `docs/refactor/01_AUDIT_REPORT.md`.
- The only importer is `src/agents/autoresearch_agent.py`, which belongs to the
  legacy LangGraph personality-swarm app (`src/main.py`) — itself non-production.
- Do not copy any constant, threshold, weight, or fitness term from here into
  `src/swing/config.py`, `src/swing/market.py`, or `src/swing/risk.py`.
- `autoresearch/evolve.py` autonomously edits `strategy.py` and can `git commit`.
  Do not run it.

## The swing equivalent

Swing hypothesis recording, validation planning, and the human-only promotion
gate live in `src/autoresearch_swing/` and are documented in
[docs/AUTORESEARCH_SWING.md](../docs/AUTORESEARCH_SWING.md). That package shares
no code with this one. Human review remains mandatory for every promotion into
the supported swing framework.

Rebuilding this directory is explicitly out of scope for the current refactor
series (`docs/REFACTOR_RULES.md` §1).
