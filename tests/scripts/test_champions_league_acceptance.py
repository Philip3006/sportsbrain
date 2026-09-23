"""Offline end-to-end acceptance checks for the CL public boundary."""

from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from scripts.champions_league_acceptance import evaluate_snapshot

FIXTURE = (
    Path(__file__).parents[1]
    / "fixtures"
    / "champions_league"
    / "publication_offline.json"
)
NOW = datetime(2026, 9, 23, 12, 10, tzinfo=timezone.utc)


def _snapshot() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_offline_snapshot_passes_full_cl_acceptance() -> None:
    result = evaluate_snapshot(_snapshot(), now=NOW, max_age_seconds=900)

    assert result["status"] == "CL_PUBLICATION_ACCEPTANCE_READY"
    assert result["schema_version"] == "champions-league-publication-v1"
    assert result["league_code"] == "UCL"
    assert result["prediction_count"] == 1
    assert result["fixture_count"] == 1
    assert result["publication_status"] == "UNPUBLISHED"
    assert result["publication_enabled"] is False


@pytest.mark.parametrize(
    "mutate",
    (
        lambda snapshot: snapshot["football"][0]["provenance"].pop("captured_at"),
        lambda snapshot: snapshot["health"]["football_releases"][0].pop("provider"),
    ),
)
def test_incomplete_provenance_fails_closed(mutate) -> None:
    broken = copy.deepcopy(_snapshot())
    mutate(broken)

    with pytest.raises(Exception, match="CL"):
        evaluate_snapshot(broken, now=NOW, max_age_seconds=900)


def test_stale_snapshot_fails_closed() -> None:
    with pytest.raises(Exception, match="stale"):
        evaluate_snapshot(
            _snapshot(),
            now=datetime(2026, 9, 23, 13, 0, tzinfo=timezone.utc),
            max_age_seconds=900,
        )


def test_publication_requires_explicit_authorization() -> None:
    snapshot = _snapshot()
    snapshot["champions_league_release"]["publication_enabled"] = True
    snapshot["champions_league_release"]["publication_status"] = "PUBLISHED"
    snapshot["champions_league_release"]["activation_state"] = "LIVE"
    snapshot["champions_league_release"]["publication_authorization_id"] = (
        "not-authorized-by-this-harness"
    )

    with pytest.raises(Exception, match="CL"):
        evaluate_snapshot(snapshot, now=NOW, max_age_seconds=900)
