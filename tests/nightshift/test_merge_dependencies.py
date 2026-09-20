from __future__ import annotations

import json
import subprocess
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from src.nightshift import (
    DeliveryError,
    DispatcherRecursionError,
    FakeExecutor,
    GhPullRequestClient,
    NightShiftDispatcher,
    SafetyViolation,
    TaskSpec,
    TaskState,
    UnknownBuilderError,
)

from .test_delivery import REPO, _code_task, _fixture


class _MergeVerifier:
    def __init__(
        self, evidence: dict[str, Any] | None = None, error: Exception | None = None
    ) -> None:
        self.evidence = evidence
        self.error = error
        self.calls = 0

    def verify_merged(self, task: Any) -> dict[str, Any]:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return dict(self.evidence or {})


def _parent_spec(base_sha: str, task_id: str, branch: str) -> TaskSpec:
    return replace(_code_task(base_sha), task_id=task_id, branch=branch)


def _child_spec(parent_id: str, base_sha: str, task_id: str) -> TaskSpec:
    return TaskSpec(
        task_id=task_id,
        builder_id="builder-2",
        objective="Review the completed parent evidence",
        branch="nightshift/builder-2/dependent-child",
        task_type="authority_review",
        risk_class="read_only",
        expected_base_sha=base_sha,
        dependency_ids=(parent_id,),
    )


def _make_pr_ready(
    dispatcher: NightShiftDispatcher,
    base_sha: str,
    *,
    task_id: str = "merge-parent-0001",
    branch: str = "nightshift/builder-1/gate-parent",
):
    task = _parent_spec(base_sha, task_id, branch)
    dispatcher.submit(task)
    dispatcher.approve(task.task_id, approver="operator")
    result = dispatcher.run_once(
        "builder-1", FakeExecutor(write_file="artifacts/change.txt")
    )
    assert result is not None and result.state is TaskState.PR_READY
    return result


def _merge_evidence(task: Any) -> dict[str, Any]:
    return {
        "source": "github-readonly",
        "repository": REPO,
        "merged": True,
        "merged_at": "2026-09-17T12:00:00Z",
        "pr_number": task.pr_number,
        "head_ref_name": task.branch,
        "head_ref_oid": task.commit_sha,
        "base_ref_name": task.base_branch,
        "base_ref_oid": task.base_sha,
        "merge_commit": {"oid": "f" * 40},
    }


def test_pr_ready_dependency_waits_for_merge(tmp_path: Path) -> None:
    dispatcher, _, base_sha, _ = _fixture(tmp_path)
    parent = _make_pr_ready(dispatcher, base_sha)
    child = dispatcher.submit(_child_spec(parent.task_id, base_sha, "merge-child-0001"))

    assert dispatcher.claim_next("builder-2") is None
    assert dispatcher.store.get(child.task_id).state is TaskState.WAITING_DEPENDENCY


def test_ceo_review_dependency_waits_for_merge(tmp_path: Path) -> None:
    dispatcher, _, base_sha, _ = _fixture(tmp_path)
    parent = _make_pr_ready(dispatcher, base_sha, task_id="ceo-parent-0001")
    dispatcher.mark_ceo_review(parent.task_id, actor="operator")
    child = dispatcher.submit(_child_spec(parent.task_id, base_sha, "ceo-child-0001"))

    assert dispatcher.claim_next("builder-2") is None
    assert dispatcher.store.get(child.task_id).state is TaskState.WAITING_DEPENDENCY


def test_independent_roadmap_item_runs_while_parent_waits(tmp_path: Path) -> None:
    dispatcher, _, base_sha, _ = _fixture(tmp_path)
    _make_pr_ready(dispatcher, base_sha, task_id="independent-parent-01")

    selected = dispatcher.select_next_roadmap_task(builder_id="builder-3")
    assert selected is not None
    assert selected.roadmap_item_id == "roadmap-b3-integration-1"
    assert selected.state is TaskState.BACKLOG


@pytest.mark.parametrize(
    "verifier",
    [
        _MergeVerifier({"merged": False}),
        _MergeVerifier(error=DeliveryError("github unavailable")),
    ],
)
def test_fake_or_unavailable_merge_evidence_fails_closed(
    tmp_path: Path, verifier: _MergeVerifier
) -> None:
    dispatcher, _, base_sha, _ = _fixture(tmp_path)
    parent = _make_pr_ready(dispatcher, base_sha, task_id="failed-merge-0001")
    child = dispatcher.submit(_child_spec(parent.task_id, base_sha, "failed-child-0001"))
    assert dispatcher.claim_next("builder-2") is None

    with pytest.raises(DeliveryError):
        dispatcher.reconcile_merged(parent.task_id, actor="operator", github=verifier)

    assert dispatcher.store.get(parent.task_id).state is TaskState.PR_READY
    assert dispatcher.store.get(child.task_id).state is TaskState.WAITING_DEPENDENCY


def test_exact_verified_merge_completes_parent_and_is_idempotent(tmp_path: Path) -> None:
    dispatcher, _, base_sha, _ = _fixture(tmp_path)
    parent = _make_pr_ready(dispatcher, base_sha, task_id="verified-merge-0001")
    verifier = _MergeVerifier(_merge_evidence(parent))

    with pytest.raises(SafetyViolation):
        dispatcher.store.transition(
            parent.task_id,
            expected=TaskState.PR_READY,
            new_state=TaskState.COMPLETED,
            actor="operator",
            event_type="completed",
        )

    completed = dispatcher.reconcile_merged(
        parent.task_id, actor="operator", github=verifier
    )
    again = dispatcher.reconcile_merged(
        parent.task_id, actor="operator", github=verifier
    )

    assert completed.state is TaskState.COMPLETED
    assert again == completed
    assert completed.delivery
    assert completed.delivery["merge_verification"]["merged"] is True
    assert verifier.calls == 1
    events = dispatcher.store.audit_events(task_id=parent.task_id, limit=1000)
    assert any(event.event_type == "merge_verified" for event in events)


def test_verified_merge_releases_dependent_without_duplicate_task(tmp_path: Path) -> None:
    dispatcher, _, base_sha, _ = _fixture(tmp_path)
    parent = _make_pr_ready(dispatcher, base_sha, task_id="release-parent-0001")
    child = dispatcher.submit(_child_spec(parent.task_id, base_sha, "release-child-0001"))
    assert dispatcher.claim_next("builder-2") is None

    dispatcher.reconcile_merged(
        parent.task_id,
        actor="operator",
        github=_MergeVerifier(_merge_evidence(parent)),
    )
    result = dispatcher.run_once("builder-2", FakeExecutor())

    assert result is not None and result.task_id == child.task_id
    assert result.state is TaskState.COMPLETED
    assert [item.task_id for item in dispatcher.store.list_tasks()].count(child.task_id) == 1


def test_merge_backpressure_is_soft_and_does_not_stop_independent_selection(
    tmp_path: Path,
) -> None:
    dispatcher, _, base_sha, _ = _fixture(tmp_path)
    dispatcher.merge_backpressure_limit = 1
    _make_pr_ready(dispatcher, base_sha, task_id="pressure-parent-0001")

    selected = dispatcher.select_next_roadmap_task(builder_id="builder-3")
    assert selected is not None
    assert selected.roadmap_item_id == "roadmap-b3-integration-1"
    assert dispatcher.status()["queue_mode"] == "CONTINUOUS_AUTONOMOUS"


def test_builder5_is_not_worker_and_future_builders_are_rejected(tmp_path: Path) -> None:
    dispatcher, _, _, _ = _fixture(tmp_path)

    with pytest.raises(DispatcherRecursionError):
        dispatcher.claim_next("builder-5")
    with pytest.raises(UnknownBuilderError):
        dispatcher.claim_next("builder-6")


class _GhFixture(GhPullRequestClient):
    def __init__(self, payload: dict[str, Any]) -> None:
        super().__init__(gh_executable="gh-fixture")
        self.payload = payload
        self.args: list[str] | None = None

    def _run(self, args: list[str], *, timeout: int) -> subprocess.CompletedProcess[str]:
        self.args = args
        return subprocess.CompletedProcess(
            args, 0, stdout=json.dumps(self.payload), stderr=""
        )


def test_github_merge_verification_is_read_only_and_exactly_bound(tmp_path: Path) -> None:
    dispatcher, _, base_sha, _ = _fixture(tmp_path)
    parent = _make_pr_ready(dispatcher, base_sha, task_id="github-merge-0001")
    evidence = _merge_evidence(parent)
    payload = {
        "number": evidence["pr_number"],
        "state": "MERGED",
        "mergedAt": evidence["merged_at"],
        "headRefName": evidence["head_ref_name"],
        "baseRefName": evidence["base_ref_name"],
        "headRefOid": evidence["head_ref_oid"],
        "baseRefOid": evidence["base_ref_oid"],
        "mergeCommit": evidence["merge_commit"],
    }
    client = _GhFixture(payload)

    verified = client.verify_merged(parent)

    assert verified["merged"] is True
    assert client.args is not None
    assert client.args[:5] == ["pr", "view", str(parent.pr_number), "--repo", REPO]
    assert "merge" not in client.args
    assert "push" not in client.args


def test_github_merge_verification_rejects_unbound_head(tmp_path: Path) -> None:
    dispatcher, _, base_sha, _ = _fixture(tmp_path)
    parent = _make_pr_ready(dispatcher, base_sha, task_id="github-bind-0001")
    payload = _merge_evidence(parent)
    client = _GhFixture(
        {
            "number": payload["pr_number"],
            "state": "MERGED",
            "mergedAt": payload["merged_at"],
            "headRefName": payload["head_ref_name"],
            "baseRefName": payload["base_ref_name"],
            "headRefOid": "a" * 40,
            "baseRefOid": payload["base_ref_oid"],
        }
    )

    with pytest.raises(DeliveryError):
        client.verify_merged(parent)


def test_reconcile_rejects_fake_repository_binding(tmp_path: Path) -> None:
    dispatcher, _, base_sha, _ = _fixture(tmp_path)
    parent = _make_pr_ready(dispatcher, base_sha, task_id="github-repo-0001")
    evidence = _merge_evidence(parent)
    evidence["repository"] = "Philip3006/other-repo"

    with pytest.raises(SafetyViolation):
        dispatcher.reconcile_merged(
            parent.task_id,
            actor="operator",
            github=_MergeVerifier(evidence),
        )
    assert dispatcher.store.get(parent.task_id).state is TaskState.PR_READY
