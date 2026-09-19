"""Offline replay coverage and readiness projection for UCL evidence."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace

from src.football.champions_league_contracts import (
    CHAMPIONS_LEAGUE_CODE,
    CHAMPIONS_LEAGUE_SCHEMA_VERSION,
    ChampionsLeagueIntegrityPolicy,
)
from src.football.champions_league_integrity import (
    ChampionsLeagueIntegrityReport,
    _as_text,
    _fixture_key,
    _row_value,
    audit_champions_league_inputs,
)


@dataclass(frozen=True)
class ChampionsLeagueReplayCoverage:
    """Coverage metrics for a static UCL replay, never a performance claim."""

    candidate_fixtures: int
    legitimate_replay_inputs: int
    rejected_replay_inputs: int
    results_attached: int
    resolved_results: int
    odds_attached: int
    features_attached: int
    serializer_inputs_attached: int
    integrity_status: str
    issue_counts: Mapping[str, int]

    @property
    def performance_eligible_observations(self) -> int:
        return self.resolved_results

    def as_payload(self) -> dict[str, object]:
        return {
            "schema_version": CHAMPIONS_LEAGUE_SCHEMA_VERSION,
            "league": CHAMPIONS_LEAGUE_CODE,
            "candidate_fixtures": self.candidate_fixtures,
            "legitimate_replay_inputs": self.legitimate_replay_inputs,
            "rejected_replay_inputs": self.rejected_replay_inputs,
            "results_attached": self.results_attached,
            "resolved_results": self.resolved_results,
            "performance_eligible_observations": self.performance_eligible_observations,
            "odds_attached": self.odds_attached,
            "features_attached": self.features_attached,
            "serializer_inputs_attached": self.serializer_inputs_attached,
            "integrity_status": self.integrity_status,
            "issue_counts": dict(self.issue_counts),
            "offline_replay": True,
            "counts_as_real": False,
            "no_bet": True,
            "publication_enabled": False,
            "activation_state": "disabled",
        }


def build_champions_league_replay_coverage(
    fixtures: Sequence[Mapping[str, object]],
    *,
    results: Sequence[Mapping[str, object]] = (),
    odds: Sequence[Mapping[str, object]] = (),
    features: Sequence[Mapping[str, object]] = (),
    serializer_inputs: Sequence[Mapping[str, object]] = (),
    cache_records: Sequence[Mapping[str, object]] = (),
    policy: ChampionsLeagueIntegrityPolicy,
) -> tuple[ChampionsLeagueReplayCoverage, ChampionsLeagueIntegrityReport]:
    """Build honest replay coverage; unresolved results never become scores."""

    report = audit_champions_league_inputs(
        fixtures,
        results=results,
        odds=odds,
        features=features,
        serializer_inputs=serializer_inputs,
        cache_records=cache_records,
        policy=replace(policy, require_results=True),
    )
    fixture_keys = set(report.fixture_keys)
    attached = {
        source: {
            _fixture_key(row)
            for row in rows
            if isinstance(row, Mapping) and _fixture_key(row) in fixture_keys
        }
        for source, rows in (
            ("odds", odds),
            ("features", features),
            ("serializer", serializer_inputs),
        )
    }
    result_keys = {
        _fixture_key(row)
        for row in results
        if isinstance(row, Mapping) and _fixture_key(row) in fixture_keys
    }
    resolved_keys = {
        _fixture_key(row)
        for row in results
        if isinstance(row, Mapping)
        and _fixture_key(row) in fixture_keys
        and _as_text(_row_value(row, "status")).casefold()
        in {"final", "finished", "completed"}
    }
    issue_fixture_keys = {
        issue.fixture_key
        for issue in report.issues
        if issue.fixture_key and issue.source != "results"
    }
    legitimate = sum(
        key not in issue_fixture_keys
        and key in attached["odds"]
        and key in attached["features"]
        and key in attached["serializer"]
        for key in fixture_keys
    )
    resolved = len(resolved_keys)
    coverage = ChampionsLeagueReplayCoverage(
        candidate_fixtures=len(fixture_keys),
        legitimate_replay_inputs=legitimate,
        rejected_replay_inputs=len(fixture_keys) - legitimate,
        results_attached=len(result_keys),
        resolved_results=resolved,
        odds_attached=len(attached["odds"]),
        features_attached=len(attached["features"]),
        serializer_inputs_attached=len(attached["serializer"]),
        integrity_status="ready" if report.valid else "blocked",
        issue_counts=report.issue_counts,
    )
    return coverage, report


def build_champions_league_observability(
    report: ChampionsLeagueIntegrityReport,
    coverage: ChampionsLeagueReplayCoverage | None = None,
) -> dict[str, object]:
    """Return a bounded readiness payload suitable for an existing health writer."""

    payload = report.as_payload()
    payload["observability"] = {
        "integrity_status": "ready" if report.valid else "blocked",
        "issue_count": sum(report.issue_counts.values()),
        "issue_counts": dict(report.issue_counts),
        "bounded_issue_samples": len(report.issues),
    }
    if coverage is not None:
        payload["replay_coverage"] = coverage.as_payload()
    return payload
