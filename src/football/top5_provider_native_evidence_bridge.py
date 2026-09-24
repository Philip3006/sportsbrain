"""Typed bridge from provider-native Discovery evidence to B4/B1/B2 seams.

This module only validates and projects already-captured evidence.  It does not
perform provider I/O, issue authority, enable a shadow, or issue a receipt.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from hashlib import sha256

from src.football.odds.therundown import THERUNDOWN_PROVIDER_NAME
from src.football.top5_controlled_shadow_authorization_package import (
    FiveLeagueReconciliationV1,
    QualificationReadyArtifactsV1,
)
from src.football.top5_therundown_event_discovery import (
    DISCOVERY_LEAGUE_ORDER,
    EventDiscoveryContractError,
    TheRundownB4QuotaProofV1,
    TheRundownEventDiscoveryEvidenceV1,
)
from src.football.top5_therundown_network_shadow import (
    QUOTA_PROOF_AFFILIATE_IDS,
    THERUNDOWN_OBSERVED_DATAPOINTS_PER_REQUEST,
    TOP5_CONTROLLED_SHADOW_DATAPOINT_BUDGET,
    TOP5_CONTROLLED_SHADOW_MINIMUM_INTERVAL_SECONDS,
    TOP5_CONTROLLED_SHADOW_QUOTA_BUDGET,
    TOP5_CONTROLLED_SHADOW_REQUEST_COUNT,
    TheRundownNetworkConfigurationV1,
    TheRundownNetworkParticipantScopeV1,
    TheRundownNetworkRequestScopeV1,
    TheRundownQuotaHeadroomEvidenceV1,
)
from src.football.top5_therundown_provider_native_discovery import (
    PROVIDER_NATIVE_BILLING_MODE_CUMULATIVE,
    PROVIDER_NATIVE_BILLING_MODE_PROVIDER,
    PROVIDER_NATIVE_DISCOVERY_TARGET_SOURCE,
    PROVIDER_NATIVE_INDEPENDENT_QUALIFICATION,
    TheRundownProviderNativeDiscoveryRunResultV1,
    provider_native_discovery_request_shape_digest,
)
from src.football.top5_therundown_shadow_canary import TheRundownCanaryTargetV1

NATIVE_PROVENANCE_SCHEMA_VERSION = (
    "top5-therundown-provider-native-discovery-provenance-v1"
)
B4_DOSSIER_SCHEMA_VERSION = "top5-b4-evidence-dossier-v1"
_TOP5 = tuple(DISCOVERY_LEAGUE_ORDER)


class ProviderNativeEvidenceBridgeError(EventDiscoveryContractError):
    """Native evidence cannot safely cross the reviewed contract boundary."""


def _digest(value: object) -> str:
    return sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
    ).hexdigest()


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProviderNativeEvidenceBridgeError(f"{name} must be non-empty")
    return value.strip()


def _sha(value: object, name: str, *, length: int = 64) -> str:
    result = _text(value, name).lower()
    if len(result) != length or any(char not in "0123456789abcdef" for char in result):
        raise ProviderNativeEvidenceBridgeError(f"{name} must be a digest")
    return result


def _utc(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ProviderNativeEvidenceBridgeError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _mapping(value: object, name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ProviderNativeEvidenceBridgeError(f"{name} must be an object")
    return value


def _strict_keys(value: Mapping[str, object], expected: set[str], name: str) -> None:
    if set(value) != expected:
        raise ProviderNativeEvidenceBridgeError(f"{name} payload shape is invalid")


def _quota_proof_payload(proof: TheRundownB4QuotaProofV1) -> dict[str, object]:
    """Serialize the typed B4 proof without inventing a second proof contract."""
    return {
        "proof_id": proof.proof_id,
        "authorization_id": proof.authorization_id,
        "provider": proof.provider,
        "sport_id": proof.sport_id,
        "snapshot_date": proof.snapshot_date.isoformat(),
        "account_scope": proof.account_scope,
        "remaining_datapoints": proof.remaining_datapoints,
        "response_started_at": _utc(
            proof.response_started_at, "proof response_started_at"
        ).isoformat(),
        "response_finished_at": _utc(
            proof.response_finished_at, "proof response_finished_at"
        ).isoformat(),
        "quota_reset_at": _utc(
            proof.quota_reset_at, "proof quota_reset_at"
        ).isoformat(),
        "response_digest": proof.response_digest,
        "evidence_digest": proof.evidence_digest,
        "request_shape_digest": proof.request_shape_digest,
        "quota_used_datapoints": proof.quota_used_datapoints,
        "quota_limit_datapoints": proof.quota_limit_datapoints,
        "quota_period": proof.quota_period,
        "quota_tier": proof.quota_tier,
    }


@dataclass(frozen=True)
class ProviderNativeDiscoveryProvenanceV1:
    """Deterministic provenance for the native-to-legacy projection."""

    provider: str
    discovery_target_source: str
    independent_fixture_source_qualification: str
    provider_affiliate_ids: tuple[str, ...]
    discovery_authorization_id: str
    discovery_authorization_digest: str
    request_shape_digest: str
    native_run_digest: str
    leagues: tuple[str, ...]
    capture_evidence_digests: tuple[str, ...]
    provider_event_ids: tuple[str, ...]
    participant_ids: tuple[str, ...]
    fixture_keys: tuple[str, ...]
    request_identities: tuple[str, ...]
    request_count: int
    datapoint_total: int
    billing_modes: tuple[str, ...]
    billing_datapoints: tuple[int, ...]
    raw_response_digests: tuple[str, ...]
    adapter_version: str
    adapter_source_sha: str
    provenance_digest: str

    def _payload_without_digest(self) -> dict[str, object]:
        return {
            "schema_version": NATIVE_PROVENANCE_SCHEMA_VERSION,
            "provider": self.provider,
            "discovery_target_source": self.discovery_target_source,
            "independent_fixture_source_qualification": self.independent_fixture_source_qualification,
            "provider_affiliate_ids": list(self.provider_affiliate_ids),
            "discovery_authorization_id": self.discovery_authorization_id,
            "discovery_authorization_digest": self.discovery_authorization_digest,
            "request_shape_digest": self.request_shape_digest,
            "native_run_digest": self.native_run_digest,
            "leagues": list(self.leagues),
            "capture_evidence_digests": list(self.capture_evidence_digests),
            "provider_event_ids": list(self.provider_event_ids),
            "participant_ids": list(self.participant_ids),
            "fixture_keys": list(self.fixture_keys),
            "request_identities": list(self.request_identities),
            "request_count": self.request_count,
            "datapoint_total": self.datapoint_total,
            "billing_modes": list(self.billing_modes),
            "billing_datapoints": list(self.billing_datapoints),
            "raw_response_digests": list(self.raw_response_digests),
            "adapter_version": self.adapter_version,
            "adapter_source_sha": self.adapter_source_sha,
        }

    @property
    def computed_provenance_digest(self) -> str:
        return _digest(self._payload_without_digest())

    def validate(self) -> None:
        if self.provider != THERUNDOWN_PROVIDER_NAME:
            raise ProviderNativeEvidenceBridgeError(
                "native provenance provider mismatch"
            )
        if self.discovery_target_source != PROVIDER_NATIVE_DISCOVERY_TARGET_SOURCE:
            raise ProviderNativeEvidenceBridgeError("native provenance source mismatch")
        if (
            self.independent_fixture_source_qualification
            != PROVIDER_NATIVE_INDEPENDENT_QUALIFICATION
        ):
            raise ProviderNativeEvidenceBridgeError("native provenance waiver mismatch")
        if self.provider_affiliate_ids != QUOTA_PROOF_AFFILIATE_IDS:
            raise ProviderNativeEvidenceBridgeError("native affiliate scope mismatch")
        if self.leagues != _TOP5 or len(self.capture_evidence_digests) != 5:
            raise ProviderNativeEvidenceBridgeError(
                "native provenance league coverage is invalid"
            )
        for name, value in (
            ("discovery_authorization_id", self.discovery_authorization_id),
            ("adapter_version", self.adapter_version),
        ):
            _text(value, name)
        for name, value in (
            ("discovery_authorization_digest", self.discovery_authorization_digest),
            ("request_shape_digest", self.request_shape_digest),
            ("native_run_digest", self.native_run_digest),
            ("adapter_source_sha", self.adapter_source_sha),
            ("provenance_digest", self.provenance_digest),
        ):
            _sha(value, name, length=40 if name == "adapter_source_sha" else 64)
        for value in self.capture_evidence_digests:
            _sha(value, "capture evidence digest")
        for value in self.raw_response_digests:
            _sha(value, "raw response digest")
        if len(self.provider_event_ids) != 5 or len(self.participant_ids) != 10:
            raise ProviderNativeEvidenceBridgeError(
                "native identity cardinality is invalid"
            )
        arrays = (
            self.provider_event_ids,
            self.fixture_keys,
            self.request_identities,
            self.raw_response_digests,
        )
        if any(len(items) != 5 for items in arrays):
            raise ProviderNativeEvidenceBridgeError(
                "native provenance array cardinality is invalid"
            )
        if any(
            not _text(item, "native identity")
            for items in (*arrays, self.participant_ids)
            for item in items
        ):
            raise ProviderNativeEvidenceBridgeError("native identity is empty")
        if len(set(self.provider_event_ids)) != 5 or len(set(self.fixture_keys)) != 5:
            raise ProviderNativeEvidenceBridgeError(
                "native event or fixture identities are duplicated"
            )
        if (
            len(set(self.participant_ids)) != 10
            or len(set(self.request_identities)) != 5
        ):
            raise ProviderNativeEvidenceBridgeError(
                "native participant or request identities are duplicated"
            )
        if (
            self.request_count < 5
            or self.request_count > 105
            or self.datapoint_total < 0
        ):
            raise ProviderNativeEvidenceBridgeError("native billing totals are invalid")
        if (
            len(self.billing_modes) != self.request_count
            or len(self.billing_datapoints) != self.request_count
            or len(self.raw_response_digests) != self.request_count
        ):
            raise ProviderNativeEvidenceBridgeError(
                "native billing arrays do not match request count"
            )
        if any(
            mode
            not in {
                PROVIDER_NATIVE_BILLING_MODE_PROVIDER,
                PROVIDER_NATIVE_BILLING_MODE_CUMULATIVE,
            }
            for mode in self.billing_modes
        ):
            raise ProviderNativeEvidenceBridgeError(
                "native billing mode is unsupported"
            )
        if any(
            value < 0 or value > THERUNDOWN_OBSERVED_DATAPOINTS_PER_REQUEST
            for value in self.billing_datapoints
        ):
            raise ProviderNativeEvidenceBridgeError(
                "native per-request billing exceeds the bound"
            )
        if sum(self.billing_datapoints) != self.datapoint_total:
            raise ProviderNativeEvidenceBridgeError(
                "native billing total does not reconcile"
            )
        if self.provenance_digest != self.computed_provenance_digest:
            raise ProviderNativeEvidenceBridgeError("native provenance digest mismatch")

    def as_payload(self) -> dict[str, object]:
        self.validate()
        return {
            **self._payload_without_digest(),
            "provenance_digest": self.provenance_digest,
        }

    @classmethod
    def from_payload(cls, payload: object) -> ProviderNativeDiscoveryProvenanceV1:
        raw = _mapping(payload, "native provenance")
        required = {
            "schema_version",
            "provider",
            "discovery_target_source",
            "independent_fixture_source_qualification",
            "provider_affiliate_ids",
            "discovery_authorization_id",
            "discovery_authorization_digest",
            "request_shape_digest",
            "native_run_digest",
            "leagues",
            "capture_evidence_digests",
            "provider_event_ids",
            "participant_ids",
            "fixture_keys",
            "request_identities",
            "request_count",
            "datapoint_total",
            "billing_modes",
            "billing_datapoints",
            "raw_response_digests",
            "adapter_version",
            "adapter_source_sha",
            "provenance_digest",
        }
        _strict_keys(raw, required, "native provenance")
        if raw.get("schema_version") != NATIVE_PROVENANCE_SCHEMA_VERSION:
            raise ProviderNativeEvidenceBridgeError(
                "native provenance schema is unsupported"
            )

        def _strings(name: str) -> tuple[str, ...]:
            value = raw.get(name)
            if not isinstance(value, list):
                raise ProviderNativeEvidenceBridgeError(f"{name} must be a list")
            return tuple(_text(item, name) for item in value)

        billing_datapoints_raw = raw.get("billing_datapoints")
        if not isinstance(billing_datapoints_raw, list) or any(
            not isinstance(item, int) or isinstance(item, bool)
            for item in billing_datapoints_raw
        ):
            raise ProviderNativeEvidenceBridgeError(
                "billing_datapoints must be integer values"
            )
        request_count = raw.get("request_count")
        datapoint_total = raw.get("datapoint_total")
        if any(
            not isinstance(item, int) or isinstance(item, bool)
            for item in (request_count, datapoint_total)
        ):
            raise ProviderNativeEvidenceBridgeError(
                "native billing totals must be integers"
            )
        provenance = cls(
            provider=_text(raw.get("provider"), "provider"),
            discovery_target_source=_text(raw.get("discovery_target_source"), "source"),
            independent_fixture_source_qualification=_text(
                raw.get("independent_fixture_source_qualification"), "waiver"
            ),
            provider_affiliate_ids=_strings("provider_affiliate_ids"),
            discovery_authorization_id=_text(
                raw.get("discovery_authorization_id"), "authorization id"
            ),
            discovery_authorization_digest=_sha(
                raw.get("discovery_authorization_digest"), "authorization digest"
            ),
            request_shape_digest=_sha(
                raw.get("request_shape_digest"), "request shape digest"
            ),
            native_run_digest=_sha(raw.get("native_run_digest"), "native run digest"),
            leagues=_strings("leagues"),
            capture_evidence_digests=tuple(
                _sha(item, "capture evidence digest")
                for item in _strings("capture_evidence_digests")
            ),
            provider_event_ids=_strings("provider_event_ids"),
            participant_ids=_strings("participant_ids"),
            fixture_keys=_strings("fixture_keys"),
            request_identities=_strings("request_identities"),
            request_count=request_count,
            datapoint_total=datapoint_total,
            billing_modes=_strings("billing_modes"),
            billing_datapoints=tuple(billing_datapoints_raw),
            raw_response_digests=tuple(
                _sha(item, "raw response digest")
                for item in _strings("raw_response_digests")
            ),
            adapter_version=_text(raw.get("adapter_version"), "adapter version"),
            adapter_source_sha=_sha(
                raw.get("adapter_source_sha"), "adapter source sha", length=40
            ),
            provenance_digest=_sha(raw.get("provenance_digest"), "provenance digest"),
        )
        provenance.validate()
        return provenance


def build_provider_native_discovery_provenance(
    run: TheRundownProviderNativeDiscoveryRunResultV1,
) -> ProviderNativeDiscoveryProvenanceV1:
    run.validate()
    authorization = run.authorization
    expected_shape = provider_native_discovery_request_shape_digest(
        authorization.search_start_date
    )
    if authorization.request_shape_digest != expected_shape:
        raise ProviderNativeEvidenceBridgeError("native request shape is not current")
    for capture in run.captures:
        capture.validate()
        if (
            capture.discovery_authorization_id
            != authorization.discovery_authorization_id
            or capture.discovery_authorization_digest
            != authorization.authorization_digest
            or capture.request_shape_digest != authorization.request_shape_digest
            or capture.snapshot_date != authorization.search_start_date
        ):
            raise ProviderNativeEvidenceBridgeError(
                "native capture authorization binding mismatch"
            )
        if _utc(capture.response_completed_at, "response completed") < _utc(
            capture.request_started_at, "request started"
        ):
            raise ProviderNativeEvidenceBridgeError(
                "native capture timestamps are invalid"
            )
    provenance = ProviderNativeDiscoveryProvenanceV1(
        provider=authorization.provider,
        discovery_target_source=PROVIDER_NATIVE_DISCOVERY_TARGET_SOURCE,
        independent_fixture_source_qualification=PROVIDER_NATIVE_INDEPENDENT_QUALIFICATION,
        provider_affiliate_ids=QUOTA_PROOF_AFFILIATE_IDS,
        discovery_authorization_id=authorization.discovery_authorization_id,
        discovery_authorization_digest=authorization.authorization_digest,
        request_shape_digest=authorization.request_shape_digest,
        native_run_digest=run.run_digest,
        leagues=tuple(capture.league for capture in run.captures),
        capture_evidence_digests=tuple(
            capture.evidence_digest for capture in run.captures
        ),
        provider_event_ids=tuple(capture.provider_event_id for capture in run.captures),
        participant_ids=tuple(
            participant
            for capture in run.captures
            for participant in (
                capture.home_participant_id,
                capture.away_participant_id,
            )
        ),
        fixture_keys=tuple(capture.fixture_key for capture in run.captures),
        request_identities=tuple(capture.request_identity for capture in run.captures),
        request_count=run.request_count,
        datapoint_total=run.datapoint_total,
        billing_modes=run.billing_modes,
        billing_datapoints=run.billing_datapoints,
        raw_response_digests=run.raw_response_digests,
        adapter_version=authorization.adapter_version,
        adapter_source_sha=authorization.adapter_source_sha,
        provenance_digest="",
    )
    provenance = replace(
        provenance, provenance_digest=provenance.computed_provenance_digest
    )
    provenance.validate()
    return provenance


def project_provider_native_discovery_evidence(
    run: TheRundownProviderNativeDiscoveryRunResultV1,
) -> tuple[TheRundownEventDiscoveryEvidenceV1, ...]:
    """Project native captures into the existing legacy evidence contract."""
    build_provider_native_discovery_provenance(run)
    authorization = run.authorization
    projected: list[TheRundownEventDiscoveryEvidenceV1] = []
    for capture in run.captures:
        item = TheRundownEventDiscoveryEvidenceV1(
            discovery_authorization_id=capture.discovery_authorization_id,
            discovery_authorization_digest=capture.discovery_authorization_digest,
            provider=capture.provider,
            league=capture.league,
            fixture_key=capture.fixture_key,
            home_team=capture.home_team,
            away_team=capture.away_team,
            home_participant_id=capture.home_participant_id,
            away_participant_id=capture.away_participant_id,
            kickoff=capture.kickoff,
            provider_event_id=capture.provider_event_id,
            request_identity=capture.request_identity,
            request_shape_digest=capture.request_shape_digest,
            request_started_at=capture.request_started_at,
            response_completed_at=capture.response_completed_at,
            raw_response_digest=capture.raw_response_digest,
            provider_event_evidence_digest="0" * 64,
            datapoints=capture.datapoints,
            remaining_datapoints=capture.remaining_datapoints,
            retry_count=capture.retry_count,
            network_execution=capture.network_execution,
        )
        item = replace(
            item,
            provider_event_evidence_digest=item.computed_provider_event_evidence_digest,
        )
        item.validate()
        projected.append(item)
    if tuple(item.league for item in projected) != _TOP5:
        raise ProviderNativeEvidenceBridgeError("legacy projection order is invalid")
    if (
        projected[0].discovery_authorization_id
        != authorization.discovery_authorization_id
    ):
        raise ProviderNativeEvidenceBridgeError(
            "legacy projection authorization mismatch"
        )
    return tuple(projected)


def materialize_prebound_network_configuration_from_provider_native(
    run: TheRundownProviderNativeDiscoveryRunResultV1,
) -> TheRundownNetworkConfigurationV1:
    """Build only the disabled configuration from validated native captures."""
    if not run.captures:
        raise ProviderNativeEvidenceBridgeError("native run has no captures")
    provenance = build_provider_native_discovery_provenance(run)
    projected = project_provider_native_discovery_evidence(run)
    targets = tuple(
        TheRundownCanaryTargetV1(
            provider=capture.provider,
            league=capture.league,
            fixture_key=capture.fixture_key,
            provider_event_id=capture.provider_event_id,
            home_team=capture.home_team,
            away_team=capture.away_team,
            kickoff=capture.kickoff,
        )
        for capture in run.captures
    )
    participant_scope = tuple(
        TheRundownNetworkParticipantScopeV1(
            fixture_key=capture.fixture_key,
            home_participant_id=capture.home_participant_id,
            away_participant_id=capture.away_participant_id,
        )
        for capture in run.captures
    )
    request_scope = tuple(
        TheRundownNetworkRequestScopeV1(
            fixture_key=capture.fixture_key,
            request_identity=capture.request_identity,
        )
        for capture in run.captures
    )
    configuration = TheRundownNetworkConfigurationV1(
        targets=targets,
        participant_scope=participant_scope,
        request_scope=request_scope,
        adapter_version=run.authorization.adapter_version,
        adapter_source_sha=run.authorization.adapter_source_sha,
        maximum_request_count=TOP5_CONTROLLED_SHADOW_REQUEST_COUNT,
        maximum_datapoints=TOP5_CONTROLLED_SHADOW_DATAPOINT_BUDGET,
        maximum_quota_cost_units=TOP5_CONTROLLED_SHADOW_QUOTA_BUDGET,
        request_quota_cost_units=THERUNDOWN_OBSERVED_DATAPOINTS_PER_REQUEST,
        maximum_source_age_seconds=300,
        minimum_interval_seconds=max(
            run.authorization.minimum_interval_seconds,
            TOP5_CONTROLLED_SHADOW_MINIMUM_INTERVAL_SECONDS,
        ),
        maximum_retries=0,
        enabled=False,
        no_bet=True,
        publication=False,
        production_activation=False,
        monetary_spend_authorized=False,
        configuration_digest="",
        provider_affiliate_ids=QUOTA_PROOF_AFFILIATE_IDS,
    )
    configuration = replace(
        configuration, configuration_digest=configuration.computed_configuration_digest
    )
    configuration.validate()
    if tuple(item.fixture_key for item in projected) != tuple(
        target.fixture_key for target in configuration.targets
    ) or provenance.request_identities != tuple(
        item.request_identity for item in configuration.request_scope
    ):
        raise ProviderNativeEvidenceBridgeError(
            "native-to-shadow identity cross-check failed"
        )
    return configuration


def validate_native_provenance_against_legacy(
    provenance_payload: object,
    legacy_evidence: Sequence[Mapping[str, object]],
) -> None:
    """Validate B1's native provenance field without weakening legacy checks."""
    provenance = ProviderNativeDiscoveryProvenanceV1.from_payload(provenance_payload)
    if len(legacy_evidence) != 5:
        raise ProviderNativeEvidenceBridgeError(
            "legacy discovery evidence must contain five items"
        )
    for index, item in enumerate(legacy_evidence):
        if not isinstance(item, Mapping):
            raise ProviderNativeEvidenceBridgeError(
                "legacy discovery evidence is malformed"
            )
        expected = {
            "provider": provenance.provider,
            "league": provenance.leagues[index],
            "fixture_key": provenance.fixture_keys[index],
            "provider_event_id": provenance.provider_event_ids[index],
            "request_identity": provenance.request_identities[index],
            "raw_response_digest": provenance.raw_response_digests[index],
            "discovery_authorization_id": provenance.discovery_authorization_id,
            "discovery_authorization_digest": provenance.discovery_authorization_digest,
            "request_shape_digest": provenance.request_shape_digest,
        }
        for name, value in expected.items():
            if item.get(name) != value:
                raise ProviderNativeEvidenceBridgeError(
                    f"native provenance diverges from legacy evidence: {name}"
                )
        if item.get("home_participant_id") != provenance.participant_ids[index * 2]:
            raise ProviderNativeEvidenceBridgeError(
                "home participant provenance mismatch"
            )
        if item.get("away_participant_id") != provenance.participant_ids[index * 2 + 1]:
            raise ProviderNativeEvidenceBridgeError(
                "away participant provenance mismatch"
            )


@dataclass(frozen=True)
class Top5B4EvidenceDossierV1:
    """Typed final B4 handoff; it contains no receipt or authority."""

    source_main_sha: str
    quota_proof: TheRundownB4QuotaProofV1
    native_provenance: ProviderNativeDiscoveryProvenanceV1
    legacy_discovery_evidence: tuple[TheRundownEventDiscoveryEvidenceV1, ...]
    configuration: TheRundownNetworkConfigurationV1
    reconciliation: FiveLeagueReconciliationV1
    qualification: QualificationReadyArtifactsV1
    controlled_shadow_evidence: QualificationReadyArtifactsV1
    independent_fixture_source_qualification: str = (
        PROVIDER_NATIVE_INDEPENDENT_QUALIFICATION
    )
    shadow_headroom: TheRundownQuotaHeadroomEvidenceV1 | None = None
    dossier_digest: str = ""

    def _payload_without_digest(self) -> dict[str, object]:
        return {
            "schema_version": B4_DOSSIER_SCHEMA_VERSION,
            "source_main_sha": self.source_main_sha,
            "quota_proof": _quota_proof_payload(self.quota_proof),
            "native_provenance": self.native_provenance.as_payload(),
            "legacy_discovery_evidence": [
                item.as_payload() for item in self.legacy_discovery_evidence
            ],
            "configuration": self.configuration.as_payload(),
            "reconciliation": self.reconciliation.as_payload(),
            "qualification": self.qualification.as_payload(),
            "controlled_shadow_evidence": self.controlled_shadow_evidence.as_payload(),
            "independent_fixture_source_qualification": self.independent_fixture_source_qualification,
            "shadow_headroom": (
                self.shadow_headroom.as_payload()
                if self.shadow_headroom is not None
                else None
            ),
        }

    @property
    def computed_dossier_digest(self) -> str:
        return _digest(self._payload_without_digest())

    def validate(self, *, now: datetime) -> None:
        _sha(self.source_main_sha, "source_main_sha", length=40)
        self.quota_proof.validate(now=now)
        self.native_provenance.validate()
        if tuple(item.league for item in self.legacy_discovery_evidence) != _TOP5:
            raise ProviderNativeEvidenceBridgeError(
                "dossier legacy evidence order is invalid"
            )
        for item in self.legacy_discovery_evidence:
            item.validate()
        validate_native_provenance_against_legacy(
            self.native_provenance.as_payload(),
            tuple(item.as_payload() for item in self.legacy_discovery_evidence),
        )
        self.configuration.validate()
        self.reconciliation.validate()
        self.qualification.validate()
        self.controlled_shadow_evidence.validate()
        if (
            self.independent_fixture_source_qualification
            != PROVIDER_NATIVE_INDEPENDENT_QUALIFICATION
        ):
            raise ProviderNativeEvidenceBridgeError("dossier waiver is invalid")
        if (
            self.qualification.as_payload()
            != self.reconciliation.artifacts.as_payload()
        ):
            raise ProviderNativeEvidenceBridgeError(
                "dossier qualification artifacts diverge from reconciliation"
            )
        if (
            self.controlled_shadow_evidence.as_payload()
            != self.reconciliation.artifacts.as_payload()
        ):
            raise ProviderNativeEvidenceBridgeError(
                "dossier controlled-shadow evidence diverges from reconciliation"
            )
        if (
            self.reconciliation.configuration_digest
            != self.configuration.configuration_digest
        ):
            raise ProviderNativeEvidenceBridgeError(
                "dossier configuration digest mismatch"
            )
        if (
            self.native_provenance.provider_event_ids
            != self.reconciliation.provider_event_ids
        ):
            raise ProviderNativeEvidenceBridgeError(
                "dossier provider event identity mismatch"
            )
        if self.native_provenance.fixture_keys != self.reconciliation.fixture_keys:
            raise ProviderNativeEvidenceBridgeError("dossier fixture identity mismatch")
        if (
            self.native_provenance.request_identities
            != self.reconciliation.provider_request_ids
        ):
            raise ProviderNativeEvidenceBridgeError("dossier request identity mismatch")
        if self.native_provenance.request_identities != tuple(
            item.request_identity for item in self.configuration.request_scope
        ):
            raise ProviderNativeEvidenceBridgeError(
                "dossier shadow request identity mismatch"
            )
        if self.configuration.provider_affiliate_ids != QUOTA_PROOF_AFFILIATE_IDS:
            raise ProviderNativeEvidenceBridgeError("dossier affiliate scope mismatch")
        if self.shadow_headroom is not None:
            self.shadow_headroom.validate(now=now)
        if self.dossier_digest != self.computed_dossier_digest:
            raise ProviderNativeEvidenceBridgeError("dossier digest mismatch")

    def as_payload(self) -> dict[str, object]:
        return {
            **self._payload_without_digest(),
            "dossier_digest": self.dossier_digest,
        }


def assemble_top5_b4_evidence_dossier(
    *,
    source_main_sha: str,
    quota_proof: TheRundownB4QuotaProofV1,
    native_run: TheRundownProviderNativeDiscoveryRunResultV1,
    configuration: TheRundownNetworkConfigurationV1,
    reconciliation: FiveLeagueReconciliationV1,
    shadow_headroom: TheRundownQuotaHeadroomEvidenceV1 | None,
    now: datetime,
) -> Top5B4EvidenceDossierV1:
    provenance = build_provider_native_discovery_provenance(native_run)
    legacy = project_provider_native_discovery_evidence(native_run)
    expected_configuration = (
        materialize_prebound_network_configuration_from_provider_native(native_run)
    )
    if configuration.as_payload() != expected_configuration.as_payload():
        raise ProviderNativeEvidenceBridgeError(
            "supplied configuration is not native-derived"
        )
    dossier = Top5B4EvidenceDossierV1(
        source_main_sha=source_main_sha,
        quota_proof=quota_proof,
        native_provenance=provenance,
        legacy_discovery_evidence=legacy,
        configuration=configuration,
        reconciliation=reconciliation,
        qualification=reconciliation.artifacts,
        controlled_shadow_evidence=reconciliation.artifacts,
        independent_fixture_source_qualification=provenance.independent_fixture_source_qualification,
        shadow_headroom=shadow_headroom,
        dossier_digest="",
    )
    dossier = replace(dossier, dossier_digest=dossier.computed_dossier_digest)
    dossier.validate(now=now)
    return dossier
