"""Optional unattended worker loop for one explicitly registered Builder."""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.nightshift import CodexExecutor, NightShiftDispatcher


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run one governed Builder 1-4 Night Shift worker"
    )
    parser.add_argument(
        "--builder",
        required=True,
        choices=("builder-1", "builder-2", "builder-3", "builder-4"),
    )
    parser.add_argument("--poll-seconds", type=int, default=30)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    if not 1 <= args.poll_seconds <= 60:
        parser.error("--poll-seconds must be between 1 and 60")
    codex = os.getenv("SPORTSBRAIN_CODEX_EXECUTABLE") or shutil.which("codex")
    if not codex:
        raise RuntimeError("Codex executable is unavailable; refusing fake fallback")
    dispatcher = NightShiftDispatcher.from_config(
        state_path=Path(os.environ["SPORTSBRAIN_NIGHTSHIFT_STATE"])
        if os.getenv("SPORTSBRAIN_NIGHTSHIFT_STATE")
        else None
    )
    if dispatcher.worktree_manager is None:
        raise RuntimeError("isolated worktrees are required")
    executor = CodexExecutor(codex, dispatcher.worktree_manager)
    while True:
        dispatcher.recover_expired()
        dispatcher.run_once(args.builder, executor)
        if args.once:
            return 0
        time.sleep(args.poll_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
