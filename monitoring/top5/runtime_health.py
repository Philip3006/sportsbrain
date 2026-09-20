"""Bounded, read-only final readiness checks for the Top-5 runtime.

This module intentionally observes repository contracts only.  It does not
contact a provider, call launchd, write a health artifact, publish a payload,
or inspect a private ledger.  A runtime-dirtiness note is an informational
warning; source dirtiness, unsafe writer state, and any production mutation
are blocking findings.
"""
from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

READY_STATUS = "TOP5_RUNTIME_HEALTH_OFFLINE_READY"
BLOCKED_STATUS = "TOP5_RUNTIME_HEALTH_BLOCKED"
OFFLINE_MODE = "offline-only"

# These limits keep an accidental diagnostic dump from becoming an unbounded
# context input.  They are deliberately small because the readiness result is
# an inventory, not an event log.
MAX_EVIDENCE_ITEMS = 64
MAX_TEXT_LENGTH = 256
MAX_MANIFEST_BYTES = 32 * 1024
MAX_REASONS = 32

_HEALTHY_STATES = frozenset({"ok", "healthy"})
_PRESENT_STATES = frozenset({"active", "healthy", "ok", "present", "read_only"})
_SAFE_UNREGISTERED_STATES = frozenset({"disabled", "not_registered", "read_only"})
_INTERFACE_STATES = frozenset({"present", "offline", "not_registered"})
_MUTATION_KEYS = (
    "publication",
    "betting",
    "ledger",
    "sealed_partition",
    "network",
    "writer",
)


class Top5RuntimeHealthError(ValueError):
    """Raised for malformed offline evidence supplied by a caller."""


def _text(name: str, value: object, *, max_length: int = MAX_TEXT_LENGTH) -> str:
    if not isinstance(value, str) or not value.strip():
        raise Top5RuntimeHealthError(f"{name} must be a nonblank string")
    result = value.strip()
    if len(result) > max_length:
        raise Top5RuntimeHealthError(f"{name} exceeds the bounded length")
    return result


def _bounded_strings(name: str, values: Sequence[object] | None) -> tuple[str, ...]:
    if values is None:
        return ()
    if isinstance(values, (str, bytes)) or len(values) > MAX_EVIDENCE_ITEMS:
        raise Top5RuntimeHealthError(f"{name} exceeds the bounded item limit")
    return tuple(_text(f"{name}[{index}]", value) for index, value in enumerate(values))


def _bounded_map(name: str, values: Mapping[object, object] | None) -> dict[str, str]:
    if values is None:
        return {}
    if not isinstance(values, Mapping) or len(values) > MAX_EVIDENCE_ITEMS:
        raise Top5RuntimeHealthError(f"{name} must be a bounded mapping")
    return {
        _text(f"{name} key", key): _text(f"{name}[{key}]", value)
        for key, value in sorted(values.items(), key=lambda item: str(item[0]))
    }


def _bounded_int_map(name: str, values: Mapping[object, object] | None) -> dict[str, int]:
    if values is None:
        return {}
    if not isinstance(values, Mapping) or len(values) > MAX_EVIDENCE_ITEMS:
        raise Top5RuntimeHealthError(f"{name} must be a bounded mapping")
    result: dict[str, int] = {}
    for key, value in sorted(values.items(), key=lambda item: str(item[0])):
        label = _text(f"{name} key", key)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise Top5RuntimeHealthError(f"{name}[{label}] must be a non-negative integer")
        result[label] = value
    return result


def _bool(name: str, value: object) -> bool:
    if not isinstance(value, bool):
        raise Top5RuntimeHealthError(f"{name} must be boolean evidence")
    return value


def _path_state(root: Path, relative_path: str) -> str:
    """Return presence without reading the target file."""

    return "present" if (root / relative_path).is_file() else "missing"


@dataclass(frozen=True)
class Top5RuntimeEvidence:
    """Small immutable evidence envelope consumed by the offline evaluator.

    The fields mirror the final-readiness inventory without copying payloads or
    logs into memory.  Callers may construct this directly for deterministic
    tests or use :func:`collect_offline_runtime_evidence` for repository-local
    presence checks.
    """

    worker_health: str = "ok"
    pwa_health: str = "ok"
    football_scheduler_state: str = "inactive"
    launchd_expectations: Mapping[str, str] = field(default_factory=dict)
    runtime_writer_state: Mapping[str, str] = field(default_factory=dict)
    ledger_writer_state: Mapping[str, str] = field(default_factory=dict)
    football_health_artifacts: Mapping[str, str] = field(default_factory=dict)
    request_interfaces: Mapping[str, str] = field(default_factory=dict)
    quota_interfaces: Mapping[str, str] = field(default_factory=dict)
    publication_enabled: bool = False
    betting_enabled: bool = False
    ledger_mutations: int = 0
    sealed_partition_mutations: int = 0
    production_mutations: Mapping[str, int] = field(default_factory=dict)
    runtime_dirtiness: tuple[str, ...] = ()
    source_dirtiness: tuple[str, ...] = ()

    @classmethod
    def from_mapping(cls, values: Mapping[str, object]) -> "Top5RuntimeEvidence":
        if not isinstance(values, Mapping):
            raise Top5RuntimeHealthError("runtime evidence must be an object")
        return cls(
            worker_health=values.get("worker_health", "ok"),
            pwa_health=values.get("pwa_health", "ok"),
            football_scheduler_state=values.get("football_scheduler_state", "inactive"),
            launchd_expectations=values.get("launchd_expectations") or {},
            runtime_writer_state=values.get("runtime_writer_state") or {},
            ledger_writer_state=values.get("ledger_writer_state") or {},
            football_health_artifacts=values.get("football_health_artifacts") or {},
            request_interfaces=values.get("request_interfaces") or {},
            quota_interfaces=values.get("quota_interfaces") or {},
            publication_enabled=values.get("publication_enabled", False),
            betting_enabled=values.get("betting_enabled", False),
            ledger_mutations=values.get("ledger_mutations", 0),
            sealed_partition_mutations=values.get("sealed_partition_mutations", 0),
            production_mutations=values.get("production_mutations") or {},
            runtime_dirtiness=_bounded_strings("runtime_dirtiness", values.get("runtime_dirtiness")),
            source_dirtiness=_bounded_strings("source_dirtiness", values.get("source_dirtiness")),
        )

    def validate_shape(self) -> None:
        for name, value in (
            ("worker_health", self.worker_health),
            ("pwa_health", self.pwa_health),
            ("football_scheduler_state", self.football_scheduler_state),
        ):
            _text(name, value)
        for name, values in (
            ("launchd_expectations", self.launchd_expectations),
            ("runtime_writer_state", self.runtime_writer_state),
            ("ledger_writer_state", self.ledger_writer_state),
            ("football_health_artifacts", self.football_health_artifacts),
            ("request_interfaces", self.request_interfaces),
            ("quota_interfaces", self.quota_interfaces),
        ):
            _bounded_map(name, values)
        _bounded_int_map("production_mutations", self.production_mutations)
        _bool("publication_enabled", self.publication_enabled)
        _bool("betting_enabled", self.betting_enabled)
        for name, value in (
            ("ledger_mutations", self.ledger_mutations),
            ("sealed_partition_mutations", self.sealed_partition_mutations),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise Top5RuntimeHealthError(f"{name} must be a non-negative integer")
        _bounded_strings("runtime_dirtiness", self.runtime_dirtiness)
        _bounded_strings("source_dirtiness", self.source_dirtiness)

    def as_payload(self) -> dict[str, object]:
        self.validate_shape()
        return {
            "worker_health": self.worker_health,
            "pwa_health": self.pwa_health,
            "football_scheduler_state": self.football_scheduler_state,
            "launchd_expectations": dict(sorted(self.launchd_expectations.items())),
            "runtime_writer_state": dict(sorted(self.runtime_writer_state.items())),
            "ledger_writer_state": dict(sorted(self.ledger_writer_state.items())),
            "football_health_artifacts": dict(sorted(self.football_health_artifacts.items())),
            "request_interfaces": dict(sorted(self.request_interfaces.items())),
            "quota_interfaces": dict(sorted(self.quota_interfaces.items())),
            "publication_enabled": self.publication_enabled,
            "betting_enabled": self.betting_enabled,
            "ledger_mutations": self.ledger_mutations,
            "sealed_partition_mutations": self.sealed_partition_mutations,
            "production_mutations": dict(sorted(self.production_mutations.items())),
            "runtime_dirtiness": list(self.runtime_dirtiness),
            "source_dirtiness": list(self.source_dirtiness),
        }


@dataclass(frozen=True)
class Top5RuntimeHealthReport:
    """Deterministic result of one bounded offline readiness evaluation."""

    status: str
    checks: Mapping[str, bool]
    blockers: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    evidence: Top5RuntimeEvidence = field(default_factory=Top5RuntimeEvidence)

    @property
    def ready(self) -> bool:
        return self.status == READY_STATUS

    @property
    def offline_only(self) -> bool:
        return True

    def as_payload(self) -> dict[str, object]:
        try:
            evidence = self.evidence.as_payload()
        except Top5RuntimeHealthError:
            # A blocked report must remain serializable even when the caller
            # supplied malformed evidence.  The blocker carries the detail;
            # do not echo an unbounded or invalid evidence object.
            evidence = {"valid": False}
        return {
            "status": self.status,
            "ready": self.ready,
            "mode": OFFLINE_MODE,
            "checks": dict(sorted(self.checks.items())),
            "blockers": list(self.blockers),
            "warnings": list(self.warnings),
            "evidence": evidence,
        }


def _append_bounded(target: list[str], message: str) -> None:
    if len(target) < MAX_REASONS:
        target.append(message[:MAX_TEXT_LENGTH])


def _evaluate(evidence: Top5RuntimeEvidence) -> Top5RuntimeHealthReport:
    checks: dict[str, bool] = {}
    blockers: list[str] = []
    warnings: list[str] = []

    try:
        evidence.validate_shape()
    except Top5RuntimeHealthError as exc:
        return Top5RuntimeHealthReport(
            BLOCKED_STATUS,
            {"evidence_shape": False},
            (str(exc),),
            (),
            evidence,
        )

    checks["worker_health"] = evidence.worker_health in _HEALTHY_STATES
    checks["pwa_health"] = evidence.pwa_health in _HEALTHY_STATES
    checks["football_scheduler_offline"] = evidence.football_scheduler_state in {
        "inactive",
        "offline",
        "not_registered",
    }
    checks["launchd_expectations"] = bool(evidence.launchd_expectations) and all(
        ("top5" not in name.casefold() or state in _SAFE_UNREGISTERED_STATES)
        and state in _PRESENT_STATES | _SAFE_UNREGISTERED_STATES
        for name, state in evidence.launchd_expectations.items()
    )
    checks["runtime_writers_active"] = bool(evidence.runtime_writer_state) and all(
        state in _PRESENT_STATES for state in evidence.runtime_writer_state.values()
    )
    checks["ledger_writers_safe"] = bool(evidence.ledger_writer_state) and all(
        state in _SAFE_UNREGISTERED_STATES for state in evidence.ledger_writer_state.values()
    )
    checks["health_artifacts_observed"] = bool(evidence.football_health_artifacts) and all(
        state in _PRESENT_STATES | _SAFE_UNREGISTERED_STATES
        for state in evidence.football_health_artifacts.values()
    )
    checks["request_interfaces_present"] = bool(evidence.request_interfaces) and all(
        state in _INTERFACE_STATES for state in evidence.request_interfaces.values()
    )
    checks["quota_interfaces_present"] = bool(evidence.quota_interfaces) and all(
        state in _INTERFACE_STATES for state in evidence.quota_interfaces.values()
    )
    checks["publication_disabled"] = not evidence.publication_enabled
    checks["betting_disabled"] = not evidence.betting_enabled
    checks["ledger_untouched"] = evidence.ledger_mutations == 0
    checks["sealed_partition_untouched"] = evidence.sealed_partition_mutations == 0
    checks["source_clean"] = not evidence.source_dirtiness

    if evidence.runtime_dirtiness:
        warnings.append(
            f"runtime dirtiness is informational ({len(evidence.runtime_dirtiness)} bounded item(s))"
        )
    if evidence.source_dirtiness:
        _append_bounded(blockers, "unexpected source dirtiness is present")

    mutations = dict(evidence.production_mutations)
    mutations.setdefault("ledger", evidence.ledger_mutations)
    mutations.setdefault("sealed_partition", evidence.sealed_partition_mutations)
    checks["production_untouched"] = all(mutations.get(key, 0) == 0 for key in _MUTATION_KEYS)
    for key, value in sorted(mutations.items()):
        if value:
            _append_bounded(blockers, f"production mutation observed: {key}={value}")

    for name, passed in checks.items():
        if not passed:
            _append_bounded(blockers, f"check failed: {name}")

    # De-duplicate while preserving deterministic insertion order and the
    # bounded reason budget.
    blockers = list(dict.fromkeys(blockers))[:MAX_REASONS]
    warnings = list(dict.fromkeys(warnings))[:MAX_REASONS]
    return Top5RuntimeHealthReport(
        READY_STATUS if not blockers else BLOCKED_STATUS,
        checks,
        tuple(blockers),
        tuple(warnings),
        evidence,
    )


def assess_top5_runtime_health(
    evidence: Top5RuntimeEvidence | Mapping[str, object],
) -> Top5RuntimeHealthReport:
    """Evaluate supplied evidence without performing any external action."""

    try:
        normalized = (
            evidence
            if isinstance(evidence, Top5RuntimeEvidence)
            else Top5RuntimeEvidence.from_mapping(evidence)
        )
        return _evaluate(normalized)
    except (Top5RuntimeHealthError, TypeError, ValueError) as exc:
        fallback = Top5RuntimeEvidence()
        return Top5RuntimeHealthReport(
            BLOCKED_STATUS,
            {"evidence_shape": False},
            (str(exc)[:MAX_TEXT_LENGTH],),
            (),
            fallback,
        )


def _load_manifest(path: Path) -> Mapping[str, object]:
    try:
        if path.stat().st_size > MAX_MANIFEST_BYTES:
            raise Top5RuntimeHealthError("launchd expectation manifest exceeds the bounded size")
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise Top5RuntimeHealthError(f"unable to read launchd expectation manifest: {exc}") from exc
    if not isinstance(payload, Mapping):
        raise Top5RuntimeHealthError("launchd expectation manifest must be an object")
    return payload


def collect_offline_runtime_evidence(
    repo_root: str | Path,
    *,
    runtime_dirtiness: Sequence[str] = (),
    source_dirtiness: Sequence[str] = (),
) -> Top5RuntimeEvidence:
    """Inventory repository contracts using bounded, read-only presence checks."""

    root = Path(repo_root).expanduser().resolve()
    manifest_path = root / "launchd" / "top5" / "runtime_health_expectations.json"
    manifest: Mapping[str, object] = {}
    if manifest_path.is_file():
        manifest = _load_manifest(manifest_path)

    if manifest and manifest.get("mode") != OFFLINE_MODE:
        raise Top5RuntimeHealthError("launchd expectation manifest is not offline-only")
    declared_scheduler = manifest.get("top5_scheduler_registered", False)
    declared_publication = manifest.get("top5_publication_registered", False)
    declared_ledger_writer = manifest.get("top5_ledger_writer_registered", False)
    _bool("top5_scheduler_registered", declared_scheduler)
    _bool("top5_publication_registered", declared_publication)
    _bool("top5_ledger_writer_registered", declared_ledger_writer)

    top5_launchd_dir = root / "launchd" / "top5"
    workflow_dir = root / ".github" / "workflows"
    has_top5_scheduler_file = top5_launchd_dir.is_dir() and any(
        path.is_file() for path in top5_launchd_dir.glob("*.plist")
    )
    has_top5_workflow = workflow_dir.is_dir() and any(
        path.is_file() for path in workflow_dir.glob("*top5*")
    )
    top5_scheduler_state = (
        "active"
        if declared_scheduler or has_top5_scheduler_file or has_top5_workflow
        else "not_registered"
    )

    expected_jobs = _bounded_map(
        "expected_existing_jobs", manifest.get("expected_existing_jobs") or {}
    )
    launchd_expectations = {
        **expected_jobs,
        "top5_runtime_scheduler": "not_registered" if top5_scheduler_state == "not_registered" else "active",
    }

    runtime_writer_state = {
        "health_writer": "active" if (root / "src" / "monitoring" / "health_writer.py").is_file() else "missing",
        "aggregate_health": "active" if (root / "src" / "monitoring" / "aggregate_health.py").is_file() else "missing",
        "runtime_artifact_publisher": "active" if (root / "scripts" / "publish_runtime_artifacts.sh").is_file() else "missing",
    }
    ledger_writer_state = {
        "top5_ledger_writer": "active" if declared_ledger_writer else "not_registered",
        # The private ledger is intentionally not opened or resolved here.
        "private_ledger": "read_only",
    }
    football_health_artifacts = {
        "aggregate_health_snapshot": _path_state(root, "docs/data/health.json"),
        "health_snapshot_directory": "present" if (root / "results" / "health").is_dir() else "missing",
        "top5_health_artifact": "not_registered",
    }
    request_interfaces = {
        "top5_contracts": _path_state(root, "src/football/production_contracts.py"),
        "top5_bulk_request": _path_state(root, "src/football/top5_dispatch.py"),
    }
    quota_interfaces = {
        "top5_quota_contract": _path_state(root, "src/football/top5_quota.py"),
        "provider_budget_contract": _path_state(root, "src/signals/provider_budget.py"),
    }

    return Top5RuntimeEvidence(
        worker_health=(
            "ok"
            if (root / "cloudflare" / "worker.js").is_file()
            and (root / "cloudflare" / "contract.js").is_file()
            else "missing"
        ),
        pwa_health=(
            "ok"
            if all(
                (root / relative).is_file()
                for relative in ("docs/index.html", "docs/js/app.js", "docs/sw.js")
            )
            else "missing"
        ),
        football_scheduler_state=top5_scheduler_state,
        launchd_expectations=launchd_expectations,
        runtime_writer_state=runtime_writer_state,
        ledger_writer_state=ledger_writer_state,
        football_health_artifacts=football_health_artifacts,
        request_interfaces=request_interfaces,
        quota_interfaces=quota_interfaces,
        publication_enabled=bool(declared_publication),
        runtime_dirtiness=_bounded_strings("runtime_dirtiness", runtime_dirtiness),
        source_dirtiness=_bounded_strings("source_dirtiness", source_dirtiness),
    )


def run_offline_runtime_health(
    repo_root: str | Path,
    *,
    runtime_dirtiness: Sequence[str] = (),
    source_dirtiness: Sequence[str] = (),
) -> Top5RuntimeHealthReport:
    """Collect and evaluate one repository-local offline readiness snapshot."""

    try:
        evidence = collect_offline_runtime_evidence(
            repo_root,
            runtime_dirtiness=runtime_dirtiness,
            source_dirtiness=source_dirtiness,
        )
    except (Top5RuntimeHealthError, OSError, ValueError) as exc:
        return Top5RuntimeHealthReport(
            BLOCKED_STATUS,
            {"inventory_collected": False},
            (str(exc)[:MAX_TEXT_LENGTH],),
            (),
            Top5RuntimeEvidence(),
        )
    report = assess_top5_runtime_health(evidence)
    checks = {"inventory_collected": True, **report.checks}
    return Top5RuntimeHealthReport(
        report.status,
        checks,
        report.blockers,
        report.warnings,
        report.evidence,
    )


# Short aliases make the contract convenient for health-check callers while
# preserving the explicit function names used by the Night Shift script.
evaluate_runtime_health = assess_top5_runtime_health
collect_runtime_evidence = collect_offline_runtime_evidence
