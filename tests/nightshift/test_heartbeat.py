from __future__ import annotations

import time
from pathlib import Path

from src.nightshift import CodexExecutor, TaskSpec, TaskState

from .test_delivery import _fixture


def _slow_codex(path: Path) -> Path:
    executable = path / "codex-fixture"
    executable.write_text(
        "#!/usr/bin/env python3\n"
        "import time\n"
        "from pathlib import Path\n"
        "time.sleep(2.2)\n"
        "Path('heartbeat.txt').write_text('ok\\n', encoding='utf-8')\n",
        encoding="utf-8",
    )
    executable.chmod(0o700)
    return executable


def _heartbeat_task() -> TaskSpec:
    return TaskSpec(
        task_id="heartbeat-test-0001",
        builder_id="builder-1",
        objective="Run a bounded heartbeat fixture",
        branch="nightshift/builder-1/heartbeat-test",
        task_type="evidence_lifecycle_audit",
        risk_class="read_only",
        allowed_paths=("heartbeat.txt",),
    )


def test_codex_executor_renews_lease_while_process_is_alive(tmp_path: Path) -> None:
    dispatcher, manager, _, _ = _fixture(tmp_path)
    task = dispatcher.submit(_heartbeat_task())
    claimed = dispatcher.claim_next(
        "builder-1", worker_instance_id="builder-1:heartbeat"
    )
    assert claimed is not None
    dispatcher.store.start_running(
        task.task_id,
        worker_id="builder-1:heartbeat",
        lease_generation=claimed.lease_generation,
    )
    heartbeats: list[float] = []

    def heartbeat() -> None:
        heartbeats.append(time.monotonic())
        dispatcher.heartbeat(
            task.task_id,
            worker_instance_id="builder-1:heartbeat",
            lease_generation=claimed.lease_generation,
        )

    def process_started(pid: int) -> None:
        dispatcher.store.record_process(
            task.task_id,
            worker_id="builder-1:heartbeat",
            lease_generation=claimed.lease_generation,
            process_id=pid,
        )

    result = CodexExecutor(
        str(_slow_codex(tmp_path)), manager, timeout_seconds=10
    )._run(
        dispatcher.store.get(task.task_id),
        heartbeat=heartbeat,
        process_started=process_started,
        heartbeat_interval_seconds=1,
    )

    assert result.success is True
    assert len(heartbeats) >= 2
    assert (Path(claimed.worktree_path or "") / "heartbeat.txt").exists()
    assert manager.changed_paths(Path(claimed.worktree_path or "")) == (
        "heartbeat.txt",
    )


def test_heartbeat_failure_stops_codex_and_returns_failed_safe(tmp_path: Path) -> None:
    dispatcher, manager, _, _ = _fixture(tmp_path)
    task = dispatcher.submit(_heartbeat_task())
    claimed = dispatcher.claim_next(
        "builder-1", worker_instance_id="builder-1:heartbeat"
    )
    assert claimed is not None
    dispatcher.store.start_running(
        task.task_id,
        worker_id="builder-1:heartbeat",
        lease_generation=claimed.lease_generation,
    )
    calls = 0

    def heartbeat() -> None:
        nonlocal calls
        calls += 1
        if calls >= 2:
            raise RuntimeError("fixture fencing")

    result = CodexExecutor(
        str(_slow_codex(tmp_path)), manager, timeout_seconds=10
    )._run(
        dispatcher.store.get(task.task_id),
        heartbeat=heartbeat,
        process_started=lambda _: None,
        heartbeat_interval_seconds=1,
    )

    assert calls >= 2
    assert result.success is False
    assert result.terminal_state is TaskState.FAILED_SAFE
