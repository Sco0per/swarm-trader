"""Reproducible decision envelopes and deterministic fingerprints."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from hashlib import sha256
from typing import Any

from .config import SwingSettings
from .security import redact_sensitive

SCANNER_VERSION = "deterministic-swing-scanner-v2"
PROMPT_VERSIONS = {
    "technical": "technical-v2",
    "fundamental": "fundamental-v2",
    "bear": "bear-v2",
    "portfolio_manager": "portfolio-manager-v2",
    "postmortem": "postmortem-v2",
}


def canonical_json(value: Any) -> str:
    return json.dumps(redact_sensitive(value), default=str, separators=(",", ":"), sort_keys=True)


def fingerprint(value: Any) -> str:
    return sha256(canonical_json(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class DecisionVersions:
    strategy_version: str
    config_version: str
    scanner_version: str
    model_name: str | None
    prompt_version: str | None

    @classmethod
    def from_settings(
        cls,
        settings: SwingSettings,
        *,
        model_name: str | None = None,
        prompt_version: str | None = None,
    ) -> "DecisionVersions":
        return cls(
            strategy_version=settings.strategy_version,
            config_version=settings.config_version,
            scanner_version=settings.scanner_version,
            model_name=model_name,
            prompt_version=prompt_version,
        )


def decision_envelope(
    *,
    versions: DecisionVersions,
    market_data_timestamp: str | None,
    feature_values: dict[str, Any],
    entry_score: float | None,
    risk_settings_snapshot: dict[str, Any],
    decision: dict[str, Any],
) -> dict[str, Any]:
    body = {
        "versions": asdict(versions),
        "market_data_timestamp": market_data_timestamp,
        "feature_values": feature_values,
        "entry_score": entry_score,
        "risk_settings_snapshot": risk_settings_snapshot,
        "decision": decision,
    }
    return {**body, "decision_fingerprint": fingerprint(body)}


def risk_settings_snapshot(settings: SwingSettings) -> dict[str, Any]:
    names = (
        "normal_risk_pct",
        "absolute_max_risk_pct",
        "max_combined_open_risk_pct",
        "max_sector_open_risk_pct",
        "max_cluster_open_risk_pct",
        "max_position_exposure_pct",
        "max_open_positions",
        "max_new_positions_day",
        "max_new_positions_week",
        "minimum_rr",
        "maximum_entry_slippage_r",
        "earnings_exclusion_trading_days",
    )
    return {name: getattr(settings, name) for name in names}


def persist_scan_decisions(
    database: Any,
    settings: SwingSettings,
    *,
    run_id: str,
    candidates: list[Any],
    funnel_report: Any,
) -> None:
    """Persist accepted and rejected scanner decisions as the experiment cohort."""

    versions = DecisionVersions.from_settings(settings)
    risk_snapshot = risk_settings_snapshot(settings)
    for candidate in candidates:
        features = {**candidate.validator_features, "score_components": candidate.score_components}
        decision = {"disposition": candidate.score_route, "validator_failures": candidate.validator_failures}
        envelope = decision_envelope(
            versions=versions,
            market_data_timestamp=candidate.data.market_timestamp.isoformat() if candidate.data.market_timestamp else None,
            feature_values=features,
            entry_score=candidate.score,
            risk_settings_snapshot=risk_snapshot,
            decision=decision,
        )
        database.record_candidate(
            candidate,
            strategy_version=settings.strategy_version,
            config_version=settings.config_version,
            scanner_version=settings.scanner_version,
        )
        database.record_decision(
            run_id=run_id,
            subject_type="CANDIDATE",
            subject_id=candidate.candidate_id,
            ticker=candidate.ticker,
            reason_code="CANDIDATE_ACCEPTED",
            **versions.__dict__,
            market_data_timestamp=envelope["market_data_timestamp"],
            feature_values=features,
            entry_score=candidate.score,
            risk_settings_snapshot=risk_snapshot,
            decision=decision,
            decision_fingerprint=envelope["decision_fingerprint"],
        )
        if candidate.score_route in {"strong", "very_strong"}:
            database.record_notification(
                event_type="NEW_HIGH_QUALITY_CANDIDATE",
                severity="INFO",
                title=f"New high-quality candidate: {candidate.ticker}",
                reason_code="CANDIDATE_ACCEPTED",
                payload={"ticker": candidate.ticker, "setup_type": candidate.setup_type.value, "score": candidate.score},
                dedupe_key=f"candidate:{candidate.ticker}:{candidate.setup_type.value}:{int(candidate.score // settings.llm_material_score_change)}",
            )
    for index, rejection in enumerate(funnel_report.rejections):
        features = {
            "stage": rejection.stage,
            "details": list(rejection.details),
            **dict(getattr(rejection, "feature_values", {}) or {}),
        }
        decision = {"disposition": "REJECTED", "reason_code": rejection.reason_code}
        envelope = decision_envelope(
            versions=versions,
            market_data_timestamp=None,
            feature_values=features,
            entry_score=None,
            risk_settings_snapshot=risk_snapshot,
            decision=decision,
        )
        database.record_decision(
            run_id=run_id,
            subject_type="REJECTION",
            subject_id=f"{run_id}:{index}",
            ticker=rejection.symbol,
            reason_code=rejection.reason_code,
            **versions.__dict__,
            market_data_timestamp=None,
            feature_values=features,
            entry_score=None,
            risk_settings_snapshot=risk_snapshot,
            decision=decision,
            decision_fingerprint=envelope["decision_fingerprint"],
        )
