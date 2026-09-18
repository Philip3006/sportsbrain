"""Offline Champions League research-readiness and evidence contracts.

This module deliberately does not fetch data, train production models, call a
provider, or write to runtime/financial stores.  It makes the research gate
explicit: season partitions are disjoint, feature timestamps are checked
against kickoff, metrics are computed only from caller-supplied predictions,
and candidate artifacts are hashed from an allow-list.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from itertools import pairwise
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "champions-league-research-readiness-v1"
COMPETITION = "UEFA Champions League"
OUTCOME_ORDER = ("away", "draw", "home")
_SEASON_RE = re.compile(r"^(\d{4})[-/](\d{2}|\d{4})$")


class ResearchReadinessError(ValueError):
    """Raised when research evidence is incomplete or unsafe to score."""


def canonical_season(value: object) -> str:
    text = str(value).strip().replace("/", "-")
    match = _SEASON_RE.fullmatch(text)
    if not match:
        raise ResearchReadinessError(f"invalid season label: {value!r}")
    start = int(match.group(1))
    end = int(match.group(2))
    if end >= 100:
        end %= 100
    if end != (start + 1) % 100:
        raise ResearchReadinessError(f"season is not consecutive: {value!r}")
    return f"{start:04d}-{end:02d}"


def _season_key(value: str) -> int:
    return int(canonical_season(value)[:4])


@dataclass(frozen=True)
class PartitionSpec:
    """Chronological, mutually-exclusive model-development partitions."""

    development: tuple[str, ...]
    calibration: tuple[str, ...]
    final: tuple[str, ...]
    shadow: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        groups = {
            "development": self.development,
            "calibration": self.calibration,
            "final": self.final,
            "shadow": self.shadow,
        }
        seen: dict[str, str] = {}
        for group, seasons in groups.items():
            if group != "shadow" and not seasons:
                raise ResearchReadinessError(f"{group} partition cannot be empty")
            for raw in seasons:
                season = canonical_season(raw)
                if season in seen:
                    raise ResearchReadinessError(
                        f"season {season} appears in {seen[season]} and {group}"
                    )
                seen[season] = group
        ordered = sorted(((_season_key(s), g) for s, g in seen.items()))
        rank = {"development": 0, "calibration": 1, "final": 2, "shadow": 3}
        if any(rank[left] > rank[right] for (_, left), (_, right) in pairwise(ordered)):
            raise ResearchReadinessError("partitions must be chronological")

    @classmethod
    def default(cls) -> PartitionSpec:
        return cls(
            development=tuple(f"{year:04d}-{(year + 1) % 100:02d}" for year in range(2015, 2022)),
            calibration=("2022-23", "2023-24"),
            final=("2024-25",),
            shadow=("2025-26",),
        )

    def label_for(self, season: object) -> str:
        value = canonical_season(season)
        for label in ("development", "calibration", "final", "shadow"):
            if value in {canonical_season(item) for item in getattr(self, label)}:
                return label
        raise ResearchReadinessError(
            f"season {value} is outside the sealed partition specification"
        )

    def as_payload(self) -> dict[str, Any]:
        return {
            "development": sorted(map(canonical_season, self.development), key=_season_key),
            "calibration": sorted(map(canonical_season, self.calibration), key=_season_key),
            "final": sorted(map(canonical_season, self.final), key=_season_key),
            "shadow": sorted(map(canonical_season, self.shadow), key=_season_key),
        }


def partition_matches(
    matches: Iterable[Mapping[str, Any]], spec: PartitionSpec | None = None
) -> dict[str, tuple[dict[str, Any], ...]]:
    """Assign and deterministically sort caller-supplied match rows."""
    spec = spec or PartitionSpec.default()
    rows: dict[str, list[dict[str, Any]]] = {k: [] for k in ("development", "calibration", "final", "shadow")}
    for index, raw in enumerate(matches):
        if "season" not in raw:
            raise ResearchReadinessError("every match row needs an explicit season")
        row = dict(raw)
        label = spec.label_for(row["season"])
        row["season"] = canonical_season(row["season"])
        row["partition"] = label
        row["_input_order"] = index
        rows[label].append(row)
    result: dict[str, tuple[dict[str, Any], ...]] = {}
    for label, values in rows.items():
        values.sort(key=lambda row: (
            _season_key(row["season"]),
            str(row.get("kickoff_at", row.get("date", ""))),
            str(row.get("match_id", "")),
            int(row["_input_order"]),
        ))
        for row in values:
            row.pop("_input_order", None)
        result[label] = tuple(values)
    return result


def _parse_timestamp(value: object) -> tuple[datetime | None, str]:
    if value is None or str(value).strip() == "":
        return None, "missing"
    text = str(value).strip()
    granularity = "date" if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text) else "instant"
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ResearchReadinessError(f"invalid timestamp: {value!r}") from exc
    if parsed.tzinfo is None:
        if granularity == "date":
            return parsed.replace(tzinfo=timezone.utc), "date"
        return parsed.replace(tzinfo=timezone.utc), "un-zoned"
    return parsed.astimezone(timezone.utc), granularity


def audit_feature_timestamps(
    observations: Iterable[Mapping[str, Any]],
    *,
    strict: bool = True,
) -> dict[str, Any]:
    """Audit point-in-time feature observations against their match cutoff.

    A source at or after kickoff is a hard leakage finding.  Date-only values
    are conditional in non-strict mode and blocked in strict mode because a
    post-kickoff publication on the same calendar day cannot be ruled out.
    """
    findings: list[dict[str, Any]] = []
    for item in observations:
        feature = str(item.get("feature_id", "unknown"))
        cutoff, cutoff_granularity = _parse_timestamp(item.get("cutoff_timestamp"))
        source, source_granularity = _parse_timestamp(item.get("source_timestamp"))
        status = "PASS"
        reason = "source timestamp is strictly before kickoff"
        if cutoff is None:
            status, reason = "BLOCKED", "kickoff timestamp is missing"
        elif source is None:
            status, reason = "BLOCKED", "feature source timestamp is missing"
        elif source_granularity == "un-zoned" or cutoff_granularity == "un-zoned":
            status, reason = "BLOCKED", "timestamps must include an explicit timezone"
        elif source >= cutoff:
            status, reason = "BLOCKED", "source is at or after kickoff (future-information risk)"
        elif source_granularity == "date":
            status = "BLOCKED" if strict else "CONDITIONAL"
            reason = "date-only source cannot prove pre-kickoff publication"
        findings.append({
            "feature_id": feature,
            "source_id": str(item.get("source_id", "unknown")),
            "status": status,
            "reason": reason,
            "source_timestamp": source.isoformat() if source else None,
            "cutoff_timestamp": cutoff.isoformat() if cutoff else None,
        })
    blocked = sum(f["status"] == "BLOCKED" for f in findings)
    conditional = sum(f["status"] == "CONDITIONAL" for f in findings)
    return {
        "status": "PASS" if not blocked and not conditional else "BLOCKED",
        "strict": strict,
        "observation_count": len(findings),
        "blocked_count": blocked,
        "conditional_count": conditional,
        "findings": findings,
    }


STATIC_FEATURE_TIMESTAMP_AUDIT: tuple[dict[str, str], ...] = (
    {
        "feature_id": "rolling_form_momentum_load_h2h",
        "status": "CONDITIONAL",
        "source": "src/features/form.py,src/features/head_to_head.py",
        "reason": "uses date < match_date; exact event-time ordering is not retained",
    },
    {
        "feature_id": "elo_pre_match_state",
        "status": "CONDITIONAL",
        "source": "src/features/builder.py,src/models/elo.py",
        "reason": "pre-match state is causal only at date granularity",
    },
    {
        "feature_id": "dixon_coles_snapshot",
        "status": "BLOCKED",
        "source": "src/features/builder.py,src/models/dixon_coles.py",
        "reason": "snapshot lookup uses <= match_date; no kickoff-safe snapshot contract",
    },
    {
        "feature_id": "market_odds",
        "status": "BLOCKED",
        "source": "src/features/builder.py,src/betting/odds_utils.py",
        "reason": "odds lookup has no captured_at/source_timestamp and can be closing or post-kickoff",
    },
    {
        "feature_id": "market_value",
        "status": "BLOCKED",
        "source": "src/data/market_values.py",
        "reason": "hard-coded current values are not historical as-of snapshots",
    },
    {
        "feature_id": "squad_availability",
        "status": "BLOCKED",
        "source": "src/data/squad_availability.py",
        "reason": "default/current squad caches do not carry an event-time release attestation",
    },
    {
        "feature_id": "statsbomb_xg_player_xg",
        "status": "BLOCKED",
        "source": "src/data/statsbomb.py",
        "reason": "match date is retained but publication timestamp is not",
    },
    {
        "feature_id": "fotmob_ratings",
        "status": "BLOCKED",
        "source": "src/data/fotmob.py",
        "reason": "ratings are post-match observations with date-only timestamps",
    },
    {
        "feature_id": "ppda",
        "status": "BLOCKED",
        "source": "src/features/ppda.py",
        "reason": "FBref season fallback is not point-in-time versioned",
    },
    {
        "feature_id": "fixture_context",
        "status": "PASS",
        "source": "src/features/builder.py",
        "reason": "neutral/tournament fields are fixture metadata supplied before scoring",
    },
)


def _row_records(value: Any) -> list[Mapping[str, Any]]:
    if hasattr(value, "to_dict"):
        return list(value.to_dict(orient="records"))
    return list(value)


def _outcome(row: Mapping[str, Any]) -> str:
    if "outcome" in row:
        outcome = str(row["outcome"]).lower()
        aliases = {"h": "home", "d": "draw", "a": "away", "home_win": "home", "away_win": "away"}
        outcome = aliases.get(outcome, outcome)
        if outcome in OUTCOME_ORDER:
            return outcome
    try:
        home, away = float(row["home_score"]), float(row["away_score"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ResearchReadinessError("prediction row needs outcome or home/away scores") from exc
    return "home" if home > away else "away" if away > home else "draw"


def _probabilities(row: Mapping[str, Any]) -> tuple[float, float, float]:
    values = tuple(float(row[f"p_{outcome}"]) for outcome in OUTCOME_ORDER)
    if any(not math.isfinite(v) or v < 0.0 for v in values):
        raise ResearchReadinessError("prediction probabilities must be finite and non-negative")
    total = sum(values)
    if total <= 0.0:
        raise ResearchReadinessError("prediction probabilities must have positive mass")
    return tuple(v / total for v in values)


def _calibration(rows: Sequence[tuple[tuple[float, float, float], str]]) -> tuple[float, float]:
    bins: list[list[tuple[float, bool]]] = [[] for _ in range(10)]
    absolute: list[float] = []
    for probabilities, actual in rows:
        predicted = max(range(3), key=probabilities.__getitem__)
        correct = OUTCOME_ORDER[predicted] == actual
        bins[min(9, int(max(probabilities) * 10))].append((max(probabilities), correct))
        absolute.extend(abs(probabilities[i] - float(OUTCOME_ORDER[i] == actual)) for i in range(3))
    ece = 0.0
    total = len(rows)
    for bucket in bins:
        if bucket:
            ece += len(bucket) / total * abs(
                sum(p for p, _ in bucket) / len(bucket)
                - sum(c for _, c in bucket) / len(bucket)
            )
    return ece, sum(absolute) / (3 * total) if total else float("nan")


def evaluate_predictions(predictions: Any) -> dict[str, Any]:
    """Return deterministic log-loss and calibration metrics by candidate/season."""
    rows = _row_records(predictions)
    groups: dict[tuple[str, str], list[tuple[tuple[float, float, float], str]]] = {}
    for row in rows:
        candidate = str(row.get("candidate_id", row.get("model_id", "unknown")))
        season = canonical_season(row.get("season"))
        probabilities = _probabilities(row)
        actual = _outcome(row)
        groups.setdefault((candidate, season), []).append((probabilities, actual))
    metrics: list[dict[str, Any]] = []
    for (candidate, season), values in sorted(groups.items(), key=lambda pair: (pair[0][0], _season_key(pair[0][1]))):
        losses = [-math.log(max(p[OUTCOME_ORDER.index(actual)], 1e-15)) for p, actual in values]
        ece, mae = _calibration(values)
        metrics.append({
            "candidate_id": candidate,
            "season": season,
            "n": len(values),
            "log_loss": sum(losses) / len(losses),
            "expected_calibration_error": ece,
            "mean_absolute_calibration_error": mae,
            "selection_eligible": season not in PartitionSpec.default().final,
        })
    return {
        "status": "OK" if metrics else "BLOCKED_NO_PREDICTIONS",
        "metric_definition": "multiclass log loss; 10-bin confidence ECE; mean absolute class calibration error",
        "metrics_by_season": metrics,
    }


_CANDIDATE_ARTIFACTS: tuple[dict[str, Any], ...] = (
    {
        "candidate_id": "uniform_prior_v1",
        "family": "uniform baseline",
        "readiness": "READY_FOR_OFFLINE_BASELINE",
        "paths": (),
        "reason": "parameter-free reproducible sanity baseline",
    },
    {
        "candidate_id": "empirical_prior_v1",
        "family": "development empirical prior",
        "readiness": "READY_FOR_OFFLINE_BASELINE",
        "paths": (),
        "reason": "fit only on rows before the scored partition",
    },
    {
        "candidate_id": "elo_v1",
        "family": "chronological Elo",
        "readiness": "CONDITIONAL",
        "paths": ("src/models/elo.py",),
        "reason": "compatible once CL event-time results and neutral/venue metadata are supplied",
    },
    {
        "candidate_id": "dixon_coles_v1",
        "family": "Dixon-Coles goals",
        "readiness": "CONDITIONAL",
        "paths": ("src/models/dixon_coles.py",),
        "reason": "compatible once causal CL training rows and kickoff-safe snapshots are supplied",
    },
    {
        "candidate_id": "market_prior_v1",
        "family": "de-vigged 1X2 market prior",
        "readiness": "BLOCKED",
        "paths": ("src/betting/odds_utils.py",),
        "reason": "requires CL odds with captured_at <= kickoff; existing builder does not enforce this",
    },
    {
        "candidate_id": "hist_gbm_v1",
        "family": "histogram gradient boosting challenger",
        "readiness": "BLOCKED",
        "paths": ("src/models/lgbm_model.py", "src/features/builder.py"),
        "reason": "feature timestamps and historical as-of artifacts are not yet CL-safe",
    },
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_candidate_artifact_manifest(root: str | Path = ".") -> dict[str, Any]:
    """Build a stable, source-only candidate manifest; never reads runtime data."""
    base = Path(root)
    candidates: list[dict[str, Any]] = []
    for candidate in _CANDIDATE_ARTIFACTS:
        artifacts = []
        for relative in sorted(candidate["paths"]):
            path = base / relative
            artifacts.append({
                "path": relative,
                "present": path.is_file(),
                "sha256": _sha256(path) if path.is_file() else None,
            })
        candidates.append({**candidate, "paths": artifacts})
    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "competition": COMPETITION,
        "scope": "offline research shadow/evidence only",
        "safety": {"network_accessed": False, "production_mutated": False, "financial_data_accessed": False},
        "partition_spec": PartitionSpec.default().as_payload(),
        "final_partition": {"locked_for_selection": True, "scoring_allowed_after_freeze": True},
        "feature_timestamp_audit": [dict(item) for item in STATIC_FEATURE_TIMESTAMP_AUDIT],
        "input_contract": {
            "required": ["season", "kickoff_at", "home_team", "away_team", "home_score", "away_score"],
            "present_in_repository": False,
            "expected_location": "data/champions_league/",
        },
        "candidates": candidates,
        "limitations": [
            "No committed Champions League match/prediction artifact is present in this worktree.",
            "src/data/football_data.py only enumerates domestic football-data.co.uk leagues.",
            (
                "src/data/international.py is a network fetcher for broad international results "
                "and is not a CL point-in-time pipeline."
            ),
            "No candidate is production-enabled by this manifest.",
        ],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    payload["manifest_sha256"] = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    return payload


def build_readiness_report(
    matches: Any = (),
    predictions: Any = (),
    *,
    spec: PartitionSpec | None = None,
    root: str | Path = ".",
) -> dict[str, Any]:
    """Compose the bounded evidence report used by the offline CLI."""
    spec = spec or PartitionSpec.default()
    match_rows = _row_records(matches)
    required = {"season", "kickoff_at", "home_team", "away_team", "home_score", "away_score"}
    for index, row in enumerate(match_rows):
        missing = sorted(required - set(row))
        if missing:
            raise ResearchReadinessError(f"match row {index} is missing required fields: {missing}")
        _, granularity = _parse_timestamp(row["kickoff_at"])
        if granularity != "instant":
            raise ResearchReadinessError(f"match row {index} needs a timezone-aware kickoff_at")
    partitions = (
        partition_matches(match_rows, spec)
        if match_rows
        else {k: () for k in ("development", "calibration", "final", "shadow")}
    )
    prediction_rows = _row_records(predictions)
    metric_report = (
        evaluate_predictions(prediction_rows)
        if prediction_rows
        else {"status": "BLOCKED_NO_PREDICTIONS", "metrics_by_season": []}
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "competition": COMPETITION,
        "status": (
            "READY_FOR_OFFLINE_EVIDENCE"
            if match_rows and metric_report["status"] == "OK"
            else "BLOCKED_NO_CL_DATA_OR_PREDICTIONS"
        ),
        "partition_counts": {key: len(value) for key, value in partitions.items()},
        "partition_spec": spec.as_payload(),
        "feature_timestamp_audit": {
            "status": "BLOCKED",
            "findings": [dict(item) for item in STATIC_FEATURE_TIMESTAMP_AUDIT],
        },
        "evaluation": metric_report,
        "candidate_manifest": build_candidate_artifact_manifest(root),
    }


__all__ = [
    "COMPETITION",
    "OUTCOME_ORDER",
    "STATIC_FEATURE_TIMESTAMP_AUDIT",
    "PartitionSpec",
    "ResearchReadinessError",
    "audit_feature_timestamps",
    "build_candidate_artifact_manifest",
    "build_readiness_report",
    "canonical_season",
    "evaluate_predictions",
    "partition_matches",
]
