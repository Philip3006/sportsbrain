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
