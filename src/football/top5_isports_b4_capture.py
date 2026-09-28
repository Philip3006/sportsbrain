"""One-shot, candidate-only iSports Top-5 capture into the B4 neutral contract.

The default CLI mode is offline preflight.  Network execution is available only
through the explicit ``--execute-network`` flag and consumes a durable,
authorization-bound marker before the credential is read.  This module never
changes provider authority, publication, activation, betting, or ledger state.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path

from src.football.odds.isports import (
    ISPORTS_ADAPTER_VERSION,
    ISPORTS_COMPETITIONS,
    ISPORTS_PROVIDER_IDENTITY,
    ISPORTS_TOP5_LEAGUES,
    MAX_SOURCE_AGE_SECONDS,
    ISportsClient,
    ISportsContractError,
    ISportsFixture,
    ISportsHttpResponse,
    aggregate_1x2,
    isports_requests_transport,
    normalized_observation,
)
from src.football.top5_b4_provider_neutral_evidence import (
    AUTHORIZATION_PROVENANCE_SCHEMA_VERSION,
    MAX_ODDS_AGE_SECONDS,
    TOP5_LEAGUE_ORDER,
    B4ControlledShadowCaptureV1,
    B4ControlledShadowEvidenceV1,
    B4FixtureDiscoveryEvidenceV1,
    B4MarketEvidenceV1,
    B4ProviderOperationEvidenceV1,
    B4ProviderReadinessV1,
    B4ProviderUsageEvidenceV1,
    ProviderNeutralB4EvidenceError,
    Top5B4ProviderNeutralEvidenceDossierV1,
    canonical_evidence_digest,
)
from src.football.top5_signal_lifecycle import (
    DEFAULT_SIGNAL_LIFECYCLE_CONTRACT,
    LifecyclePlanStatus,
    SignalLifecycleStage,
    plan_signal_lifecycle,
)

MAX_RUN_REQUESTS = 7
ZERO_RETRIES = 0
AUTHORIZATION_FIELDS = frozenset(
    {
        "schema_version",
        "provider_identity",
        "controlled_shadow_run_id",
        "qualification_session_id",
        "authorization_id",
        "authorization_digest",
        "league_scope",
        "no_bet",
        "publication",
        "production_activation",
        "betting",
        "monetary_spend_authorized",
    }
)
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_INTEGER_HEADER_RE = re.compile(r"^\d+$")
_DEFAULT_MARKER_DIRECTORY = (
    Path.home() / ".codex" / "state" / "sportsbrain" / "top5-isports-b4"
)


class Top5ISportsB4CaptureError(RuntimeError):
    """Safe fail-closed result; message/code never contains response data."""

    def __init__(
        self,
        code: str,
        *,
        request_count: int = 0,
        missing_leagues: Sequence[str] = (),
    ) -> None:
        super().__init__(code)
        self.code = code
        self.request_count = request_count
        self.missing_leagues = tuple(missing_leagues)


@dataclass(frozen=True)
class Top5ISportsB4RunAuthorization:
    """The existing #190 B4 authorization-provenance payload, not an issuer."""

    provider_identity: str
    controlled_shadow_run_id: str
    qualification_session_id: str
    authorization_id: str
    authorization_digest: str
    payload: Mapping[str, object]

    @classmethod
    def from_payload(cls, payload: object) -> Top5ISportsB4RunAuthorization:
        if not isinstance(payload, Mapping) or set(payload) != AUTHORIZATION_FIELDS:
            raise Top5ISportsB4CaptureError("authorization_shape_invalid")
        body = {
            key: value
            for key, value in payload.items()
            if key != "authorization_digest"
        }
        if payload.get("schema_version") != AUTHORIZATION_PROVENANCE_SCHEMA_VERSION:
            raise Top5ISportsB4CaptureError("authorization_schema_invalid")
        if payload.get("provider_identity") != ISPORTS_PROVIDER_IDENTITY:
            raise Top5ISportsB4CaptureError("authorization_provider_mismatch")
        if payload.get("league_scope") != list(TOP5_LEAGUE_ORDER):
            raise Top5ISportsB4CaptureError("authorization_league_scope_mismatch")
        if payload.get("no_bet") is not True or any(
            payload.get(field) is not False
            for field in (
                "publication",
                "production_activation",
                "betting",
                "monetary_spend_authorized",
            )
        ):
            raise Top5ISportsB4CaptureError("authorization_safety_flags_invalid")
        for field in (
            "controlled_shadow_run_id",
            "qualification_session_id",
            "authorization_id",
        ):
            value = payload.get(field)
            if not isinstance(value, str) or not value.strip():
                raise Top5ISportsB4CaptureError("authorization_identity_invalid")
        digest = payload.get("authorization_digest")
        if (
            not isinstance(digest, str)
            or _DIGEST_RE.fullmatch(digest) is None
            or canonical_evidence_digest(body) != digest
        ):
            raise Top5ISportsB4CaptureError("authorization_digest_mismatch")
        return cls(
            provider_identity=ISPORTS_PROVIDER_IDENTITY,
            controlled_shadow_run_id=str(payload["controlled_shadow_run_id"]),
            qualification_session_id=str(payload["qualification_session_id"]),
            authorization_id=str(payload["authorization_id"]),
            authorization_digest=digest,
            payload=dict(payload),
        )


@dataclass(frozen=True)
class Top5ISportsB4RunResult:
    status: str
    request_count: int
    credential_access_count: int
    marker_path: Path | None
    output_path: Path | None
    dossier_digest: str | None
    b1_logical_input_keys: tuple[str, ...]


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise Top5ISportsB4CaptureError("clock_must_be_timezone_aware")
    return value.astimezone(timezone.utc)


def _initial_window_eligible(fixture: ISportsFixture, reference_time: datetime) -> bool:
    """Use the canonical B1 lifecycle planner for first-materialization scope."""

    reference = _utc(reference_time)
    if not fixture.prematch_eligible_at(reference):
        return False
    plan = plan_signal_lifecycle(
        fixture.fixture,
        reference,
        lifecycle=None,
        contract=DEFAULT_SIGNAL_LIFECYCLE_CONTRACT,
    )
    return (
        plan.status is LifecyclePlanStatus.INITIAL_DUE
        and plan.due_stage is SignalLifecycleStage.INITIAL
    )


def _run_configuration_digest(*, source_main_sha: str, adapter_source_sha: str) -> str:
    """Digest the fixed path semantics plus the exact source identities."""

    body = {
        "schema_version": "top5-isports-b4-capture-config-v1",
        "provider_identity": ISPORTS_PROVIDER_IDENTITY,
        "production_provider_authority": "the_odds_api",
        "b4_evidence_league_order": list(TOP5_LEAGUE_ORDER),
        "catalog_resolved_competitions": list(ISPORTS_COMPETITIONS),
        "scheduled_league_order": list(ISPORTS_TOP5_LEAGUES),
        "schedule_selection": "minimum(kickoff_utc,provider_match_id);status=0;future",
        "market_source": "European Odds",
        "market_type": "PRE_MATCH_1X2_REGULATION",
        "maximum_adapter_source_age_seconds": MAX_SOURCE_AGE_SECONDS,
        "maximum_b4_odds_age_seconds": MAX_ODDS_AGE_SECONDS,
        "maximum_request_count": MAX_RUN_REQUESTS,
        "retry_count": ZERO_RETRIES,
        "fallback_count": 0,
        "polling_count": 0,
        "no_bet": True,
        "publication": False,
        "production_activation": False,
        "betting": False,
        "ledger_mutated": False,
        "source_main_sha": source_main_sha,
        "adapter_source_sha": adapter_source_sha,
    }
    return canonical_evidence_digest(body)


def _header_integer(headers: Mapping[str, str], names: Sequence[str]) -> int | None:
    for name in names:
        raw = headers.get(name)
        if raw is None:
            continue
        value = raw.strip()
        if _INTEGER_HEADER_RE.fullmatch(value) is None:
            raise Top5ISportsB4CaptureError("provider_usage_header_malformed")
        return int(value)
    return None


def _response_usage(response: ISportsHttpResponse) -> B4ProviderUsageEvidenceV1:
    headers = response.safe_headers
    quota_remaining = _header_integer(
        headers, ("x-quota-remaining", "x-requests-remaining")
    )
    quota_limit = _header_integer(headers, ("x-quota-limit", "x-requests-limit"))
    rate_remaining = _header_integer(
        headers,
        ("x-rate-limit-remaining", "x-ratelimit-remaining", "ratelimit-remaining"),
    )
    rate_limit = _header_integer(
        headers, ("x-rate-limit", "x-ratelimit-limit", "ratelimit-limit")
    )
    usage = B4ProviderUsageEvidenceV1(
        quota_status=(
            "available"
            if quota_remaining is not None or quota_limit is not None
            else "not_exposed"
        ),
        quota_remaining_requests=quota_remaining,
        quota_limit_requests=quota_limit,
        rate_status=(
            "available"
            if rate_remaining is not None or rate_limit is not None
            else "not_exposed"
        ),
        rate_remaining_requests=rate_remaining,
        rate_limit_requests=rate_limit,
    )
    try:
        usage.validate()
    except ProviderNeutralB4EvidenceError as exc:
        raise Top5ISportsB4CaptureError("provider_usage_evidence_invalid") from exc
    return usage


def _operation(
    response: ISportsHttpResponse,
    *,
    operation_kind: str,
    authorization: Top5ISportsB4RunAuthorization,
) -> B4ProviderOperationEvidenceV1:
    response.validate()
    if response.status_code != 200 or response.transport_error_class is not None:
        raise Top5ISportsB4CaptureError("provider_http_operation_failed")
    safe_shape = {
        "provider_identity": ISPORTS_PROVIDER_IDENTITY,
        "method": "GET",
        "endpoint_path": response.endpoint_path,
        "parameters": dict(sorted(response.request_parameters.items())),
    }
    shape_digest = canonical_evidence_digest(safe_shape)
    operation_seed = canonical_evidence_digest(
        {
            "authorization_digest": authorization.authorization_digest,
            "request_ordinal": response.request_ordinal,
            "request_shape_digest": shape_digest,
        }
    )
    result = B4ProviderOperationEvidenceV1(
        provider_identity=ISPORTS_PROVIDER_IDENTITY,
        operation_id=f"isports-b4-operation:{operation_seed[:32]}",
        request_identity=f"isports-b4-request:{operation_seed[32:]}:{shape_digest[:16]}",
        operation_kind=operation_kind,
        endpoint_path=response.endpoint_path,
        request_ordinal=response.request_ordinal,
        request_count=1,
        retry_count=ZERO_RETRIES,
        http_status=200,
        requested_at=response.started_at,
        completed_at=response.completed_at,
        response_digest=response.response_digest,
        usage=_response_usage(response),
    )
    try:
        result.validate()
    except ProviderNeutralB4EvidenceError as exc:
        raise Top5ISportsB4CaptureError("provider_operation_evidence_invalid") from exc
    return result


def _build_dossier(
    *,
    client: ISportsClient,
    authorization: Top5ISportsB4RunAuthorization,
    source_main_sha: str,
    adapter_source_sha: str,
    now: Callable[[], datetime],
) -> Top5B4ProviderNeutralEvidenceDossierV1:
    """Run the fixed catalog → five schedules → one European Odds sequence."""

    if not isinstance(client, ISportsClient) or client.request_count != 0:
        raise Top5ISportsB4CaptureError("client_not_fresh")
    started_at = _utc(now())
    operations: list[B4ProviderOperationEvidenceV1] = []
    discoveries: list[B4FixtureDiscoveryEvidenceV1] = []
    selected_by_league: dict[str, ISportsFixture] = {}
    schedules_by_league: dict[
        str,
        tuple[
            tuple[ISportsFixture, ...],
            ISportsHttpResponse,
            B4ProviderOperationEvidenceV1,
        ],
    ] = {}

    try:
        if client.request_count != 0:
            raise Top5ISportsB4CaptureError("request_budget_exhausted")
        competitions, catalog_response = client.catalog()
        catalog_operation = _operation(
            catalog_response,
            operation_kind="competition_catalog",
            authorization=authorization,
        )
        operations.append(catalog_operation)
        if not set(TOP5_LEAGUE_ORDER) <= set(competitions):
            raise Top5ISportsB4CaptureError("catalog_missing_top5_competition")
    except Top5ISportsB4CaptureError:
        raise
    except Exception:  # noqa: BLE001 - provider failures are redacted fail-closed.
        raise Top5ISportsB4CaptureError(
            "catalog_failed", request_count=client.request_count
        ) from None

    for schedule_index, league in enumerate(ISPORTS_TOP5_LEAGUES, start=1):
        try:
            if client.request_count != schedule_index:
                raise Top5ISportsB4CaptureError("request_budget_exhausted")
            fixtures, schedule_response = client.schedule(competitions[league])
            schedule_operation = _operation(
                schedule_response,
                operation_kind="competition_schedule",
                authorization=authorization,
            )
            operations.append(schedule_operation)
            schedules_by_league[league] = (
                tuple(fixtures),
                schedule_response,
                schedule_operation,
            )
        except Top5ISportsB4CaptureError:
            raise
        except Exception:  # noqa: BLE001 - schedule failures stop without retry.
            raise Top5ISportsB4CaptureError(
                "schedule_failed", request_count=client.request_count
            ) from None

    # Selection is intentionally made only after every schedule response is
    # available, using one shared clock sample for all five leagues.
    selection_time = _utc(now())
    if any(
        response.completed_at > selection_time
        for _fixtures, response, _operation_item in schedules_by_league.values()
    ):
        raise Top5ISportsB4CaptureError(
            "selection_time_precedes_schedule_completion",
            request_count=client.request_count,
        )

    missing_leagues: list[str] = []
    for league in ISPORTS_TOP5_LEAGUES:
        fixtures, schedule_response, schedule_operation = schedules_by_league[league]
        eligible = tuple(
            fixture
            for fixture in fixtures
            if _initial_window_eligible(fixture, selection_time)
        )
        if not eligible:
            missing_leagues.append(league)
            continue
        selected = min(
            eligible,
            key=lambda item: (item.kickoff_utc, item.provider_match_id),
        )
        selected_by_league[league] = selected
        discoveries.append(
            B4FixtureDiscoveryEvidenceV1(
                provider_identity=ISPORTS_PROVIDER_IDENTITY,
                league=league,
                provider_competition_id=selected.provider_league_id,
                provider_fixture_id=selected.provider_match_id,
                fixture_key=selected.fixture.fixture_key,
                home_team=selected.home_team,
                away_team=selected.away_team,
                kickoff=selected.kickoff_utc,
                discovered_at=schedule_response.completed_at,
                operation_id=schedule_operation.operation_id,
                complete=True,
            )
        )

    if missing_leagues:
        raise Top5ISportsB4CaptureError(
            "initial_window_fixture_missing",
            request_count=client.request_count,
            missing_leagues=missing_leagues,
        )
    discoveries.sort(key=lambda item: TOP5_LEAGUE_ORDER.index(item.league))
    if tuple(item.league for item in discoveries) != TOP5_LEAGUE_ORDER:
        raise Top5ISportsB4CaptureError(
            "discovery_scope_mismatch", request_count=client.request_count
        )
    selected_ids = tuple(
        selected_by_league[league].provider_match_id for league in ISPORTS_TOP5_LEAGUES
    )
    if len(set(selected_ids)) != 5:
        raise Top5ISportsB4CaptureError(
            "duplicate_selected_fixture_id", request_count=client.request_count
        )

    expected_fixtures = {
        selected_by_league[league].provider_match_id: selected_by_league[league]
        for league in ISPORTS_TOP5_LEAGUES
    }
    try:
        if client.request_count != 6:
            raise Top5ISportsB4CaptureError("request_budget_exhausted")
        european_quotes, malformed_count, european_response = client.european_odds(
            expected_fixtures
        )
        bulk_operation = _operation(
            european_response,
            operation_kind="bulk_odds",
            authorization=authorization,
        )
        operations.append(bulk_operation)
    except Top5ISportsB4CaptureError:
        raise
    except Exception:  # noqa: BLE001 - never expose provider exception material.
        raise Top5ISportsB4CaptureError(
            "european_odds_failed", request_count=client.request_count
        ) from None

    # The selection must still be INITIAL-eligible at the actual provider
    # market-capture reference time. Never reselect after the bounded bulk call.
    market_reference_time = _utc(european_response.completed_at)
    timing_drift_leagues = tuple(
        league
        for league in TOP5_LEAGUE_ORDER
        if not _initial_window_eligible(
            selected_by_league[league], market_reference_time
        )
    )
    if timing_drift_leagues:
        raise Top5ISportsB4CaptureError(
            "initial_window_expired_after_bulk",
            request_count=client.request_count,
            missing_leagues=timing_drift_leagues,
        )

    if tuple(european_response.request_parameters) != ("matchId",) or (
        european_response.request_parameters.get("matchId") != ",".join(selected_ids)
    ):
        raise Top5ISportsB4CaptureError(
            "european_request_scope_mismatch", request_count=client.request_count
        )
    if malformed_count != 0:
        raise Top5ISportsB4CaptureError(
            "european_malformed_evidence", request_count=client.request_count
        )
    missing_markets = tuple(
        league
        for league in TOP5_LEAGUE_ORDER
        if not european_quotes.get(selected_by_league[league].provider_match_id)
    )
    if missing_markets:
        raise Top5ISportsB4CaptureError(
            "european_market_missing",
            request_count=client.request_count,
            missing_leagues=missing_markets,
        )
    if client.request_count != MAX_RUN_REQUESTS:
        raise Top5ISportsB4CaptureError(
            "request_count_not_exactly_seven", request_count=client.request_count
        )

    markets: list[B4MarketEvidenceV1] = []
    captures: list[B4ControlledShadowCaptureV1] = []
    for league in TOP5_LEAGUE_ORDER:
        fixture = selected_by_league[league]
        try:
            snapshot = aggregate_1x2(
                fixture,
                european_quotes=european_quotes[fixture.provider_match_id],
                captured_at=european_response.completed_at,
                european_response_digest=european_response.response_digest,
                malformed_row_count=malformed_count,
                max_age_seconds=MAX_ODDS_AGE_SECONDS,
            )
            observation = normalized_observation(
                snapshot,
                request_identity=bulk_operation.request_identity,
                request_started_at=european_response.started_at,
                request_completed_at=european_response.completed_at,
                adapter_source_sha=adapter_source_sha,
            )
        except Exception:  # noqa: BLE001 - normalization failures fail the whole run.
            raise Top5ISportsB4CaptureError(
                "european_market_normalization_failed",
                request_count=client.request_count,
                missing_leagues=(league,),
            ) from None

        discovery = discoveries[TOP5_LEAGUE_ORDER.index(league)]
        provider_record_digest = observation.metadata.get("provider_record_digest")
        normalized_digest = observation.metadata.get("normalized_record_digest")
        raw_digest = observation.raw_record_digest
        if not all(
            isinstance(value, str) and _DIGEST_RE.fullmatch(value)
            for value in (provider_record_digest, normalized_digest, raw_digest)
        ):
            raise Top5ISportsB4CaptureError(
                "normalized_digest_missing", request_count=client.request_count
            )
        market = B4MarketEvidenceV1(
            provider_identity=ISPORTS_PROVIDER_IDENTITY,
            league=league,
            provider_fixture_id=fixture.provider_match_id,
            request_identity=bulk_operation.request_identity,
            operation_id=bulk_operation.operation_id,
            source_identity="isports_api:european",
            source_provenance=observation.source_provenance,
            market_type="PRE_MATCH_1X2_REGULATION",
            home_odds=observation.home_odds,
            draw_odds=observation.draw_odds,
            away_odds=observation.away_odds,
            odds_timestamp=snapshot.source_timestamp,
            captured_at=snapshot.captured_at,
            raw_response_digest=raw_digest,
            provider_record_digest=provider_record_digest,
            normalized_observation_digest=normalized_digest,
        )
        try:
            market.validate(now=_utc(now()))
        except ProviderNeutralB4EvidenceError as exc:
            raise Top5ISportsB4CaptureError(
                "market_evidence_invalid",
                request_count=client.request_count,
                missing_leagues=(league,),
            ) from exc
        markets.append(market)

        cascade_digest = canonical_evidence_digest(
            {
                "schema_version": "top5-b4-isports-candidate-observation-v1",
                "provider_identity": ISPORTS_PROVIDER_IDENTITY,
                "league": league,
                "fixture_key": fixture.fixture.fixture_key,
                "request_identity": bulk_operation.request_identity,
                "market_evidence_digest": market.evidence_digest,
                "provider_record_digest": provider_record_digest,
                "normalized_observation_digest": normalized_digest,
            }
        )
        capture_attestation_digest = canonical_evidence_digest(
            {
                "schema_version": "top5-b4-isports-candidate-capture-attestation-v1",
                "provider_identity": ISPORTS_PROVIDER_IDENTITY,
                "evidence_kind": "REAL_OBSERVED",
                "network_execution": True,
                "candidate_only": True,
                "no_bet": True,
                "publication": False,
                "production_activation": False,
                "betting": False,
                "ledger_mutated": False,
                "discovery_evidence_digest": discovery.evidence_digest,
                "market_evidence_digest": market.evidence_digest,
                "cascade_evidence_digest": cascade_digest,
            }
        )
        captures.append(
            B4ControlledShadowCaptureV1(
                provider_identity=ISPORTS_PROVIDER_IDENTITY,
                league=league,
                fixture_key=fixture.fixture.fixture_key,
                provider_competition_id=fixture.provider_league_id,
                provider_fixture_id=fixture.provider_match_id,
                provider_request_id=bulk_operation.request_identity,
                adapter_version=ISPORTS_ADAPTER_VERSION,
                adapter_source_sha=adapter_source_sha,
                discovery_evidence_digest=discovery.evidence_digest,
                market_evidence_digest=market.evidence_digest,
                operation_evidence_ids=(
                    operations[0].operation_id,
                    discovery.operation_id,
                    bulk_operation.operation_id,
                ),
                source_timestamp=market.odds_timestamp,
                captured_at=market.captured_at,
                raw_response_digest=market.raw_response_digest,
                provider_record_digest=market.provider_record_digest,
                normalized_observation_digest=market.normalized_observation_digest,
                cascade_evidence_digest=cascade_digest,
                capture_attestation_digest=capture_attestation_digest,
            )
        )

    readiness = B4ProviderReadinessV1(
        provider_identity=ISPORTS_PROVIDER_IDENTITY,
        maximum_request_count=MAX_RUN_REQUESTS,
        # This is the fresh local one-shot run counter, not a claim about
        # account-wide provider usage. Provider limits stay in `usage` below.
        requests_consumed_before_run=0,
        authorized_run_request_count=MAX_RUN_REQUESTS,
        run_request_count=client.request_count,
        retry_count=ZERO_RETRIES,
        quota_required_by_provider_policy=False,
        usage_observed_at=started_at,
        # No pre-request provider quota counter is supplied by this contract.
        # Per-response quota/rate headers are recorded on their operations.
        usage=B4ProviderUsageEvidenceV1(),
    )
    config_digest = _run_configuration_digest(
        source_main_sha=source_main_sha, adapter_source_sha=adapter_source_sha
    )
    shadow = B4ControlledShadowEvidenceV1(
        provider_identity=ISPORTS_PROVIDER_IDENTITY,
        controlled_shadow_run_id=authorization.controlled_shadow_run_id,
        qualification_session_id=authorization.qualification_session_id,
        authorization_id=authorization.authorization_id,
        authorization_digest=authorization.authorization_digest,
        source_main_sha=source_main_sha,
        configuration_digest=config_digest,
        adapter_version=ISPORTS_ADAPTER_VERSION,
        adapter_source_sha=adapter_source_sha,
        authorization_provenance=dict(authorization.payload),
        operations=tuple(operations),
        discovery_evidence=tuple(discoveries),
        market_evidence=tuple(markets),
        readiness=readiness,
        captures=tuple(captures),
        no_bet=True,
        publication=False,
        production_activation=False,
        betting=False,
        ledger_mutated=False,
    )
    try:
        dossier = Top5B4ProviderNeutralEvidenceDossierV1.build(shadow, now=_utc(now()))
        b1_inputs = dossier.b1_evidence_inputs(now=_utc(now()))
    except ProviderNeutralB4EvidenceError:
        raise Top5ISportsB4CaptureError(
            "provider_neutral_dossier_validation_failed",
            request_count=client.request_count,
        ) from None
    if len(b1_inputs) != 10 or tuple(b1_inputs) != (
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
    ):
        raise Top5ISportsB4CaptureError(
            "b1_logical_handoff_shape_mismatch", request_count=client.request_count
        )
    return dossier


def _private_directory(path: Path) -> None:
    try:
        info = path.stat()
    except OSError as exc:
        raise Top5ISportsB4CaptureError("private_directory_unavailable") from exc
    if not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o077:
        raise Top5ISportsB4CaptureError("private_directory_permissions_invalid")


def _validate_output_path(path: Path, *, repository_root: Path | None) -> Path:
    if not path.is_absolute() or path.exists() or path.is_symlink():
        raise Top5ISportsB4CaptureError("output_path_must_be_new_absolute_path")
    parent = path.parent
    try:
        resolved_parent = parent.resolve(strict=True)
    except OSError:
        raise Top5ISportsB4CaptureError("output_parent_unavailable") from None
    if resolved_parent != Path(os.path.abspath(parent)):
        raise Top5ISportsB4CaptureError("output_parent_must_not_be_symlink")
    _private_directory(resolved_parent)
    if repository_root is not None:
        try:
            path.resolve().relative_to(repository_root.resolve())
        except ValueError:
            pass
        else:
            raise Top5ISportsB4CaptureError("output_must_be_outside_repository")
    return path


def _marker_path(
    marker_directory: Path, authorization: Top5ISportsB4RunAuthorization
) -> Path:
    marker_key = sha256(
        f"{authorization.provider_identity}\0{authorization.authorization_id}".encode()
    ).hexdigest()
    return marker_directory / f".top5-isports-b4-consumed-{marker_key}.json"


def _prepare_marker_directory(path: Path) -> None:
    if not path.is_absolute():
        raise Top5ISportsB4CaptureError("marker_directory_must_be_absolute")
    try:
        missing: list[Path] = []
        cursor = path
        while not cursor.exists() and not cursor.is_symlink():
            missing.append(cursor)
            cursor = cursor.parent
        if cursor.is_symlink() or not cursor.is_dir():
            raise Top5ISportsB4CaptureError("marker_directory_parent_invalid")
        for directory in reversed(missing):
            directory.mkdir(mode=0o700)
            directory.chmod(0o700)
        if path.is_symlink():
            raise Top5ISportsB4CaptureError("marker_directory_must_not_be_symlink")
        _private_directory(path)
    except Top5ISportsB4CaptureError:
        raise
    except Exception:  # noqa: BLE001 - filesystem details may expose local data.
        raise Top5ISportsB4CaptureError("marker_directory_unavailable") from None


def _consume_marker(
    path: Path, authorization: Top5ISportsB4RunAuthorization, now: datetime
) -> None:
    record = {
        "schema_version": "top5-isports-b4-one-shot-consumption-v1",
        "provider_identity": authorization.provider_identity,
        "authorization_id": authorization.authorization_id,
        "authorization_digest": authorization.authorization_digest,
        "consumed_at": _utc(now).isoformat(),
        "maximum_request_count": MAX_RUN_REQUESTS,
        "retry_count": ZERO_RETRIES,
    }
    encoded = json.dumps(record, sort_keys=True, separators=(",", ":")).encode()
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise Top5ISportsB4CaptureError("authorization_already_consumed") from exc
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
            os.fchmod(handle.fileno(), 0o400)
        dir_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except Exception:  # noqa: BLE001 - preserve consumed marker on any failure.
        # The O_EXCL marker is deliberately left in place after any partial
        # consumption attempt; this authorization can never be replayed.
        raise Top5ISportsB4CaptureError("authorization_consumption_failed") from None


def _write_immutable_json(path: Path, payload: Mapping[str, object]) -> None:
    encoded = (
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        + "\n"
    ).encode("utf-8")
    fd, temporary = tempfile.mkstemp(prefix=".top5-b4-dossier-", dir=path.parent)
    temp_path = Path(temporary)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
            os.fchmod(handle.fileno(), 0o400)
        try:
            os.link(temp_path, path)
        except FileExistsError as exc:
            raise Top5ISportsB4CaptureError("output_already_exists") from exc
        dir_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except Top5ISportsB4CaptureError:
        raise
    except Exception:  # noqa: BLE001 - fail closed on any incomplete write.
        raise Top5ISportsB4CaptureError("dossier_write_failed") from None
    finally:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass


def _credential_from_environment() -> str:
    value = os.environ.get("ISPORTS_API_KEY", "")
    if not isinstance(value, str) or not value.strip():
        raise Top5ISportsB4CaptureError("isports_credential_unavailable")
    return value.strip()


def _run_one_shot(
    *,
    authorization_payload: object,
    output_path: Path,
    execute_network: bool,
    source_main_sha: str,
    adapter_source_sha: str,
    repository_root: Path | None,
    now: Callable[[], datetime],
    marker_directory: Path,
    credential_loader: Callable[[], str],
    client_factory: Callable[[str], ISportsClient],
) -> Top5ISportsB4RunResult:
    """Internal runner; dependencies are injectable only for offline tests."""

    authorization = Top5ISportsB4RunAuthorization.from_payload(authorization_payload)
    if not re.fullmatch(r"[0-9a-fA-F]{40}", source_main_sha) or not re.fullmatch(
        r"[0-9a-fA-F]{40}", adapter_source_sha
    ):
        raise Top5ISportsB4CaptureError("source_identity_invalid")
    output_path = _validate_output_path(output_path, repository_root=repository_root)
    marker = _marker_path(marker_directory, authorization)
    if marker_directory.exists() or marker_directory.is_symlink():
        if marker_directory.is_symlink():
            raise Top5ISportsB4CaptureError("marker_directory_must_not_be_symlink")
        _private_directory(marker_directory)
    if marker.exists() or marker.is_symlink():
        raise Top5ISportsB4CaptureError("authorization_already_consumed")
    current_time = _utc(now())
    if not execute_network:
        return Top5ISportsB4RunResult(
            status="PREFLIGHT_READY",
            request_count=0,
            credential_access_count=0,
            marker_path=marker,
            output_path=output_path,
            dossier_digest=None,
            b1_logical_input_keys=(),
        )

    _prepare_marker_directory(marker_directory)
    _consume_marker(marker, authorization, current_time)
    credential_access_count = 0
    client: ISportsClient | None = None
    try:
        api_key = credential_loader()
        credential_access_count = 1
        if not isinstance(api_key, str) or not api_key.strip():
            raise Top5ISportsB4CaptureError("isports_credential_unavailable")
        client = client_factory(api_key.strip())
        dossier = _build_dossier(
            client=client,
            authorization=authorization,
            source_main_sha=source_main_sha.lower(),
            adapter_source_sha=adapter_source_sha.lower(),
            now=now,
        )
        payload = dossier.as_payload(now=_utc(now()))
        _write_immutable_json(output_path, payload)
        return Top5ISportsB4RunResult(
            status="COMPLETED",
            request_count=client.request_count,
            credential_access_count=credential_access_count,
            marker_path=marker,
            output_path=output_path,
            dossier_digest=dossier.dossier_digest,
            b1_logical_input_keys=tuple(dossier.b1_evidence_inputs(now=_utc(now()))),
        )
    except Top5ISportsB4CaptureError as exc:
        raise Top5ISportsB4CaptureError(
            exc.code,
            request_count=client.request_count
            if client is not None
            else exc.request_count,
            missing_leagues=exc.missing_leagues,
        ) from None
    except Exception:  # noqa: BLE001 - sanitize failures after marker consumption.
        raise Top5ISportsB4CaptureError(
            "capture_failed_closed",
            request_count=client.request_count if client is not None else 0,
        ) from None


def run_one_shot(
    *,
    authorization_payload: object,
    output_path: Path,
    execute_network: bool = False,
) -> Top5ISportsB4RunResult:
    """Production entry point; source identity and transport are not caller-set."""

    root = Path(__file__).resolve().parents[2]
    source_main_sha, adapter_source_sha = _checkout_identity(root)
    return _run_one_shot(
        authorization_payload=authorization_payload,
        output_path=output_path,
        execute_network=execute_network,
        source_main_sha=source_main_sha,
        adapter_source_sha=adapter_source_sha,
        repository_root=root,
        now=lambda: datetime.now(timezone.utc),
        marker_directory=_DEFAULT_MARKER_DIRECTORY,
        credential_loader=_credential_from_environment,
        client_factory=_make_bounded_client,
    )


def _make_bounded_client(api_key: str) -> ISportsClient:
    """Use the reviewed transport behind a hard seven-wire-call ceiling."""

    wire_calls = 0

    def bounded_transport(endpoint, params, credential, timeout):
        nonlocal wire_calls
        if wire_calls >= MAX_RUN_REQUESTS:
            raise ISportsContractError("Top-5 iSports request ceiling reached")
        wire_calls += 1
        return isports_requests_transport(endpoint, params, credential, timeout)

    return ISportsClient(api_key=api_key, transport=bounded_transport)


def _checkout_identity(repository_root: Path) -> tuple[str, str]:
    try:
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=repository_root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        if status.strip():
            raise Top5ISportsB4CaptureError("working_tree_must_be_clean")
        source_main_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repository_root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        adapter_source_sha = subprocess.run(
            ["git", "rev-parse", "HEAD:src/football/odds/isports.py"],
            cwd=repository_root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except Top5ISportsB4CaptureError:
        raise
    except Exception:  # noqa: BLE001 - never echo local command output.
        raise Top5ISportsB4CaptureError(
            "source_checkout_identity_unavailable"
        ) from None
    if not re.fullmatch(r"[0-9a-fA-F]{40}", source_main_sha) or not re.fullmatch(
        r"[0-9a-fA-F]{40}", adapter_source_sha
    ):
        raise Top5ISportsB4CaptureError("source_checkout_identity_invalid")
    return source_main_sha.lower(), adapter_source_sha.lower()


def _load_authorization_file(path: Path) -> Mapping[str, object]:
    if not path.is_absolute() or path.is_symlink():
        raise Top5ISportsB4CaptureError("authorization_path_invalid")
    try:
        info = path.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
            raise Top5ISportsB4CaptureError("authorization_file_permissions_invalid")
        if info.st_size > 64_000:
            raise Top5ISportsB4CaptureError("authorization_file_too_large")
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Top5ISportsB4CaptureError:
        raise
    except Exception:  # noqa: BLE001 - auth file contents are sensitive input.
        raise Top5ISportsB4CaptureError("authorization_file_unreadable") from None
    if not isinstance(raw, Mapping):
        raise Top5ISportsB4CaptureError("authorization_shape_invalid")
    return raw


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--authorization-file", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument(
        "--execute-network",
        action="store_true",
        help="consume the one-shot authorization and make the seven bounded requests",
    )
    args = parser.parse_args(argv)
    try:
        authorization = _load_authorization_file(args.authorization_file)
        result = run_one_shot(
            authorization_payload=authorization,
            output_path=args.output,
            execute_network=args.execute_network,
        )
        print(
            json.dumps(
                {
                    "status": result.status,
                    "provider": ISPORTS_PROVIDER_IDENTITY,
                    "request_count": result.request_count,
                    "credential_access_count": result.credential_access_count,
                    "maximum_request_count": MAX_RUN_REQUESTS,
                    "retry_count": ZERO_RETRIES,
                    "main_odds_requested": False,
                    "ucl_schedule_requested": False,
                    "marker_path": str(result.marker_path)
                    if result.marker_path
                    else None,
                    "output_path": str(result.output_path)
                    if result.output_path
                    else None,
                    "dossier_digest": result.dossier_digest,
                    "b1_logical_input_keys": list(result.b1_logical_input_keys),
                    "production_provider_authority": "the_odds_api",
                    "publication": False,
                    "production_activation": False,
                    "betting": False,
                    "ledger_mutated": False,
                },
                sort_keys=True,
            )
        )
        return 0
    except Top5ISportsB4CaptureError as exc:
        print(
            json.dumps(
                {
                    "status": "FAILED_CLOSED",
                    "reason": exc.code,
                    "request_count": exc.request_count,
                    "missing_leagues": list(exc.missing_leagues),
                    "retry_count": ZERO_RETRIES,
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "MAX_RUN_REQUESTS",
    "Top5ISportsB4CaptureError",
    "Top5ISportsB4RunAuthorization",
    "Top5ISportsB4RunResult",
    "main",
    "run_one_shot",
]
