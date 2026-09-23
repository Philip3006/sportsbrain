"""Strict Champions League publication-contract regressions."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone

import pytest

from src.football.champions_league_publication import (
    ChampionsLeaguePublicationError,
    validate_champions_league_publication,
)

NOW = datetime(2026, 9, 23, 12, 10, tzinfo=timezone.utc)
_DIGESTS = {
    "source_sha": "a" * 64,
    "research_sha": "b" * 64,
    "model_artifact_hash": "c" * 64,
    "evidence_digest": "d" * 64,
}


def _record(market: str) -> dict[str, object]:
    return {
        "sport": "football",
        "league": "ucl",
        "fixture_key": "ucl:fixture:001",
        "match": "Home FC vs Away FC",
        "home": "Home FC",
        "away": "Away FC",
        "kickoff": "2026-09-23T16:00:00Z",
        "prediction_id": "ucl-prediction-001",
        "model_identity": "cl-model-v1",
        "prediction_timestamp": "2026-09-23T12:09:30Z",
        "source": "offline-fixture",
        "provider": "offline-fixture",
        "activation_state": "SHADOW",
        "publication_status": "UNPUBLISHED",
        "publication_enabled": False,
        "no_bet": True,
        "signal_status": "SHADOW",
        "result_status": "PENDING",
        "settlement_status": "PENDING",
        "stale_state": "FRESH",
        "market": market,
        "provenance": {
            "source": "offline-fixture",
            "provider": "offline-fixture",
            "snapshot_id": "ucl-snapshot-001",
            "snapshot_kind": "signal_time",
            "captured_at": "2026-09-23T12:09:30Z",
            "source_age_seconds": 30,
            **_DIGESTS,
        },
    }


def _product() -> dict[str, object]:
    return {
        "updated": "2026-09-23T12:09:30Z",
        "football": [_record(market) for market in ("home", "draw", "away")],
        "champions_league_release": {
            "schema_version": "champions-league-publication-v1",
            "competition": "UEFA Champions League",
            "league_code": "ucl",
            "generation_id": "ucl-generation-001",
            "activation_state": "SHADOW",
            "publication_status": "UNPUBLISHED",
            "publication_enabled": False,
            "provider_authority": "offline-fixture",
            "result_authority": "offline-result-fixture",
            "prediction_count": 1,
            "fixture_count": 1,
            "generated_at": "2026-09-23T12:09:30Z",
            "stale_after_seconds": 900,
            "no_bet": True,
            **{key: _DIGESTS[key] for key in _DIGESTS if key != "evidence_digest"},
        },
        "health": {
            "football_releases": [
                {
                    "schema_version": "football-release-health-v1",
                    "league": "ucl",
                    "publication_status": "UNPUBLISHED",
                    "stale_artifact": False,
                    "missing_result_count": 1,
                    "settlement_status": "PENDING",
                    "source_age_seconds": 30,
                    "source": "offline-fixture",
                    "provider": "offline-fixture",
                    "source_sha": _DIGESTS["source_sha"],
                    "activation_state": "SHADOW",
                    "no_bet": True,
                    "publication_enabled": False,
                    "observed_at": "2026-09-23T12:09:30Z",
                }
            ]
        },
    }


def test_valid_cl_product_requires_three_markets_and_full_provenance() -> None:
    facts = validate_champions_league_publication(
        _product(),
        now=NOW,
        max_age_seconds=900,
        require_release=True,
        require_health=True,
    )

    assert facts == {
        "schema_version": "champions-league-publication-v1",
        "league_code": "ucl",
        "prediction_count": 1,
        "fixture_count": 1,
        "publication_enabled": False,
        "publication_status": "UNPUBLISHED",
    }


@pytest.mark.parametrize(
    "mutate",
    (
        lambda product: product["football"][0]["provenance"].pop("evidence_digest"),
        lambda product: product["football"].pop(),
        lambda product: product["football"][0].update({"signal_status": "ACTIVE"}),
        lambda product: product["football"][0].update({"no_bet": False}),
    ),
)
def test_cl_contract_fails_closed_for_incomplete_or_actionable_data(mutate) -> None:
    product = deepcopy(_product())
    mutate(product)

    with pytest.raises(ChampionsLeaguePublicationError):
        validate_champions_league_publication(
            product,
            now=NOW,
            max_age_seconds=900,
            require_release=True,
            require_health=True,
        )


def test_published_stale_cl_data_is_rejected() -> None:
    product = deepcopy(_product())
    product["champions_league_release"].update(
        {
            "activation_state": "CONTROLLED",
            "publication_status": "PUBLISHED",
            "publication_enabled": True,
            "publication_authorization_id": "offline-test-authorization",
        }
    )
    for record in product["football"]:
        record.update(
            {
                "activation_state": "CONTROLLED",
                "publication_status": "PUBLISHED",
                "publication_enabled": True,
                "stale_state": "STALE",
                "signal_status": "CONTROLLED",
            }
        )
    product["health"]["football_releases"][0].update(
        {
            "publication_status": "PUBLISHED",
            "activation_state": "CONTROLLED",
            "publication_enabled": True,
        }
    )

    with pytest.raises(ChampionsLeaguePublicationError, match="stale"):
        validate_champions_league_publication(
            product,
            now=NOW,
            max_age_seconds=900,
            require_release=True,
            require_health=True,
        )


def test_published_cl_release_requires_authorization_id() -> None:
    product = deepcopy(_product())
    product["champions_league_release"].update(
        {
            "activation_state": "CONTROLLED",
            "publication_status": "PUBLISHED",
            "publication_enabled": True,
        }
    )

    with pytest.raises(ChampionsLeaguePublicationError, match="authorization"):
        validate_champions_league_publication(
            product,
            now=NOW,
            max_age_seconds=900,
            require_release=True,
            require_health=True,
        )


def test_release_stale_after_seconds_is_default_freshness_limit() -> None:
    with pytest.raises(ChampionsLeaguePublicationError, match="stale"):
        validate_champions_league_publication(
            _product(),
            now=datetime(2026, 9, 23, 12, 30, tzinfo=timezone.utc),
            require_release=True,
            require_health=True,
        )
