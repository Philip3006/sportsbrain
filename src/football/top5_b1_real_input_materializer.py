"""Offline, non-authorizing materialization of real Top-5 B1 inputs.

The operator entry point consumes a previously validated provider-neutral B4
dossier.  It has no provider client, production lifecycle store, publisher,
activation executor, scheduler, betting, or ledger dependency.
"""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any

from monitoring.top5 import governed_runtime_evidence
from monitoring.top5.governed_runtime_state import (
    STATE_RELATIVE_PATH,
    write_governed_runtime_state,
)
from src.football.production_contracts import (
    Fixture,
    MarketSnapshot,
    MarketSnapshotKind,
    PredictionInput,
)
from src.football.top5_b4_provider_neutral_evidence import (
    TOP5_LEAGUE_ORDER,
    ProviderNeutralB4EvidenceError,
    Top5B4ProviderNeutralEvidenceDossierV1,
)
from src.football.top5_final_acceptance import (
    ACTIVE_PROVIDER,
    STATUS_VERIFIED,
    verify_final_acceptance,
)
from src.football.top5_final_acceptance_composer import (
    compose_top5_final_acceptance_bundle,
)
from src.football.top5_public_acceptance import (
    TOP5_PUBLICATION_PRECHECK_READY,
    Top5PrepublicationArtifactV1,
    publication_precheck,
)
from src.football.top5_research_binding import (
    FROZEN_RESEARCH_SHA,
    M5_CANDIDATE_ID,
    Top5M5FeatureAdapter,
    Top5M5MarketModel,
    inventory_for,
)
from src.football.top5_signal_lifecycle import (
    DEFAULT_SIGNAL_LIFECYCLE_CONTRACT,
    EXPECTED_PROVIDER_IDENTITY,
    LifecyclePlanStatus,
    SignalLifecycleStage,
    Top5SignalLifecycle,
    create_initial_signal,
    plan_signal_lifecycle,
)
from src.football.top5_signal_lifecycle_public_adapter import (
    project_top5_signal_lifecycles,
)
from src.notifications.public_serializer import (
    map_prediction_to_public_football_signals,
)
from src.runtime.paths import ROOT, governed_runtime_root

_GIT_SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")
_MAX_DOSSIER_BYTES = 16 * 1024 * 1024
_MAX_HEALTH_AGE_SECONDS = 900
_OUTPUT_NAMES = (
    "lifecycle_model_evidence.json",
    "governed_runtime_evidence.json",
    "prepublication_artifact.json",
    "b1_bundle.json",
    "b1_verified.json",
    "publication_precheck.json",
    "summary.json",
)
_EXPECTED_B1_HANDOFF_KEYS = frozenset(
    {
        "source_main_sha",
        "b4_quota_proof_package",
        "b4_quota_headroom",
        "discovery_evidence",
        "provider_native_discovery_provenance",
        "controlled_shadow",
        "b4_reconciliation",
        "b4_qualification",
        "b4_native_authorization",
        "b4_dossier_digest",
    }
)


class Top5B1MaterializationError(ValueError):
    """A real B4 input cannot safely produce the read-only B1 materials."""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _sha_text(value: object, field: str) -> str:
    if not isinstance(value, str) or _GIT_SHA_RE.fullmatch(value) is None:
        raise Top5B1MaterializationError(f"{field} must be a 40-character Git SHA")
    return value.lower()


def _mapping(value: object, field: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise Top5B1MaterializationError(f"{field} must be an object")
    return value


def _instant(value: object, field: str) -> datetime:
    if not isinstance(value, str):
        raise Top5B1MaterializationError(f"{field} must be an ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise Top5B1MaterializationError(f"{field} must be an ISO timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise Top5B1MaterializationError(f"{field} must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def _inside(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _current_git_sha(repo_root: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError, TimeoutError) as exc:
        raise Top5B1MaterializationError(
            "current source-main SHA is unavailable"
        ) from exc
    if result.returncode != 0:
        raise Top5B1MaterializationError("current source-main SHA is unavailable")
    return _sha_text(result.stdout.strip(), "current source-main SHA")


def _read_json(path: Path, field: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise Top5B1MaterializationError(f"{field} is missing or malformed") from exc
    if not isinstance(value, dict):
        raise Top5B1MaterializationError(f"{field} must be an object")
    return value


def _source_runtime_facts(repo_root: Path, *, now: datetime) -> dict[str, object]:
    meta_path = repo_root / "docs/data/provenance_meta.json"
    health_path = repo_root / "docs/data/health.json"
    meta = _read_json(meta_path, "provenance_meta.json")
    health = _read_json(health_path, "health.json")
    meta_ci = _mapping(meta.get("source_ci"), "provenance_meta.source_ci")
    source_release_sha = _sha_text(
        meta.get("source_release_sha"), "provenance_meta.source_release_sha"
    )
    if (
        meta_ci.get("status") != "success"
        or _sha_text(meta_ci.get("head_sha"), "provenance_meta.source_ci.head_sha")
        != source_release_sha
    ):
        raise Top5B1MaterializationError(
            "source release CI is not successful for its SHA"
        )

    provenance = _mapping(health.get("provenance"), "health.provenance")
    health_ci = _mapping(provenance.get("source_ci"), "health.provenance.source_ci")
    if (
        _sha_text(provenance.get("source_release_sha"), "health source_release_sha")
        != source_release_sha
        or health_ci.get("status") != "success"
        or _sha_text(health_ci.get("head_sha"), "health source_ci.head_sha")
        != source_release_sha
    ):
        raise Top5B1MaterializationError(
            "health provenance does not agree with the successful source release"
        )
    runtime_data_sha = _sha_text(
        provenance.get("runtime_data_sha"), "health runtime_data_sha"
    )
    if provenance.get("source_runtime_consistent") is not True:
        raise Top5B1MaterializationError("source/runtime provenance is inconsistent")
    generated_at = _instant(health.get("generated_at"), "health.generated_at")
    if (
        generated_at > now
        or (now - generated_at).total_seconds() > _MAX_HEALTH_AGE_SECONDS
    ):
        raise Top5B1MaterializationError(
            "health provenance is stale or from the future"
        )
    health_status = health.get("overall")
    if health_status not in {"ok", "degraded"}:
        raise Top5B1MaterializationError("health status is not governed-readable")
    return {
        "source_release_sha": source_release_sha,
        "runtime_data_sha": runtime_data_sha,
        "source_runtime_consistent": True,
        "source_ci_status": "success",
        "health_status": health_status,
        "health_generated_at": generated_at.isoformat(),
        "health_source": "docs/data/health.json",
        "source_meta_path": str(meta_path),
        "health_path": str(health_path),
    }


def _publisher_workspace(
    raw_path: str | Path,
    *,
    repo_root: Path,
    is_git_clean: Callable[[Path], tuple[bool, str]],
) -> Path:
    supplied = Path(raw_path).expanduser()
    if not supplied.is_absolute():
        raise Top5B1MaterializationError("publisher workspace path must be absolute")
    if supplied.is_symlink():
        raise Top5B1MaterializationError("publisher workspace must not be a symlink")
    try:
        path = supplied.resolve(strict=True)
    except OSError as exc:
        raise Top5B1MaterializationError("publisher workspace is missing") from exc
    if not path.is_dir() or _inside(path, repo_root):
        raise Top5B1MaterializationError(
            "publisher workspace must be an external git checkout"
        )
    clean, reason = is_git_clean(path)
    if not clean:
        raise Top5B1MaterializationError(
            f"publisher workspace is dirty or unreadable: {reason}"
        )
    return path


def _output_directory(
    raw_path: str | Path,
    *,
    repo_root: Path,
    publisher_root: Path,
) -> tuple[Path, Path]:
    supplied = Path(raw_path).expanduser()
    if not supplied.is_absolute() or supplied.name in {"", ".", ".."}:
        raise Top5B1MaterializationError(
            "output directory must be an absolute new path"
        )
    if supplied.exists() or supplied.is_symlink():
        raise Top5B1MaterializationError("output directory already exists")
    try:
        parent = supplied.parent.resolve(strict=True)
    except OSError as exc:
        raise Top5B1MaterializationError("output parent directory is missing") from exc
    target = parent / supplied.name
    if (
        _inside(target, repo_root)
        or _inside(target, publisher_root)
        or _inside(publisher_root, target)
    ):
        raise Top5B1MaterializationError(
            "private output must be separate from the repository and publisher workspace"
        )
    return target, parent


def _validate_disabled_route(route: object) -> None:
    current = _mapping(route, "current durable route")
    if (
        current.get("provider_authority") != ACTIVE_PROVIDER
        or current.get("activation_mode") != "DISABLED"
        or any(
            current.get(field) is not False
            for field in (
                "publication",
                "betting",
                "ledger_mutation",
                "recurring_scheduler",
            )
        )
    ):
        raise Top5B1MaterializationError(
            "durable route is missing, active, or has an unsafe side-effect flag"
        )


def _league_inputs(
    dossier: Top5B4ProviderNeutralEvidenceDossierV1,
    *,
    now: datetime,
) -> dict[str, tuple[object, object, Fixture]]:
    shadow = dossier.controlled_shadow
    if shadow.provider_identity != "isports_api":
        raise Top5B1MaterializationError(
            "B1 real-input materializer requires isports_api evidence"
        )
    if tuple(item.league for item in shadow.discovery_evidence) != TOP5_LEAGUE_ORDER:
        raise Top5B1MaterializationError("B4 dossier league order is not canonical")
    if any(
        capture.evidence_kind != "REAL_OBSERVED"
        or capture.network_execution is not True
        for capture in shadow.captures
    ):
        raise Top5B1MaterializationError("B4 dossier is not REAL_OBSERVED")

    result: dict[str, tuple[object, object, Fixture]] = {}
    markets = {item.league: item for item in shadow.market_evidence}
    captures = {item.league: item for item in shadow.captures}
    for discovery in shadow.discovery_evidence:
        market = markets.get(discovery.league)
        capture = captures.get(discovery.league)
        if market is None or capture is None:
            raise Top5B1MaterializationError(
                "B4 discovery/market/capture join is incomplete"
            )
        if (
            market.provider_identity != "isports_api"
            or market.provider_fixture_id != discovery.provider_fixture_id
            or capture.fixture_key != discovery.fixture_key
            or capture.market_evidence_digest != market.evidence_digest
            or capture.discovery_evidence_digest != discovery.evidence_digest
            or market.market_type != "PRE_MATCH_1X2_REGULATION"
        ):
            raise Top5B1MaterializationError(
                f"B4 market does not bind the discovered fixture for {discovery.league}"
            )
        fixture = Fixture(
            fixture_key=discovery.fixture_key,
            league_code=discovery.league,
            home_team=discovery.home_team,
            away_team=discovery.away_team,
            kickoff=discovery.kickoff,
        )
        fixture.validate()
        plan = plan_signal_lifecycle(
            fixture,
            now,
            contract=DEFAULT_SIGNAL_LIFECYCLE_CONTRACT,
        )
        if plan.status is not LifecyclePlanStatus.INITIAL_DUE:
            raise Top5B1MaterializationError(
                f"B1_LIFECYCLE_WINDOW_BLOCKED: {discovery.league} is {plan.status.value}"
            )
        result[discovery.league] = (discovery, market, fixture)
    if tuple(result) != TOP5_LEAGUE_ORDER:
        raise Top5B1MaterializationError(
            "B4 input does not cover the canonical five leagues"
        )
    return result


def _runtime_state(
    facts: Mapping[str, object],
    *,
    publisher_root: Path,
    runtime_root: Path,
) -> dict[str, object]:
    state_path = runtime_root / STATE_RELATIVE_PATH
    return {
        "source_release_sha": facts["source_release_sha"],
        "source_release_resolution": {
            "source": str(state_path),
            "method": "docs/data/provenance_meta.json source_ci success",
            "resolved_sha": facts["source_release_sha"],
        },
        "runtime_data_sha": facts["runtime_data_sha"],
        "runtime_data_resolution": {
            "source": str(state_path),
            "method": "docs/data/health.json provenance.runtime_data_sha",
            "resolved_sha": facts["runtime_data_sha"],
        },
        "active_provider_order": [ACTIVE_PROVIDER],
        "provider_authority": ACTIVE_PROVIDER,
        "publisher_workspace_root": str(publisher_root),
        "no_bet": True,
        "publication": False,
        "publication_enabled": False,
        "activation_state": "DISABLED",
        "scheduler_state": "disabled",
        "betting": False,
        "ledger_mutation": False,
        "health_authority": "governed",
        "health_status": facts["health_status"],
        "health_source": facts["health_source"],
    }


def _build_models_and_lifecycles(
    inputs: Mapping[str, tuple[object, object, Fixture]],
    *,
    dossier: Top5B4ProviderNeutralEvidenceDossierV1,
    source_release_sha: str,
    now: datetime,
) -> tuple[list[Top5SignalLifecycle], dict[str, dict[str, object]], dict[str, object]]:
    feature_adapter = Top5M5FeatureAdapter()
    model = Top5M5MarketModel()
    contract = DEFAULT_SIGNAL_LIFECYCLE_CONTRACT
    signal_time = contract.as_signal_time_contract(SignalLifecycleStage.INITIAL)
    shadow = dossier.controlled_shadow
    capture_by_league = {item.league: item for item in shadow.captures}
    anchors: list[Top5SignalLifecycle] = []
    lifecycle_payload: dict[str, dict[str, object]] = {}
    prediction_payload: dict[str, object] = {}

    for league in TOP5_LEAGUE_ORDER:
        discovery, market, fixture = inputs[league]
        inventory = inventory_for(league, M5_CANDIDATE_ID)
        model_hash = inventory.model_artifact_hash
        if not model_hash:
            raise Top5B1MaterializationError(
                f"M5 inventory hash is missing for {league}"
            )
        source = market.source_provenance
        snapshot_id = f"isports_api:{market.normalized_observation_digest}"
        snapshot = MarketSnapshot(
            fixture_key=fixture.fixture_key,
            captured_at=market.captured_at,
            kind=MarketSnapshotKind.SIGNAL_TIME,
            source=source,
            odds={
                "home": float(market.home_odds),
                "draw": float(market.draw_odds),
                "away": float(market.away_odds),
            },
            snapshot_id=snapshot_id,
        )
        snapshot.validate()
        features = feature_adapter.build(fixture, snapshot)
        model_input = PredictionInput.create(
            fixture, snapshot, features, signal_time, now
        )
        probabilities = dict(model.predict(model_input))
        inverse = {
            outcome: 1.0 / float(market_odds)
            for outcome, market_odds in snapshot.odds.items()
        }
        inverse_total = sum(inverse.values())
        implied = {outcome: value / inverse_total for outcome, value in inverse.items()}
        edges = {
            outcome: (probabilities[outcome] - implied[outcome]) * 100
            for outcome in probabilities
        }
        capture = capture_by_league[league]
        provenance = {
            "evidence_provider": "isports_api",
            "source_identity": market.source_identity,
            "source_provenance": market.source_provenance,
            "provider_competition_id": discovery.provider_competition_id,
            "provider_fixture_id": discovery.provider_fixture_id,
            "source_main_sha": dossier.source_main_sha,
            "controlled_shadow_run_id": shadow.controlled_shadow_run_id,
            "qualification_session_id": shadow.qualification_session_id,
            "authorization_id": shadow.authorization_id,
            "authorization_digest": shadow.authorization_digest,
            "discovery_evidence_digest": discovery.evidence_digest,
            "market_evidence_digest": market.evidence_digest,
            "capture_evidence_digest": capture.evidence_digest,
            "normalized_observation_digest": market.normalized_observation_digest,
        }
        outcomes: dict[str, Top5SignalLifecycle] = {}
        for outcome in ("home", "draw", "away"):
            decision_seed = (
                shadow.controlled_shadow_run_id,
                shadow.qualification_session_id,
                league,
                fixture.fixture_key,
                market.evidence_digest,
                outcome,
            )
            decision_id = (
                "b1-materializer:"
                + sha256("|".join(decision_seed).encode("utf-8")).hexdigest()
            )
            outcomes[outcome] = create_initial_signal(
                fixture=fixture,
                snapshot=snapshot,
                now=now,
                market_id="h2h",
                outcome_id=outcome,
                candidate_id=M5_CANDIDATE_ID,
                model_identity=M5_CANDIDATE_ID,
                provider_identity=EXPECTED_PROVIDER_IDENTITY,
                probabilities=probabilities,
                source_sha=source_release_sha,
                research_sha=FROZEN_RESEARCH_SHA,
                model_artifact_hash=model_hash,
                eligibility_decision=True,
                decision_id=decision_id,
                decision_reason=(
                    "REAL_OBSERVED iSports B4 market evidence passed canonical M5 "
                    "SIGNAL_TIME and INITIAL lifecycle validation"
                ),
                contract=contract,
                implied_probabilities=implied,
                edges=edges,
                confidence_metadata=provenance,
            )
        anchors.append(outcomes["home"])
        lifecycle_payload[league] = {
            "provider_authority": ACTIVE_PROVIDER,
            "evidence_provider": "isports_api",
            "fixture": {
                "fixture_key": fixture.fixture_key,
                "league_code": league,
                "home_team": fixture.home_team,
                "away_team": fixture.away_team,
                "kickoff": fixture.kickoff.isoformat(),
            },
            "market_snapshot": {
                "snapshot_id": snapshot.snapshot_id,
                "snapshot_kind": snapshot.kind.value,
                "snapshot_source": snapshot.source,
                "captured_at": snapshot.captured_at.isoformat(),
                "odds": dict(snapshot.odds),
                "market_evidence_digest": market.evidence_digest,
                "source_identity": market.source_identity,
                "source_provenance": market.source_provenance,
            },
            "model": {
                "candidate_id": M5_CANDIDATE_ID,
                "model_identity": M5_CANDIDATE_ID,
                "research_sha": FROZEN_RESEARCH_SHA,
                "source_release_sha": source_release_sha,
                "model_artifact_hash": model_hash,
                "features": dict(features),
                "prediction_probabilities": probabilities,
                "implied_probabilities": implied,
                "edges_pp": edges,
                "closing_used_for_prediction": False,
            },
            "initial_lifecycles": [
                outcomes[key].as_payload() for key in ("home", "draw", "away")
            ],
        }
        prediction_payload[league] = {
            "fixture": fixture,
            "snapshot": snapshot,
            "probabilities": probabilities,
            "features": dict(features),
            "lifecycles": outcomes,
        }
    if len(anchors) != 5:
        raise Top5B1MaterializationError("exactly five lifecycle anchors are required")
    return anchors, lifecycle_payload, prediction_payload


def _build_prepublication(
    prediction_payload: Mapping[str, Mapping[str, object]],
    *,
    dossier: Top5B4ProviderNeutralEvidenceDossierV1,
    source_release_sha: str,
    runtime_data_sha: str,
    now: datetime,
) -> Top5PrepublicationArtifactV1:
    shadow = dossier.controlled_shadow
    records: list[dict[str, object]] = []
    model_hashes = {
        inventory_for(league, M5_CANDIDATE_ID).model_artifact_hash
        for league in TOP5_LEAGUE_ORDER
    }
    if len(model_hashes) != 1 or None in model_hashes:
        raise Top5B1MaterializationError("canonical M5 inventory hashes do not agree")
    model_hash = next(iter(model_hashes))
    assert isinstance(model_hash, str)
    for league in TOP5_LEAGUE_ORDER:
        item = prediction_payload[league]
        fixture = item["fixture"]
        snapshot = item["snapshot"]
        probabilities = item["probabilities"]
        lifecycles = item["lifecycles"]
        assert isinstance(fixture, Fixture)
        assert isinstance(snapshot, MarketSnapshot)
        assert isinstance(probabilities, Mapping)
        assert isinstance(lifecycles, Mapping)
        projection = project_top5_signal_lifecycles(
            [lifecycles[outcome] for outcome in ("home", "draw", "away")],
            prediction_probabilities=probabilities,
            fixture_identity=fixture.fixture_key,
            league_identity=league,
            candidate_identity=M5_CANDIDATE_ID,
            model_identity=M5_CANDIDATE_ID,
            provider_authority=ACTIVE_PROVIDER,
            prediction_timestamp=now,
            signal_timestamp=snapshot.captured_at,
            snapshot_id=snapshot.snapshot_id,
            provenance={
                "source_sha": source_release_sha,
                "research_sha": FROZEN_RESEARCH_SHA,
                "model_artifact_hash": model_hash,
            },
            market_identity="h2h",
        )
        envelope: dict[str, object] = {
            "record_type": "prediction_artifact",
            "prediction_artifact": {
                "league_code": league,
                "fixture_key": fixture.fixture_key,
                "prediction_id": "top5-b1:"
                + sha256(
                    f"{shadow.controlled_shadow_run_id}|{league}|{fixture.fixture_key}|{snapshot.snapshot_id}".encode()
                ).hexdigest(),
                "model_identity": M5_CANDIDATE_ID,
                "prediction_timestamp": now.isoformat(),
                "signal_timestamp": snapshot.captured_at.isoformat(),
                "snapshot_id": snapshot.snapshot_id,
                "snapshot_kind": "SIGNAL_TIME",
                "probabilities": dict(probabilities),
                "odds": dict(snapshot.odds),
                "lifecycle_by_market": projection,
                "candidate_id": M5_CANDIDATE_ID,
                "research_sha": FROZEN_RESEARCH_SHA,
                "source_sha": source_release_sha,
                "runtime_data_sha": runtime_data_sha,
                "source_release_sha": source_release_sha,
                "source_runtime_consistent": True,
                "model_artifact_hash": model_hash,
                "signal_time_contract_id": DEFAULT_SIGNAL_LIFECYCLE_CONTRACT.stage_contract_id(
                    SignalLifecycleStage.INITIAL
                ),
                "publication_authorized": False,
                "closing_used_for_prediction": False,
                "evidence_kind": "REAL_OBSERVED",
                "synthetic": False,
                "no_bet": True,
                "publication_enabled": False,
                "publication_status": "PREPARED",
                "controlled_shadow_run_id": shadow.controlled_shadow_run_id,
                "qualification_session_id": shadow.qualification_session_id,
                "run_id": shadow.controlled_shadow_run_id,
                "session_id": shadow.qualification_session_id,
                "provider": ACTIVE_PROVIDER,
                "source": ACTIVE_PROVIDER,
                "provider_authority": ACTIVE_PROVIDER,
                "evidence_provider": "isports_api",
                "activation_state": "shadow",
            },
            "fixture": {
                "fixture_key": fixture.fixture_key,
                "home_team": fixture.home_team,
                "away_team": fixture.away_team,
                "kickoff": fixture.kickoff.isoformat(),
            },
            "provenance": {
                "provider": ACTIVE_PROVIDER,
                "source": ACTIVE_PROVIDER,
                "source_release_sha": source_release_sha,
                "runtime_data_sha": runtime_data_sha,
                "source_runtime_consistent": True,
                "source_sha": source_release_sha,
                "research_sha": FROZEN_RESEARCH_SHA,
                "model_artifact_hash": model_hash,
                "snapshot_id": snapshot.snapshot_id,
                "snapshot_kind": "SIGNAL_TIME",
                "captured_at": snapshot.captured_at.isoformat(),
                "controlled_shadow_run_id": shadow.controlled_shadow_run_id,
                "qualification_session_id": shadow.qualification_session_id,
                "candidate_id": M5_CANDIDATE_ID,
                "signal_time_contract_id": DEFAULT_SIGNAL_LIFECYCLE_CONTRACT.stage_contract_id(
                    SignalLifecycleStage.INITIAL
                ),
                "publication_authorized": False,
                "closing_used_for_prediction": False,
                "evidence_provider": "isports_api",
            },
            "activation_state": "shadow",
            "publication_status": "PREPARED",
            "publication_enabled": False,
            "no_bet": True,
        }
        league_records = map_prediction_to_public_football_signals(envelope)
        if len(league_records) != 3:
            raise Top5B1MaterializationError(
                f"public serializer did not produce 3 outcomes for {league}"
            )
        for record in league_records:
            # The serializer's generic public projection emits an empty
            # activation_id field.  Even an empty capability-adjacent field is
            # forbidden in a prepublication artifact, so omit it explicitly.
            record.pop("activation_id", None)
            record.update(
                {
                    "activation_state": "SHADOW",
                    "activation_mode": "shadow",
                    "signal_status": "SHADOW",
                    "publication_status": "PREPARED",
                    "publication_enabled": False,
                    "publication_authorized": False,
                    "provider": ACTIVE_PROVIDER,
                    "source": ACTIVE_PROVIDER,
                    "run_id": shadow.controlled_shadow_run_id,
                    "session_id": shadow.qualification_session_id,
                    "controlled_shadow_run_id": shadow.controlled_shadow_run_id,
                    "qualification_session_id": shadow.qualification_session_id,
                    "candidate_id": M5_CANDIDATE_ID,
                    "model_identity": M5_CANDIDATE_ID,
                    "research_sha": FROZEN_RESEARCH_SHA,
                    "source_sha": source_release_sha,
                    "runtime_data_sha": runtime_data_sha,
                    "source_release_sha": source_release_sha,
                    "source_runtime_consistent": True,
                    "model_artifact_hash": model_hash,
                    "signal_time_contract_id": DEFAULT_SIGNAL_LIFECYCLE_CONTRACT.stage_contract_id(
                        SignalLifecycleStage.INITIAL
                    ),
                    "evidence_kind": "REAL_OBSERVED",
                    "synthetic": False,
                    "no_bet": True,
                    "closing_used_for_prediction": False,
                }
            )
            provenance = dict(record.get("provenance", {}))
            provenance.pop("activation_id", None)
            provenance.update(
                {
                    "provider": ACTIVE_PROVIDER,
                    "source": ACTIVE_PROVIDER,
                    "source_release_sha": source_release_sha,
                    "runtime_data_sha": runtime_data_sha,
                    "source_runtime_consistent": True,
                    "source_sha": source_release_sha,
                    "research_sha": FROZEN_RESEARCH_SHA,
                    "model_artifact_hash": model_hash,
                    "controlled_shadow_run_id": shadow.controlled_shadow_run_id,
                    "qualification_session_id": shadow.qualification_session_id,
                    "candidate_id": M5_CANDIDATE_ID,
                    "signal_time_contract_id": DEFAULT_SIGNAL_LIFECYCLE_CONTRACT.stage_contract_id(
                        SignalLifecycleStage.INITIAL
                    ),
                    "publication_authorized": False,
                    "closing_used_for_prediction": False,
                }
            )
            record["provenance"] = provenance
            records.append(record)

    release = {
        "schema_version": "top5-prepublication-release-v1",
        "release_type": "PREPUBLICATION",
        "publication_status": "PREPARED",
        "publication_enabled": False,
        "publication_authorized": False,
        "provider_authority": ACTIVE_PROVIDER,
        "source_release_sha": source_release_sha,
        "runtime_data_sha": runtime_data_sha,
        "source_runtime_consistent": True,
        "candidate_id": M5_CANDIDATE_ID,
        "model_identity": M5_CANDIDATE_ID,
        "research_sha": FROZEN_RESEARCH_SHA,
        "model_artifact_hash": model_hash,
        "signal_time_contract_id": DEFAULT_SIGNAL_LIFECYCLE_CONTRACT.stage_contract_id(
            SignalLifecycleStage.INITIAL
        ),
        "closing_used_for_prediction": False,
        "controlled_shadow_run_id": shadow.controlled_shadow_run_id,
        "qualification_session_id": shadow.qualification_session_id,
        "league_codes": sorted(TOP5_LEAGUE_ORDER),
        "generated_at": now.isoformat(),
        "no_bet": True,
    }
    public_input = {"football": records, "top5_release": release}
    artifact = Top5PrepublicationArtifactV1.create(
        worker_candidate_payload=public_input,
        static_candidate_payload=public_input,
        prepared_at=now,
    )
    artifact.validate(
        now=now,
        expected_run_id=shadow.controlled_shadow_run_id,
        expected_session_id=shadow.qualification_session_id,
        expected_source_release_sha=source_release_sha,
        expected_runtime_data_sha=runtime_data_sha,
        expected_model_artifact_hash=model_hash,
        expected_signal_time_contract_id=DEFAULT_SIGNAL_LIFECYCLE_CONTRACT.stage_contract_id(
            SignalLifecycleStage.INITIAL
        ),
    )
    if len(artifact.worker_candidate_payload["football"]) != 15:
        raise Top5B1MaterializationError(
            "prepublication product must contain 15 outcomes"
        )
    return artifact


def _write_private_artifacts(output_dir: Path, artifacts: Mapping[str, object]) -> None:
    if set(artifacts) != set(_OUTPUT_NAMES):
        raise Top5B1MaterializationError("private output artifact set is incomplete")
    created: list[Path] = []
    output_created = False
    try:
        os.mkdir(output_dir, mode=0o700)
        output_created = True
        os.chmod(output_dir, 0o700)
        for name in _OUTPUT_NAMES:
            path = output_dir / name
            payload = (
                json.dumps(
                    artifacts[name],
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                    allow_nan=False,
                ).encode("utf-8")
                + b"\n"
            )
            fd = os.open(
                path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            created.append(path)
            try:
                with os.fdopen(fd, "wb", closefd=True) as stream:
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
                    os.fchmod(stream.fileno(), 0o400)
            except BaseException:
                try:
                    os.close(fd)
                except OSError:
                    pass
                raise
    except BaseException:
        for path in created:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        if output_created:
            try:
                output_dir.rmdir()
            except OSError:
                pass
        raise


def _materialize_validated_dossier(
    dossier: Top5B4ProviderNeutralEvidenceDossierV1,
    *,
    publisher_workspace: str | Path,
    output_directory: str | Path,
    now: datetime,
    repo_root: Path = ROOT,
    runtime_root: Path | None = None,
    current_source_main_sha: str | None = None,
    writer: Callable[[Mapping[str, object]], Path] | None = None,
    observer: Callable[[], dict[str, object]] | None = None,
    route_reader: Callable[[Path], dict[str, object]] | None = None,
    is_git_clean: Callable[[Path], tuple[bool, str]] | None = None,
    clock: Callable[[], datetime] | None = None,
) -> dict[str, object]:
    """Testable core. The public CLI supplies real time and canonical paths."""
    checked_now = now.astimezone(timezone.utc)
    root = repo_root.resolve()
    state_writer = writer or write_governed_runtime_state
    runtime_observer = observer or governed_runtime_evidence.observe_governed_runtime
    durable_route_reader = route_reader or governed_runtime_evidence._read_route_state
    git_clean_check = is_git_clean or governed_runtime_evidence._git_clean
    try:
        dossier.validate(now=checked_now)
    except (ProviderNeutralB4EvidenceError, TypeError, ValueError) as exc:
        raise Top5B1MaterializationError(
            f"validated B4 dossier rejected: {exc}"
        ) from exc

    main_sha = current_source_main_sha or _current_git_sha(root)
    main_sha = _sha_text(main_sha, "current source-main SHA")
    if dossier.source_main_sha.lower() != main_sha:
        raise Top5B1MaterializationError(
            "B4 source_main_sha is not the current main SHA"
        )

    facts = _source_runtime_facts(root, now=checked_now)
    publisher_root = _publisher_workspace(
        publisher_workspace,
        repo_root=root,
        is_git_clean=git_clean_check,
    )
    output_dir, _output_parent = _output_directory(
        output_directory,
        repo_root=root,
        publisher_root=publisher_root,
    )
    repo_clean, repo_reason = git_clean_check(root)
    if not repo_clean:
        raise Top5B1MaterializationError(
            f"source checkout is dirty or unreadable: {repo_reason}"
        )

    governed_root = (runtime_root or governed_runtime_root()).resolve()
    try:
        route = durable_route_reader(governed_root)
    except (OSError, RuntimeError, ValueError) as exc:
        raise Top5B1MaterializationError(
            "existing durable disabled route state is required"
        ) from exc
    _validate_disabled_route(route)

    inputs = _league_inputs(dossier, now=checked_now)
    handoff = dossier.b1_evidence_inputs(now=checked_now)
    if set(handoff) != _EXPECTED_B1_HANDOFF_KEYS:
        raise Top5B1MaterializationError(
            "B4-to-B1 handoff does not match the canonical ten-key contract"
        )

    # This is the only authorized external write in the materialization path:
    # a governed observation record. Route and lifecycle stores are untouched.
    state = _runtime_state(
        facts, publisher_root=publisher_root, runtime_root=governed_root
    )
    state_writer(state)
    runtime_evidence = runtime_observer()
    if not isinstance(runtime_evidence, dict):
        raise Top5B1MaterializationError(
            "governed runtime observation is not a valid object"
        )
    observed_at = _instant(
        runtime_evidence.get("runtime_state_observed_at"),
        "governed runtime_state_observed_at",
    )
    captured_at = _instant(
        runtime_evidence.get("captured_at"), "governed runtime captured_at"
    )
    materialization_now = (clock or _utc_now)().astimezone(timezone.utc)
    if (
        materialization_now < checked_now
        or observed_at > materialization_now
        or captured_at > materialization_now
        or captured_at < observed_at
        or (materialization_now - observed_at).total_seconds() > _MAX_HEALTH_AGE_SECONDS
        or (materialization_now - captured_at).total_seconds() > _MAX_HEALTH_AGE_SECONDS
    ):
        raise Top5B1MaterializationError(
            "governed runtime observation timestamps are stale or inconsistent"
        )
    if (
        runtime_evidence.get("status") != "READY"
        or runtime_evidence.get("schema_version")
        != governed_runtime_evidence.ARTIFACT_SCHEMA
        or runtime_evidence.get("runtime_root_role") != "governed-runtime"
        or not governed_runtime_evidence.verify_artifact_digest(runtime_evidence)
        or runtime_evidence.get("provider_authority") != ACTIVE_PROVIDER
        or runtime_evidence.get("active_provider_order") != [ACTIVE_PROVIDER]
        or runtime_evidence.get("source_release_sha") != facts["source_release_sha"]
        or runtime_evidence.get("runtime_data_sha") != facts["runtime_data_sha"]
        or runtime_evidence.get("no_bet") is not True
        or runtime_evidence.get("publication_enabled") is not False
        or runtime_evidence.get("activation_state") != "DISABLED"
    ):
        raise Top5B1MaterializationError(
            "governed runtime observation is not READY and safe"
        )

    anchors, lifecycle_evidence, model_payload = _build_models_and_lifecycles(
        inputs,
        dossier=dossier,
        source_release_sha=str(facts["source_release_sha"]),
        now=materialization_now,
    )
    prepublication = _build_prepublication(
        model_payload,
        dossier=dossier,
        source_release_sha=str(facts["source_release_sha"]),
        runtime_data_sha=str(facts["runtime_data_sha"]),
        now=materialization_now,
    )
    bundle = compose_top5_final_acceptance_bundle(
        b4_dossier=dossier,
        lifecycles=anchors,
        prepublication_artifact=prepublication,
        now=materialization_now,
        expected_source_main_sha=main_sha,
    )
    verification = verify_final_acceptance(
        bundle,
        now=materialization_now,
        expected_source_main_sha=main_sha,
    )
    if verification.get("status") != STATUS_VERIFIED:
        raise Top5B1MaterializationError("B1 final acceptance did not verify")
    dry_run = prepublication.delivery_dry_run_manifest(rollback_ready=True)
    public_precheck = publication_precheck(
        prepublication.as_payload(),
        verification,
        now=materialization_now,
        delivery_manifest=dry_run,
    )
    if (
        public_precheck.get("status") != TOP5_PUBLICATION_PRECHECK_READY
        or public_precheck.get("publication_enabled") is not False
        or public_precheck.get("publication_authorized") is not False
        or public_precheck.get("production_mutation") is not False
        or public_precheck.get("provider_requests") != 0
    ):
        raise Top5B1MaterializationError(
            "read-only B3 publication precheck did not become READY"
        )

    manifest = verification["manifest"]
    summary: dict[str, object] = {
        "schema_version": "top5-b1-real-input-materialization-summary-v1",
        "source_main_sha": main_sha,
        "source_release_sha": facts["source_release_sha"],
        "runtime_data_sha": facts["runtime_data_sha"],
        "controlled_shadow_run_id": dossier.controlled_shadow.controlled_shadow_run_id,
        "qualification_session_id": dossier.controlled_shadow.qualification_session_id,
        "authorization_id": dossier.controlled_shadow.authorization_id,
        "provider_authority": ACTIVE_PROVIDER,
        "evidence_provider": dossier.controlled_shadow.provider_identity,
        "candidate_id": M5_CANDIDATE_ID,
        "research_sha": FROZEN_RESEARCH_SHA,
        "lifecycle_stage": "INITIAL",
        "lifecycle_anchor_count": len(anchors),
        "public_outcome_record_count": len(
            prepublication.worker_candidate_payload["football"]
        ),
        "fixture_keys": {
            league: lifecycle_evidence[league]["fixture"]["fixture_key"]
            for league in TOP5_LEAGUE_ORDER
        },
        "b4_dossier_digest": dossier.dossier_digest,
        "b1_manifest_digest": manifest["manifest_digest"],
        "prepublication_id": prepublication.prepublication_id,
        "prepublication_digest": prepublication.public_product_digest,
        "b1_status": verification["status"],
        "public_precheck_status": public_precheck["status"],
        "provider_requests": 0,
        "publication_enabled": False,
        "publication_authorized": False,
        "activation_performed": False,
        "betting": False,
        "ledger_mutation": False,
        "scheduler_registered": False,
    }
    artifacts: dict[str, object] = {
        "lifecycle_model_evidence.json": {
            "schema_version": "top5-b1-lifecycle-model-evidence-v1",
            "provider_authority": ACTIVE_PROVIDER,
            "evidence_provider": dossier.controlled_shadow.provider_identity,
            "source_main_sha": main_sha,
            "source_release_sha": facts["source_release_sha"],
            "runtime_data_sha": facts["runtime_data_sha"],
            "controlled_shadow_run_id": dossier.controlled_shadow.controlled_shadow_run_id,
            "qualification_session_id": dossier.controlled_shadow.qualification_session_id,
            "anchors": [anchor.as_payload() for anchor in anchors],
            "leagues": lifecycle_evidence,
            "provider_requests": 0,
            "production_mutation": False,
        },
        "governed_runtime_evidence.json": bundle["runtime_evidence"],
        "prepublication_artifact.json": prepublication.as_payload(),
        "b1_bundle.json": bundle,
        "b1_verified.json": verification,
        "publication_precheck.json": public_precheck,
        "summary.json": summary,
    }
    _write_private_artifacts(output_dir, artifacts)
    return {**summary, "output_directory": str(output_dir)}


def materialize_top5_b1_real_inputs(
    *,
    dossier_path: str | Path,
    publisher_workspace: str | Path,
    output_directory: str | Path,
) -> dict[str, object]:
    """Load a private B4 dossier and produce read-only B1/B3 input artifacts."""
    checked_now = _utc_now()
    supplied = Path(dossier_path).expanduser()
    if not supplied.is_absolute():
        raise Top5B1MaterializationError("B4 dossier path must be absolute")
    if supplied.is_symlink():
        raise Top5B1MaterializationError("B4 dossier file must not be a symlink")
    try:
        dossier_path_resolved = supplied.resolve(strict=True)
        metadata = dossier_path_resolved.stat()
    except OSError as exc:
        raise Top5B1MaterializationError("B4 dossier file is missing") from exc
    repo_root = ROOT.resolve()
    if (
        dossier_path_resolved.is_symlink()
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or metadata.st_size > _MAX_DOSSIER_BYTES
        or _inside(dossier_path_resolved, repo_root)
    ):
        raise Top5B1MaterializationError(
            "B4 dossier must be a bounded, user-owned external regular file"
        )
    raw = _read_json(dossier_path_resolved, "B4 dossier")
    try:
        dossier = Top5B4ProviderNeutralEvidenceDossierV1.from_payload(
            raw, now=checked_now
        )
    except (ProviderNeutralB4EvidenceError, TypeError, ValueError) as exc:
        raise Top5B1MaterializationError(
            f"B4 dossier validation failed: {exc}"
        ) from exc
    return _materialize_validated_dossier(
        dossier,
        publisher_workspace=publisher_workspace,
        output_directory=output_directory,
        now=checked_now,
        repo_root=repo_root,
    )


__all__ = [
    "Top5B1MaterializationError",
    "materialize_top5_b1_real_inputs",
]
