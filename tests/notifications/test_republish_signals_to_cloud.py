from __future__ import annotations

import copy
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from scripts import republish_signals_to_cloud as republish_script
from src.notifications import nations_league_public
from tests.football.test_nations_league_public import _artifact, _public

WORKER_URL = "https://signals.example.test/api/signals.json"
TOKEN = "never-print-this-worker-token"
INCIDENT_DIGEST = "59f5aa67e18c89177e24824473b36040d5508094871cadaee43ba9a1478b125a"


class Response:
    def __init__(self, payload=None, *, status=200, text="worker response"):
        self.status_code = status
        self.text = text
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("invalid response")
        return self._payload


def _valid_public_nl(now: datetime, *, captured_at: datetime | None = None) -> dict:
    artifact, captured = _artifact(captured_at or now)
    public = _public(artifact, captured)
    fixtures = [copy.deepcopy(public["fixtures"][0])]
    for index in range(1, 46):
        fixture = copy.deepcopy(public["fixtures"][index % len(public["fixtures"])])
        fixture["provider_event_id"] = f"nl-provider-event-{index:03d}"
        fixture["kickoff"] = (
            (now + timedelta(days=1, minutes=index)).isoformat().replace("+00:00", "Z")
        )
        fixtures.append(fixture)
    fixtures[0]["kickoff"] = (
        (now + timedelta(hours=2)).isoformat().replace("+00:00", "Z")
    )
    public["fixtures"] = fixtures
    public["fixture_count"] = len(fixtures)
    public["public_digest"] = nations_league_public._public_digest(public)
    return public


def _worker_snapshot(now: datetime, *, football=None, tennis=None, nl=None) -> dict:
    result = {
        "updated": now.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "football": [] if football is None else football,
        "tennis": [] if tennis is None else tennis,
    }
    if nl is not None:
        result["nations_league"] = copy.deepcopy(nl)
    return result


def _prepare_run(
    tmp_path,
    monkeypatch,
    *,
    public_nl=None,
    worker_before=None,
    worker_after=None,
    local_football=None,
    local_tennis=None,
    post_status=200,
    now=None,
    expected_digest=None,
):
    now = now or datetime.now(timezone.utc).replace(microsecond=0)
    public_nl = public_nl or _valid_public_nl(now)
    expected_digest = expected_digest or public_nl["public_digest"]
    root = tmp_path / "repo"
    docs = root / "docs" / "data"
    docs.mkdir(parents=True)
    local_football = [] if local_football is None else local_football
    local_tennis = [] if local_tennis is None else local_tennis
    shared = {
        "updated": "before",
        "football": copy.deepcopy(local_football),
        "tennis": copy.deepcopy(local_tennis),
    }
    if public_nl is not False:
        shared["nations_league"] = copy.deepcopy(public_nl)
    per_user = {
        "updated": "before",
        "football": copy.deepcopy(local_football),
        "tennis": copy.deepcopy(local_tennis),
    }
    (docs / "signals.json").write_text(json.dumps(shared), encoding="utf-8")
    (docs / "signals_philip.json").write_text(json.dumps(per_user), encoding="utf-8")

    before = worker_before or _worker_snapshot(
        now - timedelta(minutes=5),
        football=copy.deepcopy(local_football),
        tennis=copy.deepcopy(local_tennis),
    )
    after = worker_after or _worker_snapshot(
        now - timedelta(seconds=5),
        football=copy.deepcopy(local_football),
        tennis=copy.deepcopy(local_tennis),
        nl=public_nl if public_nl is not False else None,
    )
    get_responses = [Response(before), Response(after)]
    calls = {
        "get": [],
        "post": [],
        "payload": None,
        "provider_urls": [],
        "safe_fetch": 0,
    }

    def fake_get(url, **kwargs):
        if url != WORKER_URL:
            calls["provider_urls"].append(url)
            raise AssertionError("unexpected non-Worker network request")
        calls["get"].append((url, kwargs))
        return get_responses.pop(0)

    def fake_post(url, **kwargs):
        calls["post"].append((url, kwargs))
        calls["payload"] = json.loads(kwargs["data"].decode("utf-8"))
        return Response(status=post_status, text="response contains no token")

    monkeypatch.setattr(republish_script.requests, "get", fake_get)
    monkeypatch.setattr(republish_script.requests, "post", fake_post)
    monkeypatch.setattr(republish_script, "_source_main_sha", lambda _root: "a" * 40)
    monkeypatch.setattr(republish_script, "ROOT", root)
    monkeypatch.setattr(
        republish_script,
        "_no_network_wm_scores",
        lambda *_args, **_kwargs: (
            calls.__setitem__("safe_fetch", calls["safe_fetch"] + 1) or []
        ),
    )
    monkeypatch.setenv("SIGNALS_CLOUD_URL", WORKER_URL)
    monkeypatch.setenv("SIGNALS_API_TOKEN", TOKEN)
    (tmp_path / "ledger").mkdir(exist_ok=True)
    monkeypatch.setenv("SPORTSBRAIN_LEDGER_DIR", str(tmp_path / "ledger"))
    monkeypatch.setenv(
        "SPORTSBRAIN_RUNTIME_ARTIFACT_STAGE_DIR", str(tmp_path / "staged-output")
    )

    return root, now, expected_digest, calls


def _run(root, now, expected_digest):
    return republish_script.republish(
        expected_digest,
        root=root,
        now_fn=lambda: now,
        env={
            "SIGNALS_CLOUD_URL": WORKER_URL,
            "SIGNALS_API_TOKEN": TOKEN,
        },
    )


def test_zero_worker_and_local_counts_republish_shared_nl_via_canonical_writer(
    tmp_path, monkeypatch, capsys
):
    _root, now, expected_digest, calls = _prepare_run(tmp_path, monkeypatch)

    exit_code = republish_script.main(["--expected-nl-digest", expected_digest])
    output = capsys.readouterr()
    summary = json.loads(output.out)

    assert exit_code == 0
    assert summary["status"] == republish_script.READY
    assert summary["source_main_sha"] == "a" * 40
    assert summary["worker_updated_before"] == (
        now - timedelta(minutes=5)
    ).isoformat().replace("+00:00", "Z")
    assert summary["worker_updated_after"] == (
        now - timedelta(seconds=5)
    ).isoformat().replace("+00:00", "Z")
    assert summary["football_count_before"] == summary["football_count_after"] == 0
    assert summary["tennis_count_before"] == summary["tennis_count_after"] == 0
    assert summary["nl_present_before"] is False
    assert summary["nl_present_after"] is True
    assert summary["nl_digest_after"] == expected_digest
    assert summary["nl_fixture_count"] == 46
    assert summary["provider_requests"] == 0
    assert summary["cloud_upload_success"] is True
    assert summary["ledger_mutated"] is False
    assert summary["betting_mutated"] is False
    assert summary["activation_mutated"] is False
    assert len(calls["get"]) == 2
    assert len(calls["post"]) == 1
    assert calls["safe_fetch"] == 1
    assert calls["provider_urls"] == []
    assert calls["payload"]["nations_league"]["public_digest"] == expected_digest
    assert calls["payload"]["football"] == []
    assert calls["payload"]["tennis"] == []
    assert calls["post"][0][0] == "https://signals.example.test/api/signals"
    assert calls["post"][0][1]["allow_redirects"] is False
    assert calls["post"][0][1]["headers"]["Authorization"] == f"Bearer {TOKEN}"
    assert all("headers" not in kwargs for _url, kwargs in calls["get"])
    assert TOKEN not in output.out + output.err
    assert WORKER_URL not in output.out + output.err
    assert "Authorization" not in output.out + output.err


@pytest.mark.parametrize("sport", ["football", "tennis"])
def test_worker_count_mismatch_blocks_before_post(sport, tmp_path, monkeypatch):
    now = datetime.now(timezone.utc).replace(microsecond=0)
    before = _worker_snapshot(now - timedelta(minutes=2))
    before[sport] = [{"signal_id": "worker-only"}]
    root, now, expected_digest, calls = _prepare_run(
        tmp_path, monkeypatch, worker_before=before, now=now
    )

    summary = _run(root, now, expected_digest)

    assert summary["status"] == republish_script.BLOCKED
    assert calls["post"] == []
    assert len(calls["get"]) == 1


def test_expected_nl_digest_mismatch_blocks_before_post(tmp_path, monkeypatch):
    root, now, _digest, calls = _prepare_run(
        tmp_path, monkeypatch, expected_digest="0" * 64
    )

    summary = _run(root, now, "0" * 64)

    assert summary["status"] == republish_script.BLOCKED
    assert calls["post"] == []


def test_nl_older_than_24_hours_blocks_before_post(tmp_path, monkeypatch):
    now = datetime.now(timezone.utc).replace(microsecond=0)
    stale = _valid_public_nl(now, captured_at=now - timedelta(hours=25))
    root, now, expected_digest, calls = _prepare_run(
        tmp_path, monkeypatch, public_nl=stale, now=now
    )

    summary = _run(root, now, expected_digest)

    assert summary["status"] == republish_script.BLOCKED
    assert calls["post"] == []


@pytest.mark.parametrize("tamper", ["digest", "safety"])
def test_malformed_or_tampered_nl_blocks_before_post(tamper, tmp_path, monkeypatch):
    now = datetime.now(timezone.utc).replace(microsecond=0)
    public = _valid_public_nl(now)
    if tamper == "digest":
        public["public_digest"] = "f" * 64
        expected = "f" * 64
    else:
        public["no_bet"] = False
        expected = public["public_digest"]
    root, now, expected_digest, calls = _prepare_run(
        tmp_path, monkeypatch, public_nl=public, now=now, expected_digest=expected
    )

    summary = _run(root, now, expected_digest)

    assert summary["status"] == republish_script.BLOCKED
    assert calls["post"] == []


def test_missing_nl_blocks_before_post(tmp_path, monkeypatch):
    root, now, expected_digest, calls = _prepare_run(
        tmp_path, monkeypatch, public_nl=False, expected_digest="1" * 64
    )

    summary = _run(root, now, expected_digest)

    assert summary["status"] == republish_script.BLOCKED
    assert calls["post"] == []


def test_post_failure_is_blocked_and_never_retried(tmp_path, monkeypatch):
    root, now, expected_digest, calls = _prepare_run(
        tmp_path, monkeypatch, post_status=500
    )

    summary = _run(root, now, expected_digest)

    assert summary["status"] == republish_script.BLOCKED
    assert summary["cloud_upload_success"] is False
    assert len(calls["post"]) == 1
    assert len(calls["get"]) == 1


@pytest.mark.parametrize("changed_array", ["football", "tennis"])
def test_post_upload_sport_array_hash_change_blocks(
    changed_array, tmp_path, monkeypatch
):
    now = datetime.now(timezone.utc).replace(microsecond=0)
    original = {
        "signal_id": "canonical-row",
        "sport": changed_array,
        "match": "Arsenal vs Chelsea"
        if changed_array == "football"
        else "Player A vs Player B",
        "kickoff": (now + timedelta(hours=2)).isoformat().replace("+00:00", "Z"),
    }
    changed = {**original, "signal_id": "different-row"}
    local_football = [original] if changed_array == "football" else []
    local_tennis = [original] if changed_array == "tennis" else []
    after = _worker_snapshot(
        now - timedelta(seconds=5),
        football=[changed] if changed_array == "football" else [],
        tennis=[changed] if changed_array == "tennis" else [],
    )
    root, now, expected_digest, calls = _prepare_run(
        tmp_path,
        monkeypatch,
        worker_after=after,
        local_football=local_football,
        local_tennis=local_tennis,
        now=now,
    )

    summary = _run(root, now, expected_digest)

    assert summary["status"] == republish_script.BLOCKED
    assert summary["cloud_upload_success"] is True
    assert len(calls["post"]) == 1
    assert len(calls["get"]) == 2


@pytest.mark.parametrize("post_nl", [None, "wrong_digest"])
def test_post_upload_missing_or_wrong_digest_nl_blocks(post_nl, tmp_path, monkeypatch):
    now = datetime.now(timezone.utc).replace(microsecond=0)
    public = _valid_public_nl(now)
    if post_nl == "wrong_digest":
        public["public_digest"] = "e" * 64
    after = _worker_snapshot(
        now - timedelta(seconds=5), nl=None if post_nl is None else public
    )
    root, now, expected_digest, calls = _prepare_run(
        tmp_path, monkeypatch, worker_after=after, now=now
    )

    summary = _run(root, now, expected_digest)

    assert summary["status"] == republish_script.BLOCKED
    assert summary["cloud_upload_success"] is True
    assert len(calls["post"]) == 1


def test_post_upload_timestamp_must_be_fresh(tmp_path, monkeypatch):
    now = datetime.now(timezone.utc).replace(microsecond=0)
    public = _valid_public_nl(now)
    after = _worker_snapshot(now - timedelta(seconds=121), nl=public)
    root, now, expected_digest, calls = _prepare_run(
        tmp_path, monkeypatch, worker_after=after, public_nl=public, now=now
    )

    summary = _run(root, now, expected_digest)

    assert summary["status"] == republish_script.BLOCKED
    assert summary["cloud_upload_success"] is True
    assert len(calls["post"]) == 1
    assert len(calls["get"]) == 2


def test_nations_league_fixture_cannot_enter_actionable_football(tmp_path, monkeypatch):
    now = datetime.now(timezone.utc).replace(microsecond=0)
    public = _valid_public_nl(now)
    fixture = public["fixtures"][0]
    actionable = {
        "home": fixture["home"],
        "away": fixture["away"],
        "kickoff": fixture["kickoff"],
    }
    root, now, expected_digest, calls = _prepare_run(
        tmp_path,
        monkeypatch,
        public_nl=public,
        worker_before=_worker_snapshot(
            now - timedelta(minutes=5), football=[actionable]
        ),
        now=now,
    )
    for filename in ("signals.json", "signals_philip.json"):
        path = root / "docs" / "data" / filename
        source = json.loads(path.read_text())
        source["football"] = [actionable]
        path.write_text(json.dumps(source))

    summary = _run(root, now, expected_digest)

    assert summary["status"] == republish_script.BLOCKED
    assert calls["post"] == []


def test_dispatch_workflow_is_manual_read_only_and_uses_only_required_secrets():
    workflow = (
        Path(__file__).resolve().parents[2]
        / ".github"
        / "workflows"
        / "signals_republish.yml"
    ).read_text(encoding="utf-8")
    assert "on:\n  workflow_dispatch:" in workflow
    assert "schedule:" not in workflow
    assert "repository_dispatch:" not in workflow
    assert "permissions:\n  contents: read" in workflow
    assert "required: true" in workflow
    assert "SIGNALS_CLOUD_URL: ${{ secrets.SIGNALS_CLOUD_URL }}" in workflow
    assert "SIGNALS_API_TOKEN: ${{ secrets.SIGNALS_API_TOKEN }}" in workflow
    assert "LEDGER_PRIVATE_READ_PAT" in workflow
    assert "SPORTSBRAIN_LEDGER_DIR" in workflow
    assert "ODDS_API_KEY" not in workflow
