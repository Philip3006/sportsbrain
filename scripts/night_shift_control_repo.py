"""Provision the isolated Night Shift Git control repository.

This script never clones into or changes the production/runtime checkout. It
only creates or refreshes the dedicated bare control repository and its
separate worktree root.
"""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

REMOTE = "https://github.com/Philip3006/sportsbrain.git"
DEFAULT_CONTROL = (
    Path.home()
    / "Library"
    / "Application Support"
    / "SportsBrain"
    / "night-shift"
    / "repo.git"
)
DEFAULT_WORKTREES = (
    Path.home()
    / "Library"
    / "Application Support"
    / "SportsBrain"
    / "night-shift"
    / "worktrees"
)


def git(control: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "--git-dir", str(control), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(
            result.stderr.strip() or "git control repository operation failed"
        )
    return result.stdout.strip()


def provision(control: Path, worktrees: Path) -> dict[str, str]:
    control = control.expanduser().resolve()
    worktrees = worktrees.expanduser().resolve()
    control.parent.mkdir(parents=True, exist_ok=True)
    if not (control / "HEAD").is_file():
        result = subprocess.run(
            ["git", "init", "--bare", "--initial-branch=main", str(control)],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode:
            raise RuntimeError(
                result.stderr.strip() or "cannot initialize control repository"
            )
    remotes = git(control, "remote")
    if "origin" not in remotes.splitlines():
        git(control, "remote", "add", "origin", REMOTE)
    if (
        git(control, "remote", "get-url", "origin").removesuffix(".git").lower()
        != REMOTE.removesuffix(".git").lower()
    ):
        raise RuntimeError(
            "control repository origin is not the governed SportsBrain remote"
        )
    git(control, "config", "user.name", "SportsBrain Night Shift")
    git(control, "config", "user.email", "nightshift@sportsbrain.invalid")
    git(control, "fetch", "--no-tags", "origin", "main")
    git(control, "update-ref", "refs/heads/main", "refs/remotes/origin/main")
    git(control, "symbolic-ref", "HEAD", "refs/heads/main")
    worktrees.mkdir(parents=True, exist_ok=True)
    sha = git(control, "rev-parse", "refs/remotes/origin/main")
    return {
        "control_repo": str(control),
        "worktree_root": str(worktrees),
        "origin_main": sha,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Provision Night Shift Git isolation")
    parser.add_argument("--control-repo", type=Path, default=DEFAULT_CONTROL)
    parser.add_argument("--worktree-root", type=Path, default=DEFAULT_WORKTREES)
    args = parser.parse_args()
    print(provision(args.control_repo, args.worktree_root))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
