"""Aggregate single-trade OBSERVATION lessons into research hypotheses.

Every closed trade already creates one low-confidence OBSERVATION lesson row
(src.swing.postmortem.PostmortemEngine.close_and_review). This module groups
those rows by setup type (and by setup+regime) and, once a group crosses the
statistical-significance floor, proposes a hypothesis via the existing
SwingResearchEngine -- evidence-only, never a rule change, never validated or
promoted here. record_comparison()/approve-strategy remain the only paths to
production, both already human-gated.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from src.autoresearch_swing.research import SwingResearchEngine

from .config import SwingSettings
from .database import SwingDatabase


@dataclass
class _Group:
    setup: str
    regime: str | None
    lesson_ids: list[str] = field(default_factory=list)
    trade_ids: set[str] = field(default_factory=set)


def _group_observations(database: SwingDatabase) -> list[_Group]:
    rows = database.rows("SELECT * FROM lessons WHERE status='OBSERVATION' AND applicable_setup IS NOT NULL")
    groups: dict[tuple[str, str | None], _Group] = {}
    for row in rows:
        setup = row["applicable_setup"]
        regime = row["applicable_regime"]
        trade_ids = json.loads(row["supporting_trades_json"])
        for key in {(setup, None), (setup, regime)}:
            group = groups.setdefault(key, _Group(setup=key[0], regime=key[1]))
            group.lesson_ids.append(row["lesson_id"])
            group.trade_ids.update(trade_ids)
    return list(groups.values())


def _trade_r_values(database: SwingDatabase, trade_ids: list[str]) -> list[float]:
    if not trade_ids:
        return []
    placeholders = ",".join("?" for _ in trade_ids)
    rows = database.rows(
        f"SELECT realized_r FROM trades WHERE trade_id IN ({placeholders}) AND realized_r IS NOT NULL",
        tuple(trade_ids),
    )
    return [float(row["realized_r"]) for row in rows]


def _existing_open_hypothesis(database: SwingDatabase, setup: str, regime: str | None) -> str | None:
    rows = database.rows(
        "SELECT hypothesis_id FROM hypotheses WHERE setup_type IS ? AND market_regime IS ? "
        "AND status IN ('PROPOSED','VALIDATING') ORDER BY created_at DESC LIMIT 1",
        (setup, regime),
    )
    return rows[0]["hypothesis_id"] if rows else None


def review_observations(
    database: SwingDatabase, settings: SwingSettings, reports_dir: Path,
) -> list[dict[str, Any]]:
    """Group observations and propose hypotheses for groups with enough evidence.

    Returns a list describing every group that met the sample threshold,
    whether a new hypothesis was proposed or one already existed.
    """
    engine = SwingResearchEngine(database, settings, reports_dir)
    results: list[dict[str, Any]] = []
    for group in _group_observations(database):
        r_values = _trade_r_values(database, sorted(group.trade_ids))
        if len(r_values) < settings.minimum_statistical_sample:
            continue
        wins = sum(1 for r in r_values if r > 0)
        win_rate = wins / len(r_values)
        expectancy_r = sum(r_values) / len(r_values)
        scope = f"{group.setup} in {group.regime}" if group.regime else group.setup
        description = (
            f"{scope}: {len(r_values)} trades, {win_rate:.0%} win rate, {expectancy_r:+.2f}R expectancy "
            "(aggregated from single-trade postmortem observations; not yet validated)."
        )
        existing = _existing_open_hypothesis(database, group.setup, group.regime)
        if existing:
            results.append({
                "setup_type": group.setup, "market_regime": group.regime, "sample_size": len(r_values),
                "win_rate": win_rate, "expectancy_r": expectancy_r, "hypothesis_id": existing, "newly_proposed": False,
            })
            continue
        try:
            hypothesis_id = engine.propose(
                observation=description,
                proposed_rule="No rule change proposed yet; evidence-only aggregation pending human review and a backtest.",
                supporting_lesson_ids=sorted(set(group.lesson_ids))[:50],
                supporting_sample_size=len(r_values),
                setup_type=group.setup,
                market_regime=group.regime,
                candidate_strategy_version=f"{settings.strategy_version}_CANDIDATE",
            )
        except ValueError:
            continue
        results.append({
            "setup_type": group.setup, "market_regime": group.regime, "sample_size": len(r_values),
            "win_rate": win_rate, "expectancy_r": expectancy_r, "hypothesis_id": hypothesis_id, "newly_proposed": True,
        })
    return results
