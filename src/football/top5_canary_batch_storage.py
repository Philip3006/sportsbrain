"""Durable private Top-5 batch evidence and verified public rollback.

This store is downstream of the existing one-shot, activation and publication
guards. It accepts only a complete, authorized ``PublishedTop5BatchArtifact``;
it does not call providers or grant publication authority. Public output stays
the existing ``signals.json`` product and is emitted only as one complete
five-league generation.
"""

from __future__ import annotations

import base64
import fcntl
import hashlib
import json
import os
import stat
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Protocol

from src.football.production_contracts import Fixture, ProductionContractError
from src.football.top5_publisher import (
    TOP5_PUBLIC_RELEASE_LEAGUES,
    PublishedTop5BatchArtifact,
)
from src.football.top5_shadow_provider_redundancy import make_fixture_key
from src.football.top5_signal_lifecycle import (
    SignalLifecycleStage,
    canonical_top5_h2h_lifecycle_set,
    top5_h2h_lifecycle_set_digest,
)
from src.football.top5_signal_lifecycle_public_adapter import (
    project_top5_signal_lifecycles,
)
from src.notifications.public_serializer import serialize_public_product
from src.runtime.paths import runtime_state_path
from src.utils.atomic_io import atomic_write_json

TOP5_CANARY_BATCH_SCHEMA = "top5-canary-signal-batch-v1"
TOP5_CANARY_BATCH_STORE_SCHEMA = "top5-canary-batch-store-v1"
TOP5_CANARY_BATCH_STATE_DIR = "football/top5/canary_batches"
TOP5_CANARY_MAX_ODDS_AGE_SECONDS = 900
_LEAGUES = frozenset(TOP5_PUBLIC_RELEASE_LEAGUES)
_OUTCOMES = ("home", "draw", "away")
_PRIVATE_KEYS = frozenset(
    {
        "bankroll",
        "bankroll_state",
        "stake",
        "stake_eur",
        "stake_pct",
        "user",
        "user_id",
        "owner",
        "api_key",
        "api_token",
        "authorization",
        "authorization_nonce",
        "capability_nonce",
        "credential",
        "credentials",
        "secret",
    }
)


class Top5CanaryBatchState(str, Enum):
    PREPARING = "PREPARING"
    COMMITTED = "COMMITTED"
    FAILED = "FAILED"
    ROLLED_BACK = "ROLLED_BACK"


class Top5CanaryBatchStorageError(ProductionContractError):
    """A canary batch is incomplete, unsafe, corrupt, or not committed."""


def _canonical(value: object) -> object:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return {
            str(key): _canonical(child)
            for key, child in sorted(value.items(), key=lambda item: str(item[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_canonical(child) for child in value]
    return value


def _bytes(value: object) -> bytes:
    try:
        return json.dumps(
            _canonical(value),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise Top5CanaryBatchStorageError("batch value is not canonical JSON") from exc


def _digest(value: object) -> str:
    return hashlib.sha256(_bytes(value)).hexdigest()


def _parse_time(value: object, name: str) -> datetime:
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise Top5CanaryBatchStorageError(f"{name} must include a timezone")
        return value.astimezone(timezone.utc)
    if not isinstance(value, str) or not value:
        raise Top5CanaryBatchStorageError(f"{name} is required")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise Top5CanaryBatchStorageError(f"{name} is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise Top5CanaryBatchStorageError(f"{name} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _required_text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise Top5CanaryBatchStorageError(f"{name} is required")
    return value


def _probability_map(value: object) -> dict[str, float]:
    if not isinstance(value, Mapping) or set(value) != set(_OUTCOMES):
        raise Top5CanaryBatchStorageError("fixture model probabilities are incomplete")
    result: dict[str, float] = {}
    for outcome in _OUTCOMES:
        raw = value[outcome]
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise Top5CanaryBatchStorageError("fixture model probability is malformed")
        probability = float(raw)
        if not 0 <= probability <= 1:
            raise Top5CanaryBatchStorageError("fixture model probability is invalid")
        result[outcome] = probability
    if abs(sum(result.values()) - 1.0) > 1e-6:
        raise Top5CanaryBatchStorageError(
            "fixture model probabilities do not sum to one"
        )
    return result


def _assert_no_private_keys(value: object) -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if str(key).casefold() in _PRIVATE_KEYS:
                raise Top5CanaryBatchStorageError(
                    "canonical batch contains a prohibited private/financial field"
                )
            _assert_no_private_keys(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            _assert_no_private_keys(child)


def canonical_top5_canary_batch(
    artifact: PublishedTop5BatchArtifact,
) -> dict[str, object]:
    """Build a private, stake-free five-fixture record from validated inputs.

    Lifecycle windows and odds freshness are revalidated against the typed
    lifecycle objects. The confidence field is explicitly the highest model
    outcome probability, not a separate calibrated-confidence claim.
    """

    if not isinstance(artifact, PublishedTop5BatchArtifact):
        raise Top5CanaryBatchStorageError("a published Top-5 batch is required")
    try:
        artifact.validate()
        public = serialize_public_product(dict(artifact.public_product))
    except (AssertionError, TypeError, ValueError) as exc:
        raise Top5CanaryBatchStorageError("published Top-5 batch is invalid") from exc

    release = public.get("top5_release")
    if not isinstance(release, Mapping):
        raise Top5CanaryBatchStorageError("controlled Top-5 release is missing")
    if (
        release.get("activation_state") != "CONTROLLED"
        or release.get("batch_state") != "COMMITTED"
        or release.get("publication_status") != "PUBLISHED"
        or release.get("publication_enabled") is not True
        or release.get("no_bet") is not True
        or release.get("provider_authority") != "the_odds_api"
        or set(release.get("league_codes", ())) != _LEAGUES
        or len(release.get("league_codes", ())) != len(_LEAGUES)
    ):
        raise Top5CanaryBatchStorageError("release authorization/scope is invalid")
    batch_id = _required_text(release.get("generation_id"), "generation_id")
    activation_id = _required_text(release.get("activation_id"), "activation_id")
    publication_authorization_id = _required_text(
        release.get("publication_authorization_id"), "publication_authorization_id"
    )

    payload_by_league = {payload.league_code: payload for payload in artifact.payloads}
    if set(payload_by_league) != _LEAGUES or len(payload_by_league) != 5:
        raise Top5CanaryBatchStorageError("batch must bind the five canonical leagues")
    records: list[dict[str, object]] = []
    for league in sorted(_LEAGUES):
        payload = payload_by_league[league]
        if payload.provider_authority != "the_odds_api" or payload.no_bet is not True:
            raise Top5CanaryBatchStorageError(
                "fixture provider/no-bet binding is invalid"
            )
        if not payload.publication_enabled:
            raise Top5CanaryBatchStorageError("publication authorization is missing")
        if len(payload.football_records) != 1:
            raise Top5CanaryBatchStorageError(
                "each canonical league must contain exactly one canary fixture"
            )
        source = payload.football_records[0]
        if not isinstance(source, Mapping):
            raise Top5CanaryBatchStorageError("fixture source record is malformed")
        fixture_data = source.get("fixture")
        if not isinstance(fixture_data, Mapping):
            raise Top5CanaryBatchStorageError("canonical fixture identity is missing")
        fixture_key = _required_text(
            fixture_data.get("fixture_key") or source.get("fixture_key"), "fixture_key"
        )
        home = _required_text(fixture_data.get("home_team"), "home_team")
        away = _required_text(fixture_data.get("away_team"), "away_team")
        kickoff = _parse_time(fixture_data.get("kickoff"), "kickoff")
        fixture = Fixture(fixture_key, league, home, away, kickoff)
        fixture.validate()
        if make_fixture_key(league, home, away, kickoff) != fixture_key:
            raise Top5CanaryBatchStorageError("fixture identity is not canonical")

        probabilities = _probability_map(source.get("probabilities"))
        odds = source.get("odds")
        if not isinstance(odds, Mapping) or set(odds) != set(_OUTCOMES):
            raise Top5CanaryBatchStorageError("complete 1X2 odds are required")
        normalized_odds: dict[str, float] = {}
        for outcome, raw in odds.items():
            if isinstance(raw, bool) or not isinstance(raw, (int, float)):
                raise Top5CanaryBatchStorageError("fixture odds are malformed")
            price = float(raw)
            if price <= 1:
                raise Top5CanaryBatchStorageError("fixture odds are invalid")
            normalized_odds[outcome] = price

        generated_at = _parse_time(
            source.get("prediction_timestamp") or payload.generated_at.isoformat(),
            "prediction_timestamp",
        )
        captured_at = _parse_time(
            source.get("signal_timestamp") or source.get("captured_at"),
            "signal_timestamp",
        )
        odds_age = (generated_at - captured_at).total_seconds()
        if not 0 <= odds_age <= TOP5_CANARY_MAX_ODDS_AGE_SECONDS:
            raise Top5CanaryBatchStorageError("odds are stale or future-dated")

        lifecycles = source.get("top5_signal_lifecycles")
        if not isinstance(lifecycles, Sequence) or isinstance(
            lifecycles, (str, bytes, bytearray)
        ):
            raise Top5CanaryBatchStorageError("typed private lifecycle set is required")
        try:
            ordered = canonical_top5_h2h_lifecycle_set(lifecycles)
            current = ordered["home"].current_version
            lifecycle_projection = project_top5_signal_lifecycles(
                tuple(ordered[outcome] for outcome in _OUTCOMES),
                prediction_probabilities=probabilities,
                fixture_identity=fixture_key,
                league_identity=league,
                candidate_identity=payload.candidate_id,
                model_identity=payload.model_identity,
                provider_authority=payload.provider_authority,
                prediction_timestamp=generated_at,
                signal_timestamp=captured_at,
                snapshot_id=_required_text(
                    source.get("snapshot_id") or source.get("signal_snapshot_id"),
                    "snapshot_id",
                ),
                provenance={
                    "source_sha": payload.source_sha,
                    "research_sha": payload.research_sha,
                    "model_artifact_hash": payload.model_artifact_hash,
                },
                market_identity="h2h",
            )
        except (TypeError, ValueError) as exc:
            raise Top5CanaryBatchStorageError(
                "private lifecycle validation failed"
            ) from exc

        if (
            current.stage
            not in {SignalLifecycleStage.INITIAL, SignalLifecycleStage.REFINED}
            or current.withdrawal_authorized
            or set(lifecycle_projection) != set(_OUTCOMES)
        ):
            raise Top5CanaryBatchStorageError("lifecycle state is not publishable")
        decision_ids = {
            _required_text(ordered[outcome].current_version.decision_id, "decision_id")
            for outcome in _OUTCOMES
        }
        if len(decision_ids) != 1:
            raise Top5CanaryBatchStorageError(
                "activation authorization identity differs across outcomes"
            )
        signal_decision = current.eligibility_decision
        if not isinstance(signal_decision, bool):
            raise Top5CanaryBatchStorageError("signal decision is malformed")
        canonical_record: dict[str, object] = {
            "fixture_identity": fixture_key,
            "league": league,
            "home": home,
            "away": away,
            "kickoff": kickoff.isoformat(),
            "lifecycle_stage": (
                "INITIAL"
                if current.stage is SignalLifecycleStage.INITIAL
                else "REFINEMENT"
            ),
            "initial_timestamp": ordered[
                "home"
            ].initial_version.prediction_generated_at.isoformat(),
            "refinement_timestamp": (
                current.prediction_generated_at.isoformat()
                if current.stage is SignalLifecycleStage.REFINED
                else None
            ),
            "model_identity": payload.model_identity,
            "model_version": payload.model_artifact_hash,
            "probabilities": probabilities,
            "confidence": {
                "kind": "highest_outcome_probability",
                "value": max(probabilities.values()),
            },
            "signal_state": "SIGNAL" if signal_decision else "NO_SIGNAL",
            "signal_decision_id": next(iter(decision_ids)),
            "source": {
                "provider_authority": payload.provider_authority,
                "snapshot_source": current.snapshot_source,
                "snapshot_id": current.snapshot_id,
                "captured_at": captured_at.isoformat(),
                "source_sha": payload.source_sha,
                "research_sha": payload.research_sha,
                "model_artifact_hash": payload.model_artifact_hash,
                "evidence_digest": payload.evidence_digest,
            },
            "odds_freshness": {
                "age_seconds": round(odds_age, 3),
                "maximum_age_seconds": TOP5_CANARY_MAX_ODDS_AGE_SECONDS,
            },
            "activation_id": activation_id,
            "activation_authorization_id": next(iter(decision_ids)),
            "publication_authorization_id": publication_authorization_id,
            "generated_at": generated_at.isoformat(),
            "artifact_digest": artifact.artifact_digest,
            "version_digest": top5_h2h_lifecycle_set_digest(
                tuple(ordered[outcome] for outcome in _OUTCOMES)
            ),
            "no_bet": True,
        }
        canonical_record["record_digest"] = _digest(canonical_record)
        records.append(canonical_record)

    public_records = public.get("football")
    if not isinstance(public_records, Sequence) or isinstance(
        public_records, (str, bytes, bytearray)
    ):
        raise Top5CanaryBatchStorageError("public Top-5 adapter output is missing")
    public_top5 = [
        record
        for record in public_records
        if isinstance(record, Mapping) and record.get("league") in _LEAGUES
    ]
    if len(public_top5) != 15:
        raise Top5CanaryBatchStorageError("public adapter must contain all 15 outcomes")
    for canonical_record in records:
        grouped = [
            record
            for record in public_top5
            if record.get("fixture_key") == canonical_record["fixture_identity"]
            and record.get("league") == canonical_record["league"]
        ]
        if {record.get("market") for record in grouped} != set(_OUTCOMES):
            raise Top5CanaryBatchStorageError(
                "public adapter fixture/outcome mapping is incomplete"
            )
        if any(
            record.get("signal_status") != "CONTROLLED"
            or record.get("publication_status") != "PUBLISHED"
            or record.get("publication_enabled") is not True
            or record.get("no_bet") is not True
            for record in grouped
        ):
            raise Top5CanaryBatchStorageError("public adapter exposes an unsafe state")
        by_market = {str(record["market"]): record for record in grouped}
        for outcome in _OUTCOMES:
            public_probability = by_market[outcome].get("model_prob")
            public_odds = by_market[outcome].get("odds")
            if (
                isinstance(public_probability, bool)
                or not isinstance(public_probability, (int, float))
                or abs(float(public_probability) / 100 - probabilities[outcome])
                > 0.000051
                or isinstance(public_odds, bool)
                or not isinstance(public_odds, (int, float))
                or abs(float(public_odds) - normalized_odds[outcome]) > 0.000051
            ):
                raise Top5CanaryBatchStorageError(
                    "public adapter probabilities/odds differ from canonical fixture"
                )

    canonical_batch: dict[str, object] = {
        "schema_version": TOP5_CANARY_BATCH_SCHEMA,
        "batch_id": batch_id,
        "artifact_digest": artifact.artifact_digest,
        "generation_id": batch_id,
        "provider_authority": "the_odds_api",
        "activation_id": activation_id,
        "publication_authorization_id": publication_authorization_id,
        "generated_at": _parse_time(
            release.get("generated_at"), "generated_at"
        ).isoformat(),
        "fixture_count": 5,
        "fixture_records": records,
        "no_bet": True,
    }
    _assert_no_private_keys(canonical_batch)
    canonical_batch["batch_digest"] = _digest(canonical_batch)
    return canonical_batch


class Top5CanaryStaticTransport(Protocol):
    def current_payload(self) -> bytes | None: ...

    def stage(self, path: str, payload: bytes) -> None: ...

    def commit(self) -> None: ...

    def rollback_stage(self) -> None: ...


class Top5CanaryWorkerTransport(Protocol):
    def current_payload(self) -> bytes | None: ...

    def write_signals(self, payload: bytes) -> None: ...

    def restore_signals(self, payload: bytes | None) -> None: ...


class Top5CanaryBatchStore:
    """Owner-only append-audited batch state plus verified rollback snapshots."""

    def __init__(self, root: str | Path | None = None) -> None:
        resolved = (
            Path(root)
            if root is not None
            else runtime_state_path(TOP5_CANARY_BATCH_STATE_DIR, require_external=True)
        )
        if not resolved.is_absolute():
            raise Top5CanaryBatchStorageError("batch state root must be absolute")
        self.root = Path(os.path.abspath(resolved))
        checkout = Path(__file__).resolve().parents[2]
        if self.root.resolve() == checkout or checkout in self.root.resolve().parents:
            raise Top5CanaryBatchStorageError(
                "batch state must remain outside checkout"
            )

    @staticmethod
    def _event(
        previous: Mapping[str, object] | None, name: str, at: datetime, digest: str
    ) -> dict[str, object]:
        prior_events = previous.get("events", []) if previous else []
        prior = prior_events[-1].get("event_digest") if prior_events else "0" * 64
        event: dict[str, object] = {
            "sequence": len(prior_events) + 1,
            "event": name,
            "at": at.astimezone(timezone.utc).isoformat(),
            "public_digest": digest,
            "previous_event_digest": prior,
        }
        event["event_digest"] = _digest(event)
        return event

    def _batch_path(self, batch_id: str) -> Path:
        name = hashlib.sha256(batch_id.encode("utf-8")).hexdigest()
        return self.root / f"{name}.json"

    def _locked(self):
        class Lock:
            def __init__(self, root: Path):
                self.root = root
                self.descriptor: int | None = None

            def __enter__(self):
                self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
                if self.root.is_symlink() or self.root.resolve() != self.root:
                    raise Top5CanaryBatchStorageError("batch state root is symlinked")
                metadata = self.root.stat()
                if metadata.st_uid != os.getuid():
                    raise Top5CanaryBatchStorageError(
                        "batch state root is not owner-owned"
                    )
                os.chmod(self.root, 0o700)
                nofollow = getattr(os, "O_NOFOLLOW", None)
                if nofollow is None:
                    raise Top5CanaryBatchStorageError(
                        "no-follow file support is required"
                    )
                self.descriptor = os.open(
                    self.root / ".batch.lock", os.O_CREAT | os.O_RDWR | nofollow, 0o600
                )
                if not stat.S_ISREG(os.fstat(self.descriptor).st_mode):
                    raise Top5CanaryBatchStorageError(
                        "batch lock is not a regular file"
                    )
                os.fchmod(self.descriptor, 0o600)
                fcntl.flock(self.descriptor, fcntl.LOCK_EX)
                return self

            def __exit__(self, *_args):
                assert self.descriptor is not None
                fcntl.flock(self.descriptor, fcntl.LOCK_UN)
                os.close(self.descriptor)

        return Lock(self.root)

    def _read(self, batch_id: str) -> dict[str, object] | None:
        path = self._batch_path(batch_id)
        if path.is_symlink():
            raise Top5CanaryBatchStorageError("batch state must not be a symlink")
        if not path.exists():
            return None
        nofollow = getattr(os, "O_NOFOLLOW", None)
        if nofollow is None:
            raise Top5CanaryBatchStorageError("no-follow file support is required")
        try:
            descriptor = os.open(path, os.O_RDONLY | nofollow)
            with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
                metadata = os.fstat(handle.fileno())
                if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid():
                    raise Top5CanaryBatchStorageError(
                        "batch state file ownership is invalid"
                    )
                if metadata.st_mode & 0o077:
                    raise Top5CanaryBatchStorageError(
                        "batch state file permissions are unsafe"
                    )
                raw = json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            raise Top5CanaryBatchStorageError(
                "batch state is unreadable or malformed"
            ) from exc
        fields = {
            "schema_version",
            "batch_id",
            "state",
            "canonical_batch",
            "canonical_batch_digest",
            "artifact_digest",
            "public_payload_b64",
            "public_payload_digest",
            "previous_payload_b64",
            "previous_payload_digest",
            "previous_generation_id",
            "events",
            "store_digest",
        }
        if (
            not isinstance(raw, dict)
            or set(raw) != fields
            or raw.get("batch_id") != batch_id
        ):
            raise Top5CanaryBatchStorageError("batch state identity is malformed")
        actual = raw.pop("store_digest", None)
        if actual != _digest(raw):
            raise Top5CanaryBatchStorageError("batch state digest mismatch")
        raw["store_digest"] = actual
        if raw.get("schema_version") != TOP5_CANARY_BATCH_STORE_SCHEMA:
            raise Top5CanaryBatchStorageError("unsupported batch state schema")
        try:
            Top5CanaryBatchState(raw.get("state"))
        except (TypeError, ValueError) as exc:
            raise Top5CanaryBatchStorageError("batch state value is invalid") from exc
        canonical_batch = raw.get("canonical_batch")
        unsigned_batch = (
            dict(canonical_batch) if isinstance(canonical_batch, Mapping) else {}
        )
        batch_digest = unsigned_batch.pop("batch_digest", None)
        if (
            not isinstance(canonical_batch, Mapping)
            or canonical_batch.get("schema_version") != TOP5_CANARY_BATCH_SCHEMA
            or canonical_batch.get("batch_id") != batch_id
            or batch_digest != _digest(unsigned_batch)
            or _digest(canonical_batch) != raw.get("canonical_batch_digest")
            or canonical_batch.get("artifact_digest") != raw.get("artifact_digest")
        ):
            raise Top5CanaryBatchStorageError("canonical stored batch is malformed")
        try:
            public_raw = base64.b64decode(raw["public_payload_b64"], validate=True)
            previous_raw = base64.b64decode(raw["previous_payload_b64"], validate=True)
        except (TypeError, ValueError) as exc:
            raise Top5CanaryBatchStorageError(
                "stored public snapshot encoding is invalid"
            ) from exc
        public = self._public_payload(public_raw, "stored public payload")
        previous = self._public_payload(previous_raw, "stored previous payload")
        if hashlib.sha256(public).hexdigest() != raw.get(
            "public_payload_digest"
        ) or hashlib.sha256(previous).hexdigest() != raw.get("previous_payload_digest"):
            raise Top5CanaryBatchStorageError("stored public snapshot digest mismatch")
        events = raw.get("events")
        if not isinstance(events, list) or not events:
            raise Top5CanaryBatchStorageError("batch audit trail is missing")
        previous_event_digest = "0" * 64
        for index, event in enumerate(events, start=1):
            if not isinstance(event, Mapping):
                raise Top5CanaryBatchStorageError("batch audit event is malformed")
            unsigned = dict(event)
            event_digest = unsigned.pop("event_digest", None)
            if (
                event.get("sequence") != index
                or event.get("previous_event_digest") != previous_event_digest
                or event_digest != _digest(unsigned)
            ):
                raise Top5CanaryBatchStorageError("batch audit trail digest mismatch")
            previous_event_digest = str(event_digest)
        return raw

    def _write(self, record: dict[str, object]) -> dict[str, object]:
        clean = dict(record)
        clean.pop("store_digest", None)
        clean["store_digest"] = _digest(clean)
        path = self._batch_path(str(clean["batch_id"]))
        atomic_write_json(path, clean, sort_keys=True, ensure_ascii=True)
        os.chmod(path, 0o600)
        return clean

    def prepare(
        self,
        *,
        canonical_batch: Mapping[str, object],
        artifact_digest: str,
        public_payload: bytes,
        previous_payload: bytes,
        now: datetime,
    ) -> dict[str, object]:
        if canonical_batch.get("schema_version") != TOP5_CANARY_BATCH_SCHEMA:
            raise Top5CanaryBatchStorageError("unsupported canonical batch schema")
        batch_id = _required_text(canonical_batch.get("batch_id"), "batch_id")
        unsigned_batch = dict(canonical_batch)
        batch_digest = unsigned_batch.pop("batch_digest", None)
        _assert_no_private_keys(canonical_batch)
        fixture_records = canonical_batch.get("fixture_records")
        if (
            batch_digest != _digest(unsigned_batch)
            or canonical_batch.get("fixture_count") != 5
            or not isinstance(fixture_records, Sequence)
            or isinstance(fixture_records, (str, bytes, bytearray))
            or len(fixture_records) != 5
        ):
            raise Top5CanaryBatchStorageError(
                "canonical five-fixture batch is malformed"
            )
        if canonical_batch.get("artifact_digest") != artifact_digest:
            raise Top5CanaryBatchStorageError("artifact digest binding mismatch")
        current_product = self._public_payload(public_payload, "new public payload")
        previous_product = self._public_payload(
            previous_payload, "previous public payload"
        )
        public_digest = hashlib.sha256(current_product).hexdigest()
        previous_digest = hashlib.sha256(previous_product).hexdigest()
        prior_release = json.loads(previous_product).get("top5_release")
        previous_generation = (
            prior_release.get("generation_id")
            if isinstance(prior_release, Mapping)
            else None
        )
        with self._locked():
            existing = self._read(batch_id)
            candidate = {
                "schema_version": TOP5_CANARY_BATCH_STORE_SCHEMA,
                "batch_id": batch_id,
                "state": Top5CanaryBatchState.PREPARING.value,
                "canonical_batch": dict(canonical_batch),
                "canonical_batch_digest": _digest(canonical_batch),
                "artifact_digest": artifact_digest,
                "public_payload_b64": base64.b64encode(current_product).decode("ascii"),
                "public_payload_digest": public_digest,
                "previous_payload_b64": base64.b64encode(previous_product).decode(
                    "ascii"
                ),
                "previous_payload_digest": previous_digest,
                "previous_generation_id": previous_generation,
                "events": [],
            }
            if existing is not None:
                for field in (
                    "canonical_batch_digest",
                    "artifact_digest",
                    "public_payload_digest",
                    "previous_payload_digest",
                ):
                    if existing.get(field) != candidate[field]:
                        raise Top5CanaryBatchStorageError(
                            "same batch identity has conflicting immutable content"
                        )
                return existing
            candidate["events"] = [
                self._event(None, "batch_started", now, public_digest),
                self._event(
                    {
                        "events": [
                            self._event(None, "batch_started", now, public_digest)
                        ]
                    },
                    "validation_passed",
                    now,
                    public_digest,
                ),
            ]
            return self._write(candidate)

    @staticmethod
    def _public_payload(payload: bytes, name: str) -> bytes:
        if not isinstance(payload, bytes) or not payload:
            raise Top5CanaryBatchStorageError(f"{name} is missing")
        try:
            decoded = json.loads(payload)
            if not isinstance(decoded, dict):
                raise TypeError
            canonical = serialize_public_product(decoded)
            encoded = _bytes(canonical)
        except (
            UnicodeDecodeError,
            json.JSONDecodeError,
            AssertionError,
            TypeError,
            ValueError,
        ) as exc:
            raise Top5CanaryBatchStorageError(
                f"{name} is not canonical public JSON"
            ) from exc
        return encoded

    def transition(
        self,
        batch_id: str,
        state: Top5CanaryBatchState,
        *,
        event: str,
        public_digest: str,
        now: datetime,
    ) -> dict[str, object]:
        with self._locked():
            record = self._read(batch_id)
            if record is None:
                raise Top5CanaryBatchStorageError("batch state is missing")
            current = Top5CanaryBatchState(record["state"])
            allowed = {
                Top5CanaryBatchState.PREPARING: {
                    Top5CanaryBatchState.COMMITTED,
                    Top5CanaryBatchState.FAILED,
                    Top5CanaryBatchState.ROLLED_BACK,
                },
                Top5CanaryBatchState.COMMITTED: {
                    Top5CanaryBatchState.FAILED,
                    Top5CanaryBatchState.ROLLED_BACK,
                },
                Top5CanaryBatchState.FAILED: set(),
                Top5CanaryBatchState.ROLLED_BACK: set(),
            }
            if state is current:
                return record
            if state not in allowed[current]:
                raise Top5CanaryBatchStorageError("invalid batch state transition")
            if public_digest != record["public_payload_digest"]:
                raise Top5CanaryBatchStorageError("transition public digest mismatch")
            events = list(record["events"])
            events.append(self._event(record, event, now, public_digest))
            record["events"] = events
            record["state"] = state.value
            return self._write(record)

    def committed_public_product(self, batch_id: str) -> dict[str, object]:
        record = self.load(batch_id)
        if (
            record is None
            or record.get("state") != Top5CanaryBatchState.COMMITTED.value
        ):
            raise Top5CanaryBatchStorageError(
                "public adapter rejects uncommitted batch"
            )
        payload = base64.b64decode(record["public_payload_b64"], validate=True)
        self._public_payload(payload, "committed public payload")
        result = json.loads(payload)
        if (
            hashlib.sha256(_bytes(result)).hexdigest()
            != record["public_payload_digest"]
        ):
            raise Top5CanaryBatchStorageError(
                "committed public payload digest mismatch"
            )
        return result

    def load(self, batch_id: str) -> dict[str, object] | None:
        with self._locked():
            return self._read(batch_id)

    def record_event(
        self, batch_id: str, *, event: str, public_digest: str, now: datetime
    ) -> dict[str, object]:
        with self._locked():
            record = self._read(batch_id)
            if record is None:
                raise Top5CanaryBatchStorageError("batch state is missing")
            if public_digest != record.get("public_payload_digest"):
                raise Top5CanaryBatchStorageError("audit event public digest mismatch")
            events = list(record["events"])
            events.append(self._event(record, event, now, public_digest))
            record["events"] = events
            return self._write(record)

    def rollback_payload(
        self,
        batch_id: str,
        *,
        expected_generation_id: str,
        expected_current_digest: str,
    ) -> bytes:
        record = self.load(batch_id)
        if (
            record is None
            or record.get("state") != Top5CanaryBatchState.COMMITTED.value
        ):
            raise Top5CanaryBatchStorageError(
                "only a committed batch can be rolled back"
            )
        if (
            record.get("canonical_batch", {}).get("generation_id")
            != expected_generation_id
            or record.get("public_payload_digest") != expected_current_digest
        ):
            raise Top5CanaryBatchStorageError(
                "rollback target confirmation does not match"
            )
        payload = base64.b64decode(record["previous_payload_b64"], validate=True)
        canonical = self._public_payload(payload, "rollback snapshot")
        if hashlib.sha256(canonical).hexdigest() != record["previous_payload_digest"]:
            raise Top5CanaryBatchStorageError(
                "known-good rollback snapshot is unverifiable"
            )
        return canonical


def _transport_digest(transport: object) -> str | None:
    payload = transport.current_payload()  # type: ignore[attr-defined]
    if payload is None:
        return None
    canonical = Top5CanaryBatchStore._public_payload(payload, "delivery target")
    return hashlib.sha256(canonical).hexdigest()


def execute_stored_top5_batch(
    *,
    store: Top5CanaryBatchStore,
    executor: object,
    artifact: PublishedTop5BatchArtifact,
    current_public_snapshot: Mapping[str, object],
    plan: object,
    attestation: object,
    capability: Mapping[str, object],
    now: datetime,
) -> object:
    """Persist PREPARING, execute the existing guarded writer, verify, commit."""

    canonical_batch = canonical_top5_canary_batch(artifact)
    plan.validate()  # type: ignore[attr-defined]
    attestation.validate(  # type: ignore[attr-defined]
        plan=plan, artifact=artifact, capability=capability, now=now
    )
    previous = _bytes(serialize_public_product(dict(current_public_snapshot)))
    store.prepare(
        canonical_batch=canonical_batch,
        artifact_digest=artifact.artifact_digest,
        public_payload=plan.serialized_payload,  # type: ignore[attr-defined]
        previous_payload=previous,
        now=now,
    )
    current_record = store.load(canonical_batch["batch_id"])
    assert current_record is not None
    expected = hashlib.sha256(plan.serialized_payload).hexdigest()  # type: ignore[attr-defined]
    previous_digest = hashlib.sha256(previous).hexdigest()
    static = getattr(executor, "static_transport", None)
    worker = getattr(executor, "worker_transport", None)
    if current_record.get("state") == Top5CanaryBatchState.COMMITTED.value:
        if (
            static is None
            or worker is None
            or _transport_digest(static) != expected
            or _transport_digest(worker) != expected
        ):
            raise Top5CanaryBatchStorageError("committed batch target readback changed")
        from src.football.top5_public_delivery import Top5DeliveryExecutionResult

        return Top5DeliveryExecutionResult(
            "TOP5_DELIVERY_IDEMPOTENT",
            ("PREPARED",),
            str(canonical_batch["generation_id"]),
            expected,
        )
    if current_record.get("state") in {
        Top5CanaryBatchState.FAILED.value,
        Top5CanaryBatchState.ROLLED_BACK.value,
    }:
        raise Top5CanaryBatchStorageError("batch identity is already terminal")
    if static is not None and worker is not None:
        static_digest = _transport_digest(static)
        worker_digest = _transport_digest(worker)
        if static_digest == expected and worker_digest == expected:
            store.transition(
                canonical_batch["batch_id"],
                Top5CanaryBatchState.COMMITTED,
                event="public_readback_reconciled",
                public_digest=expected,
                now=now,
            )
            from src.football.top5_public_delivery import Top5DeliveryExecutionResult

            return Top5DeliveryExecutionResult(
                "TOP5_DELIVERY_IDEMPOTENT",
                ("PREPARED",),
                str(canonical_batch["generation_id"]),
                expected,
            )
        if (static_digest, worker_digest) not in {
            (previous_digest, previous_digest),
            (None, None),
        }:
            store.transition(
                canonical_batch["batch_id"],
                Top5CanaryBatchState.FAILED,
                event="interrupted_batch_target_mismatch",
                public_digest=expected,
                now=now,
            )
            raise Top5CanaryBatchStorageError("interrupted batch targets disagree")
    try:
        result = executor.execute(  # type: ignore[attr-defined]
            artifact=artifact,
            current_public_snapshot=current_public_snapshot,
            plan=plan,
            attestation=attestation,
            capability=capability,
            dry_run=False,
        )
    except Exception:
        previous_verified = (
            static is not None
            and worker is not None
            and _transport_digest(static) == previous_digest
            and _transport_digest(worker) == previous_digest
        )
        store.transition(
            canonical_batch["batch_id"],
            Top5CanaryBatchState.ROLLED_BACK
            if previous_verified
            else Top5CanaryBatchState.FAILED,
            event="batch_failed_rolled_back"
            if previous_verified
            else "batch_failed_closed",
            public_digest=hashlib.sha256(plan.serialized_payload).hexdigest(),  # type: ignore[attr-defined]
            now=now,
        )
        raise

    status = getattr(result, "status", None)
    if status in {"ACCEPTANCE_REQUIRED", "TOP5_DELIVERY_IDEMPOTENT"}:
        if (
            static is None
            or worker is None
            or _transport_digest(static) != expected
            or _transport_digest(worker) != expected
        ):
            restored = False
            try:
                previous_bytes = base64.b64decode(
                    current_record["previous_payload_b64"], validate=True
                )
                static.stage("docs/data/signals.json", previous_bytes)
                worker.restore_signals(previous_bytes)
                static.commit()
                restored = (
                    _transport_digest(static) == previous_digest
                    and _transport_digest(worker) == previous_digest
                )
            except Exception:  # noqa: BLE001 - a failed external write is resolved by readback.
                try:
                    static.rollback_stage()
                except Exception:  # noqa: BLE001, S110 - cleanup is best-effort; state stays FAILED unless readback proves rollback.
                    pass
            store.transition(
                canonical_batch["batch_id"],
                Top5CanaryBatchState.ROLLED_BACK
                if restored
                else Top5CanaryBatchState.FAILED,
                event="public_readback_failed_rolled_back"
                if restored
                else "public_readback_failed",
                public_digest=expected,
                now=now,
            )
            raise Top5CanaryBatchStorageError("public target readback did not verify")
        store.record_event(
            canonical_batch["batch_id"],
            event="storage_committed",
            public_digest=expected,
            now=now,
        )
        store.transition(
            canonical_batch["batch_id"],
            Top5CanaryBatchState.COMMITTED,
            event="public_adapter_updated",
            public_digest=expected,
            now=now,
        )
        store.committed_public_product(canonical_batch["batch_id"])
    elif status == "TOP5_DELIVERY_ROLLBACK_SUCCEEDED":
        previous_digest = hashlib.sha256(previous).hexdigest()
        if (
            static is None
            or worker is None
            or _transport_digest(static) != previous_digest
            or _transport_digest(worker) != previous_digest
        ):
            store.transition(
                canonical_batch["batch_id"],
                Top5CanaryBatchState.FAILED,
                event="automatic_rollback_unverified",
                public_digest=expected,
                now=now,
            )
        else:
            store.transition(
                canonical_batch["batch_id"],
                Top5CanaryBatchState.ROLLED_BACK,
                event="automatic_rollback_verified",
                public_digest=expected,
                now=now,
            )
    else:
        store.transition(
            canonical_batch["batch_id"],
            Top5CanaryBatchState.FAILED,
            event="delivery_failed_closed",
            public_digest=expected,
            now=now,
        )
    return result


def rollback_committed_top5_batch(
    *,
    store: Top5CanaryBatchStore,
    batch_id: str,
    expected_generation_id: str,
    expected_current_digest: str,
    static_transport: Top5CanaryStaticTransport,
    worker_transport: Top5CanaryWorkerTransport,
    now: datetime,
) -> dict[str, object]:
    """Restore verified prior public bytes once; never retry or erase evidence."""

    previous = store.rollback_payload(
        batch_id,
        expected_generation_id=expected_generation_id,
        expected_current_digest=expected_current_digest,
    )
    record = store.load(batch_id)
    assert record is not None
    expected_digest = str(record["public_payload_digest"])
    previous_digest = str(record["previous_payload_digest"])
    if (
        _transport_digest(static_transport) != expected_digest
        or _transport_digest(worker_transport) != expected_digest
    ):
        store.record_event(
            batch_id,
            event="operator_rollback_refused_target_mismatch",
            public_digest=expected_digest,
            now=now,
        )
        raise Top5CanaryBatchStorageError(
            "rollback refused because public targets no longer match this generation"
        )
    static_staged = False
    try:
        static_transport.stage("docs/data/signals.json", previous)
        static_staged = True
        worker_transport.restore_signals(previous)
        static_transport.commit()
    except Exception as exc:
        if static_staged:
            try:
                static_transport.rollback_stage()
            except Exception:  # noqa: BLE001, S110 - cleanup is best-effort; verification below is authoritative.
                pass
        # A write may have applied before an error. A single readback determines
        # whether the rollback completed; no second mutation attempt is made.
        if (
            _transport_digest(static_transport) == previous_digest
            and _transport_digest(worker_transport) == previous_digest
        ):
            store.transition(
                batch_id,
                Top5CanaryBatchState.ROLLED_BACK,
                event="operator_rollback_verified_after_transport_error",
                public_digest=expected_digest,
                now=now,
            )
            return {
                "status": "ROLLED_BACK",
                "public_digest": previous_digest,
                "audit_preserved": True,
            }
        store.transition(
            batch_id,
            Top5CanaryBatchState.FAILED,
            event="operator_rollback_unverified",
            public_digest=expected_digest,
            now=now,
        )
        raise Top5CanaryBatchStorageError(
            "operator rollback failed verification"
        ) from exc

    if (
        _transport_digest(static_transport) != previous_digest
        or _transport_digest(worker_transport) != previous_digest
    ):
        store.transition(
            batch_id,
            Top5CanaryBatchState.FAILED,
            event="operator_rollback_readback_failed",
            public_digest=expected_digest,
            now=now,
        )
        raise Top5CanaryBatchStorageError("operator rollback readback did not verify")
    store.transition(
        batch_id,
        Top5CanaryBatchState.ROLLED_BACK,
        event="operator_rollback_verified",
        public_digest=expected_digest,
        now=now,
    )
    return {
        "status": "ROLLED_BACK",
        "public_digest": previous_digest,
        "audit_preserved": True,
    }


__all__ = [
    "TOP5_CANARY_BATCH_SCHEMA",
    "TOP5_CANARY_BATCH_STATE_DIR",
    "Top5CanaryBatchState",
    "Top5CanaryBatchStorageError",
    "Top5CanaryBatchStore",
    "canonical_top5_canary_batch",
    "execute_stored_top5_batch",
    "rollback_committed_top5_batch",
]
