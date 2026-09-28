"""Safely republish canonical local SportsBrain state to the signals Worker.

This recovery path deliberately performs no sports-provider requests. It reads
the existing public/per-user snapshots, validates the Nations League shadow,
and delegates reconstruction and upload to the canonical dashboard writer.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import os
import re
import subprocess
import sys
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import requests

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
INCIDENT_NL_PUBLIC_DIGEST = (
    "59f5aa67e18c89177e24824473b36040d5508094871cadaee43ba9a1478b125a"
)
INCIDENT_NL_FIXTURE_COUNT = 46
READY = "SPORTSBRAIN_WORKER_REPUBLISH_READY"
BLOCKED = "SPORTSBRAIN_WORKER_REPUBLISH_BLOCKED"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_NL_SCHEMA = "nations-league-public-v1"
_NL_SPORT_KEY = "soccer_uefa_nations_league"


class RepublishBlocked(Exception):
    """A fail-closed condition; details are intentionally not printed."""


def _canonical_hash(value: object) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _source_main_sha(root: Path) -> str | None:
    try:
        value = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            stderr=subprocess.DEVNULL,
            timeout=5,
            text=True,
        ).strip()
    except (OSError, subprocess.SubprocessError):
        return None
    return value if re.fullmatch(r"[0-9a-f]{40}", value) else None


def _worker_url(value: str | None) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RepublishBlocked
    url = value.strip()
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or not parsed.path.endswith("/signals.json")
    ):
        raise RepublishBlocked
    return url


def _read_json_object(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RepublishBlocked from exc
    if not isinstance(value, dict):
        raise RepublishBlocked
    return value


def _public_array(snapshot: Mapping, key: str) -> list[dict]:
    value = snapshot.get(key)
    if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
        raise RepublishBlocked
    return value


def _timestamp(value: object) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise RepublishBlocked
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RepublishBlocked from exc
    if parsed.tzinfo is None:
        raise RepublishBlocked
    return parsed.astimezone(timezone.utc)


def _validated_nl(value: object, expected_digest: str, now: datetime) -> dict:
    from src.notifications.nations_league_public import (
        NationsLeaguePublicError,
        validate_public_nations_league,
    )

    try:
        public = validate_public_nations_league(value, now=now)
    except NationsLeaguePublicError as exc:
        raise RepublishBlocked from exc
    if (
        public.get("schema") != _NL_SCHEMA
        or public.get("lifecycle") != "SHADOW_ONLY"
        or public.get("evidence_status") != "WEAK_EVIDENCE_SHADOW_ONLY"
        or public.get("no_bet") is not True
        or public.get("publication_enabled") is not False
        or not isinstance(public.get("fixture_count"), int)
        or isinstance(public.get("fixture_count"), bool)
        or public["fixture_count"] < 1
        or public.get("public_digest") != expected_digest
    ):
        raise RepublishBlocked
    if (
        expected_digest == INCIDENT_NL_PUBLIC_DIGEST
        and public["fixture_count"] != INCIDENT_NL_FIXTURE_COUNT
    ):
        raise RepublishBlocked
    return public


def _normal_team(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.casefold().split())


def _row_teams(row: Mapping) -> tuple[str, str]:
    home = row.get("home", row.get("home_team", row.get("homeName", "")))
    away = row.get("away", row.get("away_team", row.get("awayName", "")))
    match = row.get("match")
    if (not home or not away) and isinstance(match, str) and " vs " in match:
        home, away = match.split(" vs ", 1)
    return _normal_team(home), _normal_team(away)


def _no_network_wm_scores(*_args, **_kwargs) -> list:
    """Replacement for the writer's single embedded provider fetch."""
    return []


def _row_kickoff(row: Mapping) -> datetime | None:
    for key in ("kickoff", "commence_time", "start_time", "match_time"):
        value = row.get(key)
        if value is not None:
            try:
                return _timestamp(value)
            except RepublishBlocked:
                return None
    return None


def _contains_nl_fixture(football: list[dict], public_nl: Mapping) -> bool:
    fixture_ids = {
        fixture.get("provider_event_id")
        for fixture in public_nl.get("fixtures", [])
        if isinstance(fixture, Mapping)
    }
    fixture_pairs = {
        (
            _normal_team(fixture.get("home")),
            _normal_team(fixture.get("away")),
        )
        for fixture in public_nl.get("fixtures", [])
        if isinstance(fixture, Mapping)
    }
    for row in football:
        for field in (
            "competition",
            "tournament",
            "league",
            "league_name",
            "sport",
            "sport_key",
        ):
            label = row.get(field)
            if isinstance(label, str) and (
                "nations league" in label.casefold()
                or label.casefold() == _NL_SPORT_KEY
            ):
                return True
        if any(
            row.get(field) in fixture_ids for field in ("provider_event_id", "event_id")
        ):
            return True
        home, away = _row_teams(row)
        kickoff = _row_kickoff(row)
        if home and away and (home, away) in fixture_pairs:
            if kickoff is None:
                return True
            if any(
                _normal_team(fixture.get("home")) == home
                and _normal_team(fixture.get("away")) == away
                and _row_kickoff(fixture) == kickoff
                for fixture in public_nl.get("fixtures", [])
                if isinstance(fixture, Mapping)
            ):
                return True
    return False


def _worker_get(url: str):
    # No Authorization header is ever sent to the public endpoint.
    return requests.get(url, timeout=10, allow_redirects=False)


def _response_document(response) -> dict:
    if response.status_code != 200:
        raise RepublishBlocked
    try:
        value = response.json()
    except Exception as exc:
        raise RepublishBlocked from exc
    if not isinstance(value, dict):
        raise RepublishBlocked
    return value


def _record_worker(summary: dict, suffix: str, response, document: Mapping) -> None:
    summary[f"worker_http_status_{suffix}"] = response.status_code
    updated = document.get("updated")
    summary[f"worker_updated_{suffix}"] = updated if isinstance(updated, str) else None
    football = document.get("football")
    tennis = document.get("tennis")
    summary[f"football_count_{suffix}"] = (
        len(football) if isinstance(football, list) else None
    )
    summary[f"tennis_count_{suffix}"] = (
        len(tennis) if isinstance(tennis, list) else None
    )
    nl_present = "nations_league" in document
    summary[f"nl_present_{suffix}"] = nl_present
    nl = document.get("nations_league")
    summary[f"nl_digest_{suffix}"] = (
        nl.get("public_digest") if isinstance(nl, Mapping) else None
    )
    summary[f"nl_fixture_count_{suffix}"] = (
        nl.get("fixture_count") if isinstance(nl, Mapping) else None
    )


def _main_summary(root: Path) -> dict:
    return {
        "status": BLOCKED,
        "source_main_sha": _source_main_sha(root),
        "worker_http_status_before": None,
        "worker_http_status_after": None,
        "worker_updated_before": None,
        "worker_updated_after": None,
        "football_count_before": None,
        "football_count_after": None,
        "tennis_count_before": None,
        "tennis_count_after": None,
        "nl_present_before": None,
        "nl_present_after": None,
        "nl_digest_before": None,
        "nl_digest_after": None,
        "nl_fixture_count_before": None,
        "nl_fixture_count_after": None,
        "nl_fixture_count": None,
        "provider_requests": 0,
        "cloud_upload_success": False,
        "ledger_mutated": False,
        "betting_mutated": False,
        "activation_mutated": False,
    }


def republish(
    expected_nl_digest: str,
    *,
    root: Path = ROOT,
    now_fn=None,
    env: Mapping[str, str] | None = None,
) -> dict:
    """Republish existing snapshots or fail closed; never retries the POST."""
    summary = _main_summary(root)
    environment = os.environ if env is None else env
    current_time = now_fn or (lambda: datetime.now(timezone.utc))
    try:
        if not summary["source_main_sha"]:
            raise RepublishBlocked
        if not isinstance(expected_nl_digest, str) or not _SHA256.fullmatch(
            expected_nl_digest
        ):
            raise RepublishBlocked
        token = environment.get("SIGNALS_API_TOKEN", "")
        if not isinstance(token, str) or not token:
            raise RepublishBlocked
        url = _worker_url(environment.get("SIGNALS_CLOUD_URL"))

        before_response = _worker_get(url)
        summary["worker_http_status_before"] = before_response.status_code
        before = _response_document(before_response)
        _record_worker(summary, "before", before_response, before)
        before_updated = _timestamp(before.get("updated"))
        worker_football = _public_array(before, "football")
        worker_tennis = _public_array(before, "tennis")

        shared = _read_json_object(root / "docs" / "data" / "signals.json")
        per_user = _read_json_object(root / "docs" / "data" / "signals_philip.json")
        shared_football = _public_array(shared, "football")
        shared_tennis = _public_array(shared, "tennis")
        per_user_football = _public_array(per_user, "football")
        per_user_tennis = _public_array(per_user, "tennis")
        if (
            len(worker_football) != len(shared_football)
            or len(worker_football) != len(per_user_football)
            or len(worker_tennis) != len(shared_tennis)
            or len(worker_tennis) != len(per_user_tennis)
        ):
            raise RepublishBlocked

        public_nl = _validated_nl(
            shared.get("nations_league"),
            expected_nl_digest,
            current_time().astimezone(timezone.utc),
        )
        summary["nl_fixture_count"] = public_nl["fixture_count"]
        if _contains_nl_fixture(worker_football, public_nl):
            raise RepublishBlocked

        football_hash_before = _canonical_hash(worker_football)
        tennis_hash_before = _canonical_hash(worker_tennis)

        # The writer's one embedded score-provider fetch is neutralized before
        # the writer module is imported. Its deterministic empty response keeps
        # existing wm_results intact and cannot perform network I/O.
        from src.data import odds_api

        original_fetch_wm_scores = odds_api.fetch_wm_scores
        provider_fetch_invocations = 0

        def no_network_wm_scores(*_args, **_kwargs):
            nonlocal provider_fetch_invocations
            provider_fetch_invocations += 1
            return _no_network_wm_scores(*_args, **_kwargs)

        odds_api.fetch_wm_scores = no_network_wm_scores
        try:
            from src.notifications import web_dashboard

            original_root = web_dashboard.ROOT
            original_uploader = web_dashboard.upload_signals_to_cloud
            original_post = requests.post
            web_dashboard.ROOT = root
            upload_invocations = 0
            post_invocations = 0

            def guarded_uploader(*args, **kwargs):
                nonlocal upload_invocations
                payload = kwargs.get("payload")
                if not isinstance(payload, dict):
                    raise RepublishBlocked
                candidate_nl = _validated_nl(
                    payload.get("nations_league"),
                    expected_nl_digest,
                    current_time().astimezone(timezone.utc),
                )
                football_payload = _public_array(payload, "football")
                if _contains_nl_fixture(football_payload, candidate_nl):
                    raise RepublishBlocked
                upload_invocations += 1
                return original_uploader(*args, **kwargs)

            def single_no_redirect_post(*args, **kwargs):
                nonlocal post_invocations
                if post_invocations:
                    raise RepublishBlocked
                post_invocations += 1
                kwargs["allow_redirects"] = False
                return original_post(*args, **kwargs)

            web_dashboard.upload_signals_to_cloud = guarded_uploader
            requests.post = single_no_redirect_post
            try:
                # No football or tennis replacement arrays: preserve the
                # canonical per-user sections and shared NL fallback.
                with (
                    contextlib.redirect_stdout(io.StringIO()),
                    contextlib.redirect_stderr(io.StringIO()),
                ):
                    writer_ok = web_dashboard.write_signals_json()
            finally:
                web_dashboard.upload_signals_to_cloud = original_uploader
                web_dashboard.ROOT = original_root
                requests.post = original_post
        finally:
            odds_api.fetch_wm_scores = original_fetch_wm_scores

        if (
            writer_ok is not True
            or upload_invocations != 1
            or post_invocations != 1
            or provider_fetch_invocations != 1
        ):
            raise RepublishBlocked
        summary["cloud_upload_success"] = True

        after_response = _worker_get(url)
        summary["worker_http_status_after"] = after_response.status_code
        after = _response_document(after_response)
        _record_worker(summary, "after", after_response, after)
        after_updated = _timestamp(after.get("updated"))
        current_utc = current_time().astimezone(timezone.utc)
        after_football = _public_array(after, "football")
        after_tennis = _public_array(after, "tennis")
        after_nl = _validated_nl(
            after.get("nations_league"), expected_nl_digest, current_utc
        )
        age = current_utc - after_updated
        if (
            after_updated <= before_updated
            or age.total_seconds() < 0
            or age.total_seconds() > 120
            or _canonical_hash(after_football) != football_hash_before
            or _canonical_hash(after_tennis) != tennis_hash_before
            or after_nl["public_digest"] != expected_nl_digest
            or after_nl["fixture_count"] != public_nl["fixture_count"]
            or _contains_nl_fixture(after_football, after_nl)
        ):
            raise RepublishBlocked

        summary["status"] = READY
    except Exception:  # noqa: BLE001 - only a sanitized status is emitted.
        summary["status"] = BLOCKED
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(add_help=True)
    parser.add_argument("--expected-nl-digest")
    args = parser.parse_args(argv)
    summary = republish(args.expected_nl_digest or "", root=ROOT)
    print(json.dumps(summary, sort_keys=True, separators=(",", ":")))
    return 0 if summary["status"] == READY else 1


if __name__ == "__main__":
    sys.exit(main())
