"""Receipt-only aggregation for Top-5 qualification sample sufficiency.

The aggregator consumes only already validated
``Builder2QualificationReceiptV1`` artifacts.  It does not issue receipts,
read observations or reports, call providers, select authorities, bind models,
publish, bet, mutate a ledger, or authorize production activation.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace

from src.football.top5_builder2_qualification_receipt import (
    Builder2QualificationReceiptError,
    Builder2QualificationReceiptV1,
    semantic_digest,
)
from src.football.top5_controlled_shadow_provider_qualification import (
    TOP5_LEAGUES,
    MinimumSamplePolicy,
    QualificationContractError,
)

BUILDER2_QUALIFICATION_SAMPLE_AGGREGATOR_CONTRACT_VERSION = (
    "top5-builder2-qualification-sample-aggregator-v1"
)
QUALIFICATION_SAMPLE_AGGREGATOR_SCHEMA_VERSION = (
    BUILDER2_QUALIFICATION_SAMPLE_AGGREGATOR_CONTRACT_VERSION
)
SAMPLE_AGGREGATOR_SCHEMA_VERSION = QUALIFICATION_SAMPLE_AGGREGATOR_SCHEMA_VERSION

FAILURE_DUPLICATE_RECEIPT = "DUPLICATE_RECEIPT"
FAILURE_DUPLICATE_OBSERVATION = "DUPLICATE_OBSERVATION"
FAILURE_DIVERGENT_RECEIPT_ID_CONFLICT = "DIVERGENT_RECEIPT_ID_CONFLICT"
FAILURE_OBSERVATION_DIGEST_CONFLICT = "OBSERVATION_DIGEST_CONFLICT"
FAILURE_OBSERVATION_ID_CONFLICT = "OBSERVATION_ID_CONFLICT"
FAILURE_FIXTURE_PROVIDER_CONFLICT = "FIXTURE_PROVIDER_CONFLICT"
FAILURE_UNATTRIBUTED_LEAGUE = "UNATTRIBUTED_LEAGUE"

_SHA_RE = re.compile(r"^[0-9a-fA-F]{64}$")


class Builder2QualificationSampleAggregatorError(QualificationContractError):
    """Malformed receipt input or unsafe aggregation state."""


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise Builder2QualificationSampleAggregatorError(
            f"{name} must be non-empty text"
        )
    return value


def _sha(value: object, name: str) -> str:
    value = _text(value, name)
    if _SHA_RE.fullmatch(value) is None:
        raise Builder2QualificationSampleAggregatorError(
            f"{name} must be a 64-character hexadecimal digest"
        )
    return value


def _count_map(value: Mapping[str, object], name: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for key, raw_count in value.items():
        key = _text(key, f"{name} key")
        if (
            not isinstance(raw_count, int)
            or isinstance(raw_count, bool)
            or raw_count < 0
        ):
            raise Builder2QualificationSampleAggregatorError(
                f"{name} values must be non-negative integers"
            )
        counts[key] = raw_count
    return counts


def _sorted_unique(values: Iterable[str], name: str) -> tuple[str, ...]:
    normalized = tuple(_text(value, name) for value in values)
    return tuple(sorted(set(normalized)))


def _fixture_league(fixture_key: str) -> str | None:
    """Return a league only when the receipt carries a canonical fixture key."""

    parts = fixture_key.split("|")
    if len(parts) != 4 or parts[0] not in TOP5_LEAGUES:
        return None
    return parts[0]


@dataclass(frozen=True)
class Builder2QualificationReceiptProvenanceV1:
    """The non-secret provenance projection retained for one receipt."""

    qualification_receipt_id: str
    qualification_report_identity: str
    qualification_report_digest: str
    qualification_result_digest: str
    qualification_session_id: str
    controlled_shadow_run_id: str
    ceo_authorization_id: str
    fixture_key: str
    provider_identity: str
    provider_event_id: str
    provider_request_id: str
    observation_id: str
    observation_digest: str
    normalized_record_digest: str
    cascade_evidence_digest: str
    capture_attestation_digest: str
    adapter_version: str
    adapter_source_sha: str
    qualification_status: str
    receipt_digest: str = ""
    input_occurrence_count: int = 1

    @classmethod
    def from_receipt(
        cls, receipt: Builder2QualificationReceiptV1
    ) -> Builder2QualificationReceiptProvenanceV1:
        return cls(
            qualification_receipt_id=receipt.qualification_receipt_id,
            qualification_report_identity=receipt.qualification_report_identity,
            qualification_report_digest=receipt.qualification_report_digest,
            qualification_result_digest=receipt.qualification_result_digest,
            qualification_session_id=receipt.qualification_session_id,
            controlled_shadow_run_id=receipt.controlled_shadow_run_id,
            ceo_authorization_id=receipt.ceo_authorization_id,
            fixture_key=receipt.fixture_key,
            provider_identity=receipt.provider_identity,
            provider_event_id=receipt.provider_event_id,
            provider_request_id=receipt.provider_request_id,
            observation_id=receipt.observation_id,
            observation_digest=receipt.observation_digest,
            normalized_record_digest=receipt.normalized_record_digest,
            cascade_evidence_digest=receipt.cascade_evidence_digest,
            capture_attestation_digest=receipt.capture_attestation_digest,
            adapter_version=receipt.adapter_version,
            adapter_source_sha=receipt.adapter_source_sha,
            qualification_status=(
                receipt.qualification_status.value
                if hasattr(receipt.qualification_status, "value")
                else str(receipt.qualification_status)
            ),
            receipt_digest=receipt.receipt_digest,
            input_occurrence_count=1,
        )

    def validate(self) -> None:
        for name, value in (
            ("qualification_receipt_id", self.qualification_receipt_id),
            ("qualification_report_identity", self.qualification_report_identity),
            ("qualification_session_id", self.qualification_session_id),
            ("controlled_shadow_run_id", self.controlled_shadow_run_id),
            ("ceo_authorization_id", self.ceo_authorization_id),
            ("fixture_key", self.fixture_key),
            ("provider_identity", self.provider_identity),
            ("provider_event_id", self.provider_event_id),
            ("provider_request_id", self.provider_request_id),
            ("observation_id", self.observation_id),
            ("adapter_version", self.adapter_version),
            ("qualification_status", self.qualification_status),
        ):
            _text(value, name)
        for name, value in (
            ("qualification_report_digest", self.qualification_report_digest),
            ("qualification_result_digest", self.qualification_result_digest),
            ("observation_digest", self.observation_digest),
            ("normalized_record_digest", self.normalized_record_digest),
            ("cascade_evidence_digest", self.cascade_evidence_digest),
            ("capture_attestation_digest", self.capture_attestation_digest),
            ("adapter_source_sha", self.adapter_source_sha),
            ("receipt_digest", self.receipt_digest),
        ):
            _sha(value, name)
        if (
            not isinstance(self.input_occurrence_count, int)
            or isinstance(self.input_occurrence_count, bool)
            or self.input_occurrence_count <= 0
        ):
            raise Builder2QualificationSampleAggregatorError(
                "input_occurrence_count must be a positive integer"
            )

    def as_payload(self) -> dict[str, object]:
        self.validate()
        return {
            "qualification_receipt_id": self.qualification_receipt_id,
            "qualification_report_identity": self.qualification_report_identity,
            "qualification_report_digest": self.qualification_report_digest,
            "qualification_result_digest": self.qualification_result_digest,
            "qualification_session_id": self.qualification_session_id,
            "controlled_shadow_run_id": self.controlled_shadow_run_id,
            "ceo_authorization_id": self.ceo_authorization_id,
            "fixture_key": self.fixture_key,
            "provider_identity": self.provider_identity,
            "provider_event_id": self.provider_event_id,
            "provider_request_id": self.provider_request_id,
            "observation_id": self.observation_id,
            "observation_digest": self.observation_digest,
            "normalized_record_digest": self.normalized_record_digest,
            "cascade_evidence_digest": self.cascade_evidence_digest,
            "capture_attestation_digest": self.capture_attestation_digest,
            "adapter_version": self.adapter_version,
            "adapter_source_sha": self.adapter_source_sha,
            "qualification_status": self.qualification_status,
            "receipt_digest": self.receipt_digest,
            "input_occurrence_count": self.input_occurrence_count,
        }

    @classmethod
    def from_payload(cls, payload: object) -> Builder2QualificationReceiptProvenanceV1:
        raw = payload if isinstance(payload, Mapping) else {}
        return cls(
            qualification_receipt_id=raw.get("qualification_receipt_id", ""),
            qualification_report_identity=raw.get("qualification_report_identity", ""),
            qualification_report_digest=raw.get("qualification_report_digest", ""),
            qualification_result_digest=raw.get("qualification_result_digest", ""),
            qualification_session_id=raw.get("qualification_session_id", ""),
            controlled_shadow_run_id=raw.get("controlled_shadow_run_id", ""),
            ceo_authorization_id=raw.get("ceo_authorization_id", ""),
            fixture_key=raw.get("fixture_key", ""),
            provider_identity=raw.get("provider_identity", ""),
            provider_event_id=raw.get("provider_event_id", ""),
            provider_request_id=raw.get("provider_request_id", ""),
            observation_id=raw.get("observation_id", ""),
            observation_digest=raw.get("observation_digest", ""),
            normalized_record_digest=raw.get("normalized_record_digest", ""),
            cascade_evidence_digest=raw.get("cascade_evidence_digest", ""),
            capture_attestation_digest=raw.get("capture_attestation_digest", ""),
            adapter_version=raw.get("adapter_version", ""),
            adapter_source_sha=raw.get("adapter_source_sha", ""),
            qualification_status=raw.get("qualification_status", ""),
            receipt_digest=raw.get("receipt_digest", ""),
            input_occurrence_count=raw.get("input_occurrence_count", 1),
        )


@dataclass(frozen=True)
class Builder2FixtureProviderConflictV1:
    """Conflicting identities for one fixture/provider pair."""

    fixture_key: str
    provider_identity: str
    reason: str
    provider_event_ids: tuple[str, ...]
    provider_request_ids: tuple[str, ...]
    observation_ids: tuple[str, ...]

    def validate(self) -> None:
        for name, value in (
            ("fixture_key", self.fixture_key),
            ("provider_identity", self.provider_identity),
            ("reason", self.reason),
        ):
            _text(value, name)
        for name, values in (
            ("provider_event_ids", self.provider_event_ids),
            ("provider_request_ids", self.provider_request_ids),
            ("observation_ids", self.observation_ids),
        ):
            if tuple(sorted(set(values))) != values:
                raise Builder2QualificationSampleAggregatorError(
                    f"{name} must be sorted and unique"
                )
            for value in values:
                _text(value, name)

    def as_payload(self) -> dict[str, object]:
        self.validate()
        return {
            "fixture_key": self.fixture_key,
            "provider_identity": self.provider_identity,
            "reason": self.reason,
            "provider_event_ids": list(self.provider_event_ids),
            "provider_request_ids": list(self.provider_request_ids),
            "observation_ids": list(self.observation_ids),
        }

    @classmethod
    def from_payload(cls, payload: object) -> Builder2FixtureProviderConflictV1:
        raw = payload if isinstance(payload, Mapping) else {}
        return cls(
            fixture_key=raw.get("fixture_key", ""),
            provider_identity=raw.get("provider_identity", ""),
            reason=raw.get("reason", ""),
            provider_event_ids=tuple(raw.get("provider_event_ids", ())),
            provider_request_ids=tuple(raw.get("provider_request_ids", ())),
            observation_ids=tuple(raw.get("observation_ids", ())),
        )


@dataclass(frozen=True)
class Builder2EvidenceAvailabilityV1:
    """Explicitly reports whether Receipt V1 supports a metric."""

    metric: str
    supported: bool
    reason: str
    observed_count: int | None = None
    denominator: int | None = None
    rate: float | None = None

    def validate(self) -> None:
        _text(self.metric, "metric")
        _text(self.reason, "reason")
        if not isinstance(self.supported, bool):
            raise Builder2QualificationSampleAggregatorError(
                "evidence availability supported must be boolean"
            )
        for name, value in (
            ("observed_count", self.observed_count),
            ("denominator", self.denominator),
        ):
            if value is not None and (
                not isinstance(value, int) or isinstance(value, bool) or value < 0
            ):
                raise Builder2QualificationSampleAggregatorError(
                    f"{name} must be a non-negative integer or null"
                )
        if self.rate is not None and not 0.0 <= self.rate <= 1.0:
            raise Builder2QualificationSampleAggregatorError(
                "evidence availability rate must be between zero and one"
            )
        if not self.supported and any(
            value is not None
            for value in (self.observed_count, self.denominator, self.rate)
        ):
            raise Builder2QualificationSampleAggregatorError(
                "unsupported evidence cannot claim observed counts or a rate"
            )
        if self.supported and (
            self.denominator is None
            or self.denominator <= 0
            or self.observed_count is None
            or self.rate is None
        ):
            raise Builder2QualificationSampleAggregatorError(
                "supported evidence requires an observed count, denominator, and rate"
            )

    def as_payload(self) -> dict[str, object]:
        self.validate()
        return {
            "metric": self.metric,
            "supported": self.supported,
            "reason": self.reason,
            "observed_count": self.observed_count,
            "denominator": self.denominator,
            "rate": self.rate,
        }

    @classmethod
    def from_payload(cls, payload: object) -> Builder2EvidenceAvailabilityV1:
        raw = payload if isinstance(payload, Mapping) else {}
        return cls(
            metric=raw.get("metric", ""),
            supported=raw.get("supported"),
            reason=raw.get("reason", ""),
            observed_count=raw.get("observed_count"),
            denominator=raw.get("denominator"),
            rate=raw.get("rate"),
        )


@dataclass(frozen=True)
class _DerivedProvenanceState:
    """Re-derived report state; never populated from report counters."""

    input_receipt_count: int
    total_valid_receipts: int
    distinct_observation_count: int
    distinct_fixture_count: int
    per_league_counts: dict[str, int]
    per_provider_counts: dict[str, int]
    controlled_shadow_run_ids: tuple[str, ...]
    qualification_session_ids: tuple[str, ...]
    ceo_authorization_ids: tuple[str, ...]
    duplicate_receipt_ids: tuple[str, ...]
    divergent_receipt_ids: tuple[str, ...]
    duplicate_observation_ids: tuple[str, ...]
    duplicate_observation_digests: tuple[str, ...]
    observation_identity_conflict_ids: tuple[str, ...]
    observation_identity_conflict_digests: tuple[str, ...]
    fixture_provider_conflicts: tuple[Builder2FixtureProviderConflictV1, ...]
    unattributed_fixture_keys: tuple[str, ...]
    failure_taxonomy: dict[str, int]
    eligible_receipt_count: int
    eligible_distinct_observation_count: int
    eligible_distinct_fixture_count: int
    eligible_per_league_counts: dict[str, int]
    eligible_per_provider_counts: dict[str, int]


def _derive_provenance_state(
    provenance: tuple[Builder2QualificationReceiptProvenanceV1, ...],
) -> _DerivedProvenanceState:
    """Derive every count and diagnostic from retained provenance rows."""

    by_receipt_id: dict[str, list[Builder2QualificationReceiptProvenanceV1]] = (
        defaultdict(list)
    )
    variant_keys: set[tuple[str, str]] = set()
    for item in provenance:
        variant_key = (item.qualification_receipt_id, item.receipt_digest)
        if variant_key in variant_keys:
            raise Builder2QualificationSampleAggregatorError(
                "receipt provenance must contain one row per receipt ID/digest variant"
            )
        variant_keys.add(variant_key)
        by_receipt_id[item.qualification_receipt_id].append(item)

    duplicate_receipt_ids = tuple(
        sorted(
            receipt_id
            for receipt_id, items in by_receipt_id.items()
            if sum(item.input_occurrence_count for item in items) > 1
        )
    )
    divergent_receipt_ids = tuple(
        sorted(
            receipt_id
            for receipt_id, items in by_receipt_id.items()
            if len({item.receipt_digest for item in items}) > 1
        )
    )

    observation_id_digests: dict[str, set[str]] = defaultdict(set)
    observation_digest_ids: dict[str, set[str]] = defaultdict(set)
    fixture_provider_rows: dict[
        tuple[str, str], list[Builder2QualificationReceiptProvenanceV1]
    ] = defaultdict(list)
    per_league_counts: dict[str, int] = defaultdict(int)
    per_provider_counts: dict[str, int] = defaultdict(int)
    fixture_keys: set[str] = set()
    unattributed_fixture_keys: set[str] = set()

    for item in provenance:
        observation_id_digests[item.observation_id].add(item.observation_digest)
        observation_digest_ids[item.observation_digest].add(item.observation_id)
        fixture_provider_rows[(item.fixture_key, item.provider_identity)].append(item)
        fixture_keys.add(item.fixture_key)
        per_provider_counts[item.provider_identity] += 1
        league = _fixture_league(item.fixture_key)
        if league is None:
            unattributed_fixture_keys.add(item.fixture_key)
        else:
            per_league_counts[league] += 1

    duplicate_observation_ids = tuple(
        sorted(
            observation_id
            for observation_id, digests in observation_id_digests.items()
            if len(digests) > 0
            and sum(1 for item in provenance if item.observation_id == observation_id)
            > 1
        )
    )
    duplicate_observation_digests = tuple(
        sorted(
            observation_digest
            for observation_digest, observation_ids in observation_digest_ids.items()
            if len(observation_ids) > 0
            and sum(
                1
                for item in provenance
                if item.observation_digest == observation_digest
            )
            > 1
        )
    )
    observation_identity_conflict_ids = tuple(
        sorted(
            observation_id
            for observation_id, digests in observation_id_digests.items()
            if len(digests) > 1
        )
    )
    observation_identity_conflict_digests = tuple(
        sorted(
            observation_digest
            for observation_digest, observation_ids in observation_digest_ids.items()
            if len(observation_ids) > 1
        )
    )

    conflicts: list[Builder2FixtureProviderConflictV1] = []
    conflicting_fixture_provider_keys: set[tuple[str, str]] = set()
    for (fixture_key, provider_identity), items in fixture_provider_rows.items():
        event_ids = tuple(sorted({item.provider_event_id for item in items}))
        request_ids = tuple(sorted({item.provider_request_id for item in items}))
        if len(event_ids) <= 1 and len(request_ids) <= 1:
            continue
        conflicting_fixture_provider_keys.add((fixture_key, provider_identity))
        conflicts.append(
            Builder2FixtureProviderConflictV1(
                fixture_key=fixture_key,
                provider_identity=provider_identity,
                reason=(
                    "one fixture/provider pair has multiple provider event or "
                    "request identities"
                ),
                provider_event_ids=event_ids,
                provider_request_ids=request_ids,
                observation_ids=tuple(sorted({item.observation_id for item in items})),
            )
        )
    fixture_provider_conflicts = tuple(
        sorted(
            conflicts,
            key=lambda item: (item.fixture_key, item.provider_identity, item.reason),
        )
    )

    conflicting_observation_ids = set(observation_identity_conflict_ids)
    conflicting_observation_digests = set(observation_identity_conflict_digests)
    candidate_rows = tuple(
        item
        for item in provenance
        if item.qualification_receipt_id not in divergent_receipt_ids
        and item.observation_id not in conflicting_observation_ids
        and item.observation_digest not in conflicting_observation_digests
        and (item.fixture_key, item.provider_identity)
        not in conflicting_fixture_provider_keys
        and item.fixture_key not in unattributed_fixture_keys
    )
    observation_fixture_keys: dict[tuple[str, str], set[str]] = defaultdict(set)
    for item in candidate_rows:
        observation_fixture_keys[(item.observation_id, item.observation_digest)].add(
            item.fixture_key
        )
    stable_observation_pairs = {
        observation_pair
        for observation_pair, fixtures in observation_fixture_keys.items()
        if len(fixtures) == 1
    }
    # A duplicate observation with contradictory fixture attribution cannot
    # safely add either fixture.  A repeated observation on one fixture still
    # contributes that observation and fixture exactly once.
    eligible_rows = tuple(
        item
        for item in candidate_rows
        if (item.observation_id, item.observation_digest) in stable_observation_pairs
    )
    eligible_observations = stable_observation_pairs
    eligible_fixtures = {
        next(iter(fixtures))
        for observation_pair, fixtures in observation_fixture_keys.items()
        if observation_pair in stable_observation_pairs
    }
    eligible_per_league_counts: dict[str, int] = defaultdict(int)
    eligible_per_provider_counts: dict[str, int] = defaultdict(int)
    eligible_provider_pairs = {
        (
            item.observation_id,
            item.observation_digest,
            item.provider_identity,
        )
        for item in eligible_rows
    }
    for _, _, provider_identity in eligible_provider_pairs:
        eligible_per_provider_counts[provider_identity] += 1
    eligible_league_pairs = {
        (
            item.observation_id,
            item.observation_digest,
            item.fixture_key,
        )
        for item in eligible_rows
    }
    for _, _, fixture_key in eligible_league_pairs:
        league = _fixture_league(fixture_key)
        if league is not None:
            eligible_per_league_counts[league] += 1

    failure_taxonomy: dict[str, int] = {}
    if duplicate_receipt_ids:
        failure_taxonomy[FAILURE_DUPLICATE_RECEIPT] = len(duplicate_receipt_ids)
    if divergent_receipt_ids:
        failure_taxonomy[FAILURE_DIVERGENT_RECEIPT_ID_CONFLICT] = len(
            divergent_receipt_ids
        )
    duplicate_observation_signals = len(duplicate_observation_ids) + len(
        duplicate_observation_digests
    )
    if duplicate_observation_signals:
        failure_taxonomy[FAILURE_DUPLICATE_OBSERVATION] = duplicate_observation_signals
    if observation_identity_conflict_ids:
        failure_taxonomy[FAILURE_OBSERVATION_ID_CONFLICT] = len(
            observation_identity_conflict_ids
        )
    if observation_identity_conflict_digests:
        failure_taxonomy[FAILURE_OBSERVATION_DIGEST_CONFLICT] = len(
            observation_identity_conflict_digests
        )
    if fixture_provider_conflicts:
        failure_taxonomy[FAILURE_FIXTURE_PROVIDER_CONFLICT] = len(
            fixture_provider_conflicts
        )
    if unattributed_fixture_keys:
        failure_taxonomy[FAILURE_UNATTRIBUTED_LEAGUE] = len(unattributed_fixture_keys)

    return _DerivedProvenanceState(
        input_receipt_count=sum(item.input_occurrence_count for item in provenance),
        total_valid_receipts=len(provenance),
        distinct_observation_count=len(
            {(item.observation_id, item.observation_digest) for item in provenance}
        ),
        distinct_fixture_count=len(fixture_keys),
        per_league_counts=dict(sorted(per_league_counts.items())),
        per_provider_counts=dict(sorted(per_provider_counts.items())),
        controlled_shadow_run_ids=tuple(
            sorted({item.controlled_shadow_run_id for item in provenance})
        ),
        qualification_session_ids=tuple(
            sorted({item.qualification_session_id for item in provenance})
        ),
        ceo_authorization_ids=tuple(
            sorted({item.ceo_authorization_id for item in provenance})
        ),
        duplicate_receipt_ids=duplicate_receipt_ids,
        divergent_receipt_ids=divergent_receipt_ids,
        duplicate_observation_ids=duplicate_observation_ids,
        duplicate_observation_digests=duplicate_observation_digests,
        observation_identity_conflict_ids=observation_identity_conflict_ids,
        observation_identity_conflict_digests=observation_identity_conflict_digests,
        fixture_provider_conflicts=fixture_provider_conflicts,
        unattributed_fixture_keys=tuple(sorted(unattributed_fixture_keys)),
        failure_taxonomy=dict(sorted(failure_taxonomy.items())),
        eligible_receipt_count=len(eligible_observations),
        eligible_distinct_observation_count=len(eligible_observations),
        eligible_distinct_fixture_count=len(eligible_fixtures),
        eligible_per_league_counts=dict(sorted(eligible_per_league_counts.items())),
        eligible_per_provider_counts=dict(sorted(eligible_per_provider_counts.items())),
    )


@dataclass(frozen=True)
class Builder2QualificationSampleReportV1:
    """Deterministic receipt sample report with no production authority."""

    schema_version: str
    input_receipt_count: int
    total_valid_receipts: int
    distinct_observation_count: int
    distinct_fixture_count: int
    per_league_counts: Mapping[str, int]
    per_provider_counts: Mapping[str, int]
    controlled_shadow_run_ids: tuple[str, ...]
    qualification_session_ids: tuple[str, ...]
    ceo_authorization_ids: tuple[str, ...]
    duplicate_receipt_ids: tuple[str, ...]
    duplicate_observation_ids: tuple[str, ...]
    duplicate_observation_digests: tuple[str, ...]
    fixture_provider_conflicts: tuple[Builder2FixtureProviderConflictV1, ...]
    unattributed_fixture_keys: tuple[str, ...]
    receipt_provenance: tuple[Builder2QualificationReceiptProvenanceV1, ...]
    failure_taxonomy: Mapping[str, int]
    freshness: Builder2EvidenceAvailabilityV1
    observation_coverage: Builder2EvidenceAvailabilityV1
    minimum_sample_policy: MinimumSamplePolicy | None = None
    sample_sufficient: bool | None = None
    no_bet: bool = True
    publication: bool = False
    production_activation_authorized: bool = False
    report_digest: str = ""
    eligible_receipt_count: int = 0
    eligible_distinct_observation_count: int = 0
    eligible_distinct_fixture_count: int = 0
    eligible_per_league_counts: Mapping[str, int] = field(default_factory=dict)
    eligible_per_provider_counts: Mapping[str, int] = field(default_factory=dict)
    divergent_receipt_ids: tuple[str, ...] = ()
    observation_identity_conflict_ids: tuple[str, ...] = ()
    observation_identity_conflict_digests: tuple[str, ...] = ()

    def _payload(self, *, include_report_digest: bool) -> dict[str, object]:
        payload: dict[str, object] = {
            "schema_version": self.schema_version,
            "input_receipt_count": self.input_receipt_count,
            "total_valid_receipts": self.total_valid_receipts,
            "distinct_observation_count": self.distinct_observation_count,
            "distinct_fixture_count": self.distinct_fixture_count,
            "per_league_counts": dict(sorted(self.per_league_counts.items())),
            "per_provider_counts": dict(sorted(self.per_provider_counts.items())),
            "controlled_shadow_run_ids": list(self.controlled_shadow_run_ids),
            "qualification_session_ids": list(self.qualification_session_ids),
            "ceo_authorization_ids": list(self.ceo_authorization_ids),
            "duplicate_receipt_ids": list(self.duplicate_receipt_ids),
            "duplicate_observation_ids": list(self.duplicate_observation_ids),
            "duplicate_observation_digests": list(self.duplicate_observation_digests),
            "fixture_provider_conflicts": [
                item.as_payload() for item in self.fixture_provider_conflicts
            ],
            "unattributed_fixture_keys": list(self.unattributed_fixture_keys),
            "receipt_provenance": [
                item.as_payload() for item in self.receipt_provenance
            ],
            "failure_taxonomy": dict(sorted(self.failure_taxonomy.items())),
            "freshness": self.freshness.as_payload(),
            "observation_coverage": self.observation_coverage.as_payload(),
            "minimum_sample_policy": (
                self.minimum_sample_policy.as_payload()
                if self.minimum_sample_policy is not None
                else None
            ),
            "sample_sufficient": self.sample_sufficient,
            "no_bet": self.no_bet,
            "publication": self.publication,
            "production_activation_authorized": self.production_activation_authorized,
            "eligible_receipt_count": self.eligible_receipt_count,
            "eligible_distinct_observation_count": self.eligible_distinct_observation_count,
            "eligible_distinct_fixture_count": self.eligible_distinct_fixture_count,
            "eligible_per_league_counts": dict(
                sorted(self.eligible_per_league_counts.items())
            ),
            "eligible_per_provider_counts": dict(
                sorted(self.eligible_per_provider_counts.items())
            ),
            "divergent_receipt_ids": list(self.divergent_receipt_ids),
            "observation_identity_conflict_ids": list(
                self.observation_identity_conflict_ids
            ),
            "observation_identity_conflict_digests": list(
                self.observation_identity_conflict_digests
            ),
        }
        if include_report_digest:
            payload["report_digest"] = self.report_digest
        return payload

    @classmethod
    def from_payload(cls, payload: object) -> Builder2QualificationSampleReportV1:
        raw = payload if isinstance(payload, Mapping) else {}
        policy_raw = raw.get("minimum_sample_policy")
        policy = None
        if isinstance(policy_raw, Mapping):
            policy = MinimumSamplePolicy(
                policy_raw.get("minimum_real_observations"),
                policy_raw.get("minimum_distinct_fixtures", 1),
            )
        conflicts = tuple(
            Builder2FixtureProviderConflictV1.from_payload(item)
            for item in raw.get("fixture_provider_conflicts", ())
        )
        provenance = tuple(
            Builder2QualificationReceiptProvenanceV1.from_payload(item)
            for item in raw.get("receipt_provenance", ())
        )
        return cls(
            schema_version=raw.get("schema_version", ""),
            input_receipt_count=raw.get("input_receipt_count", -1),
            total_valid_receipts=raw.get("total_valid_receipts", -1),
            distinct_observation_count=raw.get("distinct_observation_count", -1),
            distinct_fixture_count=raw.get("distinct_fixture_count", -1),
            per_league_counts=(
                raw.get("per_league_counts", {})
                if isinstance(raw.get("per_league_counts", {}), Mapping)
                else {}
            ),
            per_provider_counts=(
                raw.get("per_provider_counts", {})
                if isinstance(raw.get("per_provider_counts", {}), Mapping)
                else {}
            ),
            controlled_shadow_run_ids=tuple(raw.get("controlled_shadow_run_ids", ())),
            qualification_session_ids=tuple(raw.get("qualification_session_ids", ())),
            ceo_authorization_ids=tuple(raw.get("ceo_authorization_ids", ())),
            duplicate_receipt_ids=tuple(raw.get("duplicate_receipt_ids", ())),
            duplicate_observation_ids=tuple(raw.get("duplicate_observation_ids", ())),
            duplicate_observation_digests=tuple(
                raw.get("duplicate_observation_digests", ())
            ),
            fixture_provider_conflicts=conflicts,
            unattributed_fixture_keys=tuple(raw.get("unattributed_fixture_keys", ())),
            receipt_provenance=provenance,
            failure_taxonomy=(
                raw.get("failure_taxonomy", {})
                if isinstance(raw.get("failure_taxonomy", {}), Mapping)
                else {}
            ),
            freshness=Builder2EvidenceAvailabilityV1.from_payload(
                raw.get("freshness", {})
            ),
            observation_coverage=Builder2EvidenceAvailabilityV1.from_payload(
                raw.get("observation_coverage", {})
            ),
            minimum_sample_policy=policy,
            sample_sufficient=raw.get("sample_sufficient"),
            no_bet=raw.get("no_bet"),
            publication=raw.get("publication"),
            production_activation_authorized=raw.get(
                "production_activation_authorized"
            ),
            report_digest=raw.get("report_digest", ""),
            eligible_receipt_count=raw.get("eligible_receipt_count", -1),
            eligible_distinct_observation_count=raw.get(
                "eligible_distinct_observation_count", -1
            ),
            eligible_distinct_fixture_count=raw.get(
                "eligible_distinct_fixture_count", -1
            ),
            eligible_per_league_counts=(
                raw.get("eligible_per_league_counts", {})
                if isinstance(raw.get("eligible_per_league_counts", {}), Mapping)
                else {}
            ),
            eligible_per_provider_counts=(
                raw.get("eligible_per_provider_counts", {})
                if isinstance(raw.get("eligible_per_provider_counts", {}), Mapping)
                else {}
            ),
            divergent_receipt_ids=tuple(raw.get("divergent_receipt_ids", ())),
            observation_identity_conflict_ids=tuple(
                raw.get("observation_identity_conflict_ids", ())
            ),
            observation_identity_conflict_digests=tuple(
                raw.get("observation_identity_conflict_digests", ())
            ),
        )

    def validate(self) -> None:
        if self.schema_version != QUALIFICATION_SAMPLE_AGGREGATOR_SCHEMA_VERSION:
            raise Builder2QualificationSampleAggregatorError(
                "unsupported qualification sample report schema"
            )
        for name, value in (
            ("input_receipt_count", self.input_receipt_count),
            ("total_valid_receipts", self.total_valid_receipts),
            ("distinct_observation_count", self.distinct_observation_count),
            ("distinct_fixture_count", self.distinct_fixture_count),
            ("eligible_receipt_count", self.eligible_receipt_count),
            (
                "eligible_distinct_observation_count",
                self.eligible_distinct_observation_count,
            ),
            ("eligible_distinct_fixture_count", self.eligible_distinct_fixture_count),
        ):
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise Builder2QualificationSampleAggregatorError(
                    f"{name} must be a non-negative integer"
                )
        if self.total_valid_receipts > self.input_receipt_count:
            raise Builder2QualificationSampleAggregatorError(
                "valid receipts cannot exceed input receipts"
            )
        if self.eligible_receipt_count > self.total_valid_receipts:
            raise Builder2QualificationSampleAggregatorError(
                "eligible receipts cannot exceed valid receipt variants"
            )
        if self.eligible_distinct_observation_count > self.distinct_observation_count:
            raise Builder2QualificationSampleAggregatorError(
                "eligible observations cannot exceed raw observations"
            )
        if self.eligible_distinct_fixture_count > self.distinct_fixture_count:
            raise Builder2QualificationSampleAggregatorError(
                "eligible fixtures cannot exceed raw fixtures"
            )
        _count_map(self.per_league_counts, "per_league_counts")
        if any(key not in TOP5_LEAGUES for key in self.per_league_counts):
            raise Builder2QualificationSampleAggregatorError(
                "per-league counts contain an unsupported league"
            )
        _count_map(self.per_provider_counts, "per_provider_counts")
        _count_map(self.eligible_per_league_counts, "eligible_per_league_counts")
        if any(key not in TOP5_LEAGUES for key in self.eligible_per_league_counts):
            raise Builder2QualificationSampleAggregatorError(
                "eligible per-league counts contain an unsupported league"
            )
        _count_map(self.eligible_per_provider_counts, "eligible_per_provider_counts")
        _count_map(self.failure_taxonomy, "failure_taxonomy")
        for name, values in (
            ("controlled_shadow_run_ids", self.controlled_shadow_run_ids),
            ("qualification_session_ids", self.qualification_session_ids),
            ("ceo_authorization_ids", self.ceo_authorization_ids),
            ("duplicate_receipt_ids", self.duplicate_receipt_ids),
            ("divergent_receipt_ids", self.divergent_receipt_ids),
            ("duplicate_observation_ids", self.duplicate_observation_ids),
            ("duplicate_observation_digests", self.duplicate_observation_digests),
            (
                "observation_identity_conflict_ids",
                self.observation_identity_conflict_ids,
            ),
            (
                "observation_identity_conflict_digests",
                self.observation_identity_conflict_digests,
            ),
            ("unattributed_fixture_keys", self.unattributed_fixture_keys),
        ):
            if tuple(sorted(set(values))) != values:
                raise Builder2QualificationSampleAggregatorError(
                    f"{name} must be sorted and unique"
                )
            for value in values:
                _text(value, name)
        for item in self.receipt_provenance:
            item.validate()
        if (
            tuple(
                sorted(
                    self.receipt_provenance,
                    key=lambda item: (
                        item.qualification_receipt_id,
                        item.receipt_digest,
                    ),
                )
            )
            != self.receipt_provenance
        ):
            raise Builder2QualificationSampleAggregatorError(
                "receipt provenance must be sorted by receipt ID and digest"
            )
        for item in self.fixture_provider_conflicts:
            item.validate()
        if (
            tuple(
                sorted(
                    self.fixture_provider_conflicts,
                    key=lambda item: (
                        item.fixture_key,
                        item.provider_identity,
                        item.reason,
                    ),
                )
            )
            != self.fixture_provider_conflicts
        ):
            raise Builder2QualificationSampleAggregatorError(
                "fixture/provider conflicts must be deterministically ordered"
            )
        state = _derive_provenance_state(self.receipt_provenance)
        expected_fields = (
            ("input_receipt_count", state.input_receipt_count),
            ("total_valid_receipts", state.total_valid_receipts),
            ("distinct_observation_count", state.distinct_observation_count),
            ("distinct_fixture_count", state.distinct_fixture_count),
            ("per_league_counts", state.per_league_counts),
            ("per_provider_counts", state.per_provider_counts),
            ("controlled_shadow_run_ids", state.controlled_shadow_run_ids),
            ("qualification_session_ids", state.qualification_session_ids),
            ("ceo_authorization_ids", state.ceo_authorization_ids),
            ("duplicate_receipt_ids", state.duplicate_receipt_ids),
            ("divergent_receipt_ids", state.divergent_receipt_ids),
            ("duplicate_observation_ids", state.duplicate_observation_ids),
            ("duplicate_observation_digests", state.duplicate_observation_digests),
            (
                "observation_identity_conflict_ids",
                state.observation_identity_conflict_ids,
            ),
            (
                "observation_identity_conflict_digests",
                state.observation_identity_conflict_digests,
            ),
            ("fixture_provider_conflicts", state.fixture_provider_conflicts),
            ("unattributed_fixture_keys", state.unattributed_fixture_keys),
            ("failure_taxonomy", state.failure_taxonomy),
            ("eligible_receipt_count", state.eligible_receipt_count),
            (
                "eligible_distinct_observation_count",
                state.eligible_distinct_observation_count,
            ),
            ("eligible_distinct_fixture_count", state.eligible_distinct_fixture_count),
            ("eligible_per_league_counts", state.eligible_per_league_counts),
            ("eligible_per_provider_counts", state.eligible_per_provider_counts),
        )
        for name, expected in expected_fields:
            actual = getattr(self, name)
            if actual != expected:
                raise Builder2QualificationSampleAggregatorError(
                    f"{name} does not match retained receipt provenance"
                )
        self.freshness.validate()
        self.observation_coverage.validate()
        if self.freshness != _freshness_unavailable():
            raise Builder2QualificationSampleAggregatorError(
                "freshness must remain explicitly unsupported by Receipt V1"
            )
        if self.observation_coverage != _coverage_unavailable():
            raise Builder2QualificationSampleAggregatorError(
                "observation coverage must remain explicitly unsupported by Receipt V1"
            )
        if self.minimum_sample_policy is not None:
            if not isinstance(self.minimum_sample_policy, MinimumSamplePolicy):
                raise Builder2QualificationSampleAggregatorError(
                    "minimum_sample_policy must be the caller-supplied canonical policy"
                )
            self.minimum_sample_policy.validate()
        if self.sample_sufficient is not None:
            if not isinstance(self.sample_sufficient, bool):
                raise Builder2QualificationSampleAggregatorError(
                    "sample_sufficient must be boolean or null"
                )
            if self.minimum_sample_policy is None:
                raise Builder2QualificationSampleAggregatorError(
                    "sample sufficiency requires a caller-supplied policy"
                )
            expected_sufficiency = (
                self.eligible_distinct_observation_count
                >= self.minimum_sample_policy.minimum_real_observations
                and self.eligible_distinct_fixture_count
                >= self.minimum_sample_policy.minimum_distinct_fixtures
            )
            if self.sample_sufficient is not expected_sufficiency:
                raise Builder2QualificationSampleAggregatorError(
                    "sample sufficiency does not match the caller policy"
                )
        if self.no_bet is not True:
            raise Builder2QualificationSampleAggregatorError(
                "sample reports must remain NO-BET"
            )
        if self.publication is not False:
            raise Builder2QualificationSampleAggregatorError(
                "sample reports must remain unpublished"
            )
        if self.production_activation_authorized is not False:
            raise Builder2QualificationSampleAggregatorError(
                "sample sufficiency cannot authorize production activation"
            )
        expected_digest = semantic_digest(self._payload(include_report_digest=False))
        if self.report_digest.lower() != expected_digest:
            raise Builder2QualificationSampleAggregatorError(
                "qualification sample report digest mismatch"
            )

    def as_payload(self) -> dict[str, object]:
        self.validate()
        return self._payload(include_report_digest=True)


ReceiptInput = Builder2QualificationReceiptV1 | Mapping[str, object]


def _validated_receipts(
    receipts: Iterable[ReceiptInput],
) -> tuple[Builder2QualificationReceiptV1, ...]:
    if isinstance(receipts, (str, bytes, Mapping, Builder2QualificationReceiptV1)):
        raise Builder2QualificationSampleAggregatorError(
            "aggregator input must be an iterable of canonical receipts"
        )
    try:
        iterator = iter(receipts)
    except TypeError as exc:
        raise Builder2QualificationSampleAggregatorError(
            "aggregator input must be iterable"
        ) from exc
    normalized: list[Builder2QualificationReceiptV1] = []
    for index, candidate in enumerate(iterator):
        try:
            if isinstance(candidate, Builder2QualificationReceiptV1):
                receipt = candidate
            elif isinstance(candidate, Mapping):
                receipt = Builder2QualificationReceiptV1.from_payload(candidate)
            else:
                raise Builder2QualificationSampleAggregatorError(
                    "only Builder2QualificationReceiptV1 receipts are accepted"
                )
            receipt.validate()
        except (
            Builder2QualificationReceiptError,
            Builder2QualificationSampleAggregatorError,
            QualificationContractError,
            TypeError,
            ValueError,
        ) as exc:
            raise Builder2QualificationSampleAggregatorError(
                f"invalid canonical receipt at index {index}: {exc}"
            ) from exc
        normalized.append(receipt)
    return tuple(normalized)


def _freshness_unavailable() -> Builder2EvidenceAvailabilityV1:
    return Builder2EvidenceAvailabilityV1(
        metric="freshness",
        supported=False,
        reason=(
            "Builder2QualificationReceiptV1 does not bind captured_at, "
            "source_timestamp, or source_age_seconds."
        ),
    )


def _coverage_unavailable() -> Builder2EvidenceAvailabilityV1:
    return Builder2EvidenceAvailabilityV1(
        metric="observation_coverage",
        supported=False,
        reason=(
            "Builder2QualificationReceiptV1 has observed fixture identity but "
            "no expected-fixture denominator; no coverage rate is inferred."
        ),
    )


def aggregate_builder2_qualification_samples(
    receipts: Iterable[ReceiptInput],
    *,
    minimum_sample_policy: MinimumSamplePolicy | None = None,
) -> Builder2QualificationSampleReportV1:
    """Aggregate validated receipts without creating any authority.

    Exact duplicate receipt IDs are collapsed for evidence counts.  Duplicate
    observations and fixture/provider identity conflicts remain visible in the
    report and never create additional sample evidence.  A policy is optional;
    when supplied, it is caller-owned and is evaluated only against the
    de-duplicated observation count and fixture count.
    """

    if minimum_sample_policy is not None:
        if not isinstance(minimum_sample_policy, MinimumSamplePolicy):
            raise Builder2QualificationSampleAggregatorError(
                "minimum_sample_policy must be the caller-supplied canonical policy"
            )
        minimum_sample_policy.validate()

    validated = _validated_receipts(receipts)
    by_variant: dict[tuple[str, str], list[Builder2QualificationReceiptV1]] = (
        defaultdict(list)
    )
    for receipt in validated:
        by_variant[(receipt.qualification_receipt_id, receipt.receipt_digest)].append(
            receipt
        )
    provenance: list[Builder2QualificationReceiptProvenanceV1] = []
    for _, items in sorted(by_variant.items()):
        # Identical receipt ID/digest rows are exact semantic duplicates.  The
        # digest is the canonical variant key, so they collapse without
        # selecting between divergent variants.
        canonical = min(items, key=lambda item: repr(item.as_payload()))
        provenance.append(
            replace(
                Builder2QualificationReceiptProvenanceV1.from_receipt(canonical),
                input_occurrence_count=len(items),
            )
        )
    retained_provenance = tuple(provenance)
    state = _derive_provenance_state(retained_provenance)
    sample_sufficient = (
        None
        if minimum_sample_policy is None
        else state.eligible_distinct_observation_count
        >= minimum_sample_policy.minimum_real_observations
        and state.eligible_distinct_fixture_count
        >= minimum_sample_policy.minimum_distinct_fixtures
    )
    report = Builder2QualificationSampleReportV1(
        schema_version=QUALIFICATION_SAMPLE_AGGREGATOR_SCHEMA_VERSION,
        input_receipt_count=state.input_receipt_count,
        total_valid_receipts=state.total_valid_receipts,
        distinct_observation_count=state.distinct_observation_count,
        distinct_fixture_count=state.distinct_fixture_count,
        per_league_counts=state.per_league_counts,
        per_provider_counts=state.per_provider_counts,
        controlled_shadow_run_ids=state.controlled_shadow_run_ids,
        qualification_session_ids=state.qualification_session_ids,
        ceo_authorization_ids=state.ceo_authorization_ids,
        duplicate_receipt_ids=state.duplicate_receipt_ids,
        duplicate_observation_ids=state.duplicate_observation_ids,
        duplicate_observation_digests=state.duplicate_observation_digests,
        fixture_provider_conflicts=state.fixture_provider_conflicts,
        unattributed_fixture_keys=state.unattributed_fixture_keys,
        receipt_provenance=retained_provenance,
        failure_taxonomy=state.failure_taxonomy,
        freshness=_freshness_unavailable(),
        observation_coverage=_coverage_unavailable(),
        minimum_sample_policy=minimum_sample_policy,
        sample_sufficient=sample_sufficient,
        eligible_receipt_count=state.eligible_receipt_count,
        eligible_distinct_observation_count=state.eligible_distinct_observation_count,
        eligible_distinct_fixture_count=state.eligible_distinct_fixture_count,
        eligible_per_league_counts=state.eligible_per_league_counts,
        eligible_per_provider_counts=state.eligible_per_provider_counts,
        divergent_receipt_ids=state.divergent_receipt_ids,
        observation_identity_conflict_ids=state.observation_identity_conflict_ids,
        observation_identity_conflict_digests=state.observation_identity_conflict_digests,
    )
    report = replace(
        report,
        report_digest=semantic_digest(report._payload(include_report_digest=False)),
    )
    report.validate()
    return report


class Builder2QualificationSampleAggregator:
    """Stateless facade for the receipt-only aggregation function."""

    @staticmethod
    def aggregate(
        receipts: Iterable[ReceiptInput],
        *,
        minimum_sample_policy: MinimumSamplePolicy | None = None,
    ) -> Builder2QualificationSampleReportV1:
        return aggregate_builder2_qualification_samples(
            receipts, minimum_sample_policy=minimum_sample_policy
        )


aggregate_qualification_samples = aggregate_builder2_qualification_samples
Builder2QualificationSampleAggregatorV1 = Builder2QualificationSampleAggregator
Builder2QualificationSampleReport = Builder2QualificationSampleReportV1

__all__ = [
    "BUILDER2_QUALIFICATION_SAMPLE_AGGREGATOR_CONTRACT_VERSION",
    "FAILURE_DIVERGENT_RECEIPT_ID_CONFLICT",
    "FAILURE_DUPLICATE_OBSERVATION",
    "FAILURE_DUPLICATE_RECEIPT",
    "FAILURE_FIXTURE_PROVIDER_CONFLICT",
    "FAILURE_OBSERVATION_DIGEST_CONFLICT",
    "FAILURE_OBSERVATION_ID_CONFLICT",
    "FAILURE_UNATTRIBUTED_LEAGUE",
    "QUALIFICATION_SAMPLE_AGGREGATOR_SCHEMA_VERSION",
    "SAMPLE_AGGREGATOR_SCHEMA_VERSION",
    "Builder2EvidenceAvailabilityV1",
    "Builder2FixtureProviderConflictV1",
    "Builder2QualificationReceiptProvenanceV1",
    "Builder2QualificationSampleAggregator",
    "Builder2QualificationSampleAggregatorError",
    "Builder2QualificationSampleAggregatorV1",
    "Builder2QualificationSampleReport",
    "Builder2QualificationSampleReportV1",
    "aggregate_builder2_qualification_samples",
    "aggregate_qualification_samples",
]
