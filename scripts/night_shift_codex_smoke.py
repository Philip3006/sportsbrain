"""Run one harmless real CodexExecutor smoke test in a disposable repo."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.nightshift import CodexExecutor, NightShiftDispatcher, TaskSpec
from src.nightshift.worktree import WorktreeManager

REPO = "Philip3006/sportsbrain"


def _git(path: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(path), *args], capture_output=True, text=True, check=False
    )
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "git smoke fixture failed")
    return result.stdout.strip()


def main() -> int:
    codex = shutil.which("codex")
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
        manager = WorktreeManager(root / "runtime", {REPO: repo})
        dispatcher = NightShiftDispatcher.from_config(
            state_path=root / "runtime" / "state.sqlite3",
            worktree_manager=manager,
            lease_seconds=120,
            retry_base_seconds=0,
        )
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
        output = {
            "status": "PASS"
            if result.state.value == "pr_ready" and content_ok
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
            "summary": result.last_error or "success",
            "branch": branch,
            "head": head,
            "executor_data": (result.result or {}).get("data", {})
            if result.result
            else {},
            "changed_paths": list(
                (result.result or {}).get("data", {}).get("changed_paths", [])
            )
            if result.result
            else [],
            "pid": (result.result or {}).get("process_id") if result.result else None,
            "file_sha256": hashlib.sha256(target.read_bytes()).hexdigest()
            if content_ok
            else None,
            "merge_performed": False,
            "deployment_performed": False,
        }
        print(json.dumps(output, sort_keys=True, indent=2))
        return 0 if output["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
