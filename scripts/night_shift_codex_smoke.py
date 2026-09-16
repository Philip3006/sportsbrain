"""Run one harmless real CodexExecutor smoke test in a disposable repo."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.nightshift import CodexExecutor, NightShiftDispatcher, TaskSpec, TaskState
from src.nightshift.worktree import WorktreeManager

REPO = "Philip3006/sportsbrain"


class _DisposablePullRequestClient:
    """Local PR fixture so a smoke run cannot mutate public GitHub state."""

    def find_or_create(
        self, task: Any, *, commit_sha: str, verification: dict[str, Any]
    ) -> dict[str, Any]:
        return {
            "number": 1,
            "url": f"https://example.invalid/nightshift/{task.task_id}",
            "reused": False,
            "task_id": task.task_id,
            "commit_sha": commit_sha,
            "verification": verification,
        }


def _git(path: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(path), *args], capture_output=True, text=True, check=False
    )
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "git smoke fixture failed")
    return result.stdout.strip()


def main() -> int:
    codex = os.getenv("SPORTSBRAIN_CODEX_PATH") or shutil.which("codex")
    fallback = Path("/Applications/ChatGPT.app/Contents/Resources/codex")
    if not codex and fallback.is_file():
        codex = str(fallback)
    if not codex:
        print(
            json.dumps(
                {"status": "BLOCKED", "reason": "Codex executable unavailable"},
                sort_keys=True,
            )
        )
        return 2
    with tempfile.TemporaryDirectory(prefix="sportsbrain-codex-smoke-") as temp:
        root = Path(temp)
        repo = root / "repo"
        repo.mkdir()
        _git(repo, "init", "-q")
        _git(repo, "config", "user.email", "smoke@example.invalid")
        _git(repo, "config", "user.name", "Builder 5 Smoke")
        (repo / "README.md").write_text("disposable fixture\n", encoding="utf-8")
        _git(repo, "add", "README.md")
        _git(repo, "commit", "-qm", "disposable fixture")
        remote = root / "remote.git"
        subprocess.run(
            ["git", "init", "--bare", "-q", str(remote)],
            check=True,
            capture_output=True,
            text=True,
        )
        _git(repo, "remote", "add", "origin", str(remote))
        _git(repo, "push", "-q", "origin", "HEAD:main")
        base_sha = _git(repo, "rev-parse", "HEAD")
        manager = WorktreeManager(root / "runtime", {REPO: repo})
        dispatcher = NightShiftDispatcher.from_config(
            state_path=root / "runtime" / "state.sqlite3",
            worktree_manager=manager,
            lease_seconds=120,
            retry_base_seconds=0,
        )
        assert dispatcher.delivery_pipeline is not None
        dispatcher.delivery_pipeline.github = _DisposablePullRequestClient()
        task = dispatcher.submit(
            TaskSpec(
                task_id="codex-smoke-0001",
                builder_id="builder-1",
                objective="Create smoke/BUILDER_5_SMOKE.txt with exactly the text BUILDER_5_SMOKE_OK followed by a newline.",
                branch="nightshift/builder-1/codex-smoke",
                task_type="evidence_lifecycle_audit",
                risk_class="code_change",
                allowed_paths=("smoke",),
                requires_approval=True,
                expected_base_sha=base_sha,
                required_tests=("compileall",),
                verification_commands=(("python3", "-m", "compileall", "-q", "smoke"),),
                max_runtime_seconds=300,
            )
        )
        dispatcher.approve(
            task.task_id, approver="smoke-operator", reason="disposable fixture only"
        )
        result = dispatcher.run_once(
            "builder-1", CodexExecutor(codex, manager, timeout_seconds=300)
        )
        if result is None or not result.worktree_path:
            raise RuntimeError("Codex smoke did not claim a task")
        target = Path(result.worktree_path) / "smoke" / "BUILDER_5_SMOKE.txt"
        content_ok = (
            target.read_text(encoding="utf-8") == "BUILDER_5_SMOKE_OK\n"
            if target.exists()
            else False
        )
        branch = _git(Path(result.worktree_path), "branch", "--show-current")
        head = _git(Path(result.worktree_path), "rev-parse", "HEAD")
        heartbeat_events = sum(
            event.event_type == "heartbeat"
            for event in dispatcher.store.audit_events(
                task_id=result.task_id, limit=1000
            )
        )
        data = (result.result or {}).get("data", {}) if result.result else {}
        delivery_blocked = result.failure_class == "DELIVERY_BLOCKED_GITHUB_AUTH"
        successful_delivery = result.state in {TaskState.PR_READY, TaskState.CEO_REVIEW}
        output = {
            "status": "PASS"
            if successful_delivery and content_ok
            else "BLOCKED"
            if delivery_blocked
            else "FAIL",
            "codex": codex,
            "command_form": [
                codex,
                "exec",
                "--ephemeral",
                "-C",
                "<isolated-worktree>",
                "--sandbox",
                "workspace-write",
                "--skip-git-repo-check",
                "-",
            ],
            "task_state": result.state.value,
            "failure_class": result.failure_class,
            "summary": result.last_error or "success",
            "branch": branch,
            "head": head,
            "base_branch": result.base_branch,
            "base_sha": result.base_sha,
            "origin_sha": result.origin_sha,
            "commit_sha": result.commit_sha,
            "remote_sha": result.remote_sha,
            "pr_number": result.pr_number,
            "pr_url": result.pr_url,
            "pr_client": "disposable-local-fixture",
            "executor_data": data,
            "changed_paths": list(data.get("changed_paths", [])),
            "pid": data.get("pid") or data.get("process_id"),
            "heartbeat_events": heartbeat_events,
            "verification": result.verification,
            "file_sha256": hashlib.sha256(target.read_bytes()).hexdigest()
            if content_ok
            else None,
            "merge_performed": False,
            "deployment_performed": False,
        }
        print(json.dumps(output, sort_keys=True, indent=2))
        return (
            0
            if output["status"] == "PASS"
            else 2
            if output["status"] == "BLOCKED"
            else 1
        )


if __name__ == "__main__":
    raise SystemExit(main())
