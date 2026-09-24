from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from src.nightshift import (
    FakeExecutor,
    NightShiftDispatcher,
    TaskSpec,
    TaskState,
    WorktreeManager,
    WorktreeSafetyError,
)
from src.nightshift.worktree import RuntimeDirtyPolicy


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args: str) -> None:
        subprocess.run(
            ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
        )

    git("init", "-q")
    git("config", "user.email", "test@example.invalid")
    git("config", "user.name", "Night Shift Test")
    (repo / "README.md").write_text("fixture\n", encoding="utf-8")
    git("add", "README.md")
    git("commit", "-qm", "fixture")
    remote = tmp_path / "remote.git"
    subprocess.run(
        ["git", "init", "--bare", "-q", str(remote)],
        check=True,
        capture_output=True,
        text=True,
    )
    git("remote", "add", "origin", str(remote))
    git("push", "-q", "origin", "HEAD:main")
    return repo


def _git(path: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(path), *args], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def _task(task_id: str, branch: str, **kwargs: object) -> TaskSpec:
    return TaskSpec(
        task_id=task_id,
        builder_id="builder-1",
        objective="Inspect bounded evidence",
        branch=branch,
        task_type="evidence_lifecycle_audit",
        risk_class="read_only",
        **kwargs,
    )


def test_worktree_allocation_is_isolated_and_canonical_stays_clean(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    manager = WorktreeManager(tmp_path / "runtime", {"Philip3006/sportsbrain": repo})
    task = _task(
        "worktree-000001", "nightshift/builder-1/isolated", allowed_paths=("fixtures",)
    )
    allocation = manager.allocate(task)
    assert allocation.path != repo
    assert allocation.path.is_dir()
    assert allocation.branch == task.branch
    assert manager.canonical_check(task.repo)["clean"] is True


def test_dirty_canonical_checkout_blocks_allocation(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "dirty.txt").write_text("unsafe\n", encoding="utf-8")
    manager = WorktreeManager(tmp_path / "runtime", {"Philip3006/sportsbrain": repo})
    with pytest.raises(WorktreeSafetyError, match="not clean"):
        manager.allocate(_task("worktree-000002", "nightshift/builder-1/dirty"))


def test_required_merged_sha_accepts_a_newer_authoritative_base(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    merged_sha = _git(repo, "rev-parse", "HEAD")
    (repo / "README.md").write_text("newer base\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-qm", "advance authoritative base")
    _git(repo, "push", "-q", "origin", "HEAD:main")

    manager = WorktreeManager(tmp_path / "runtime", {"Philip3006/sportsbrain": repo})
    allocation = manager.allocate(
        _task(
            "worktree-000003",
            "nightshift/builder-1/merge-bound",
            payload={"required_merged_sha": merged_sha},
        )
    )

    assert allocation.base_sha != merged_sha
    assert _git(allocation.path, "merge-base", "--is-ancestor", merged_sha, allocation.base_sha) == ""


def test_scope_violation_fails_safe_and_keeps_diagnostic(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    manager = WorktreeManager(tmp_path / "runtime", {"Philip3006/sportsbrain": repo})
    dispatcher = NightShiftDispatcher.from_config(
        state_path=tmp_path / "runtime" / "state.sqlite3",
        worktree_manager=manager,
        lease_seconds=30,
        retry_base_seconds=0,
    )
    dispatcher.submit(
        _task(
            "worktree-000003", "nightshift/builder-1/scope", allowed_paths=("fixtures",)
        )
    )
    failed = dispatcher.run_once("builder-1", FakeExecutor(write_file="outside.txt"))
    assert failed is not None and failed.state is TaskState.FAILED_SAFE
    assert failed.failure_class == "SCOPE_VIOLATION"
    assert Path(failed.diagnostic_path or "").exists()


def test_expected_runtime_dirtiness_uses_dedicated_control_repo(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "docs" / "data").mkdir(parents=True)
    health = repo / "docs" / "data" / "health.json"
    health.write_text('{"status":"ok"}\n', encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "docs/data/health.json"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-qm", "health fixture"], check=True
    )
    subprocess.run(
        ["git", "-C", str(repo), "push", "-q", "origin", "HEAD:main"], check=True
    )
    remote = subprocess.run(
        ["git", "-C", str(repo), "remote", "get-url", "origin"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    control = tmp_path / "control.git"
    control.mkdir()
    subprocess.run(
        ["git", "-C", str(control), "init", "-q", "--bare", "--initial-branch=main"],
        check=True,
    )
    subprocess.run(
        ["git", "--git-dir", str(control), "remote", "add", "origin", remote],
        check=True,
    )
    subprocess.run(
        ["git", "--git-dir", str(control), "fetch", "-q", "origin", "main"],
        check=True,
    )
    health.write_text('{"status":"warn"}\n', encoding="utf-8")
    policy = RuntimeDirtyPolicy.from_mapping(
        {
            "version": 1,
            "repositories": {
                "Philip3006/sportsbrain": [
                    {
                        "path": "docs/data/health.json",
                        "job": "fixture-writer",
                        "script": "fixture",
                        "reason": "known runtime writer",
                        "observed_evidence": "fixture evidence",
                    }
                ]
            },
        }
    )
    manager = WorktreeManager(
        tmp_path / "runtime",
        {"Philip3006/sportsbrain": repo},
        control_repo_paths={"Philip3006/sportsbrain": control},
        runtime_dirty_policy=policy,
        worktrees_dir=tmp_path / "isolated-worktrees",
    )
    task = _task("worktree-expected-001", "nightshift/builder-1/expected")
    check = manager.canonical_check(task.repo)
    assert check["dirty_class"] == "RUNTIME_CHECKOUT_DIRTY_EXPECTED"
    allocation = manager.allocate(task)
    assert allocation.path != repo
    assert allocation.origin_sha == manager.control_check(task.repo)["origin_sha"]


def test_unexpected_source_dirtiness_blocks_even_with_control_repo(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    control = tmp_path / "control.git"
    control.mkdir()
    subprocess.run(
        ["git", "-C", str(control), "init", "-q", "--bare", "--initial-branch=main"],
        check=True,
    )
    remote = subprocess.run(
        ["git", "-C", str(repo), "remote", "get-url", "origin"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    subprocess.run(
        ["git", "--git-dir", str(control), "remote", "add", "origin", remote],
        check=True,
    )
    subprocess.run(
        ["git", "--git-dir", str(control), "fetch", "-q", "origin", "main"],
        check=True,
    )
    (repo / "unexpected-source.py").write_text("unsafe\n", encoding="utf-8")
    manager = WorktreeManager(
        tmp_path / "runtime",
        {"Philip3006/sportsbrain": repo},
        control_repo_paths={"Philip3006/sportsbrain": control},
        worktrees_dir=tmp_path / "isolated-worktrees",
    )
    check = manager.canonical_check("Philip3006/sportsbrain")
    assert check["dirty_class"] == "UNEXPECTED_SOURCE_DIRTY"
    with pytest.raises(WorktreeSafetyError, match="unexpected source dirtiness"):
        manager.allocate(
            _task("worktree-unexpected-001", "nightshift/builder-1/unexpected")
        )


def test_explicit_control_repo_cannot_reuse_canonical_checkout(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    manager = WorktreeManager(
        tmp_path / "runtime",
        {"Philip3006/sportsbrain": repo},
        control_repo_paths={"Philip3006/sportsbrain": repo},
    )
    check = manager.control_check("Philip3006/sportsbrain")
    assert check["is_control_repository"] is False
    assert "separate" in check["error"]
    with pytest.raises(WorktreeSafetyError, match="separate"):
        manager.allocate(_task("worktree-same-control-1", "nightshift/builder-1/same"))
