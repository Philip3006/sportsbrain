"""Regression tests for isolated runtime artifact publication."""
from __future__ import annotations

import os
import shlex
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PUBLISHER = ROOT / "scripts" / "publish_runtime_artifacts.sh"


def _git(args: list[str], cwd: Path, *, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=cwd, text=True, capture_output=True, check=check)


def _clone(origin: Path, destination: Path) -> Path:
    _git(["clone", str(origin), str(destination)], origin.parent)
    _git(["config", "user.name", "Test"], destination)
    _git(["config", "user.email", "test@example.invalid"], destination)
    return destination


def _seed(tmp_path: Path) -> tuple[Path, Path, Path]:
    origin = tmp_path / "origin.git"
    _git(["init", "--bare", str(origin)], tmp_path)
    seed = _clone(origin, tmp_path / "seed")
    signals = seed / "docs" / "data" / "signals.json"
    signals.parent.mkdir(parents=True)
    signals.write_text('{"tennis": []}\n')
    _git(["add", "docs/data/signals.json"], seed)
    _git(["commit", "-m", "seed"], seed)
    _git(["push", "origin", "HEAD:main"], seed)
    _git(["symbolic-ref", "HEAD", "refs/heads/main"], origin)
    return origin, _clone(origin, tmp_path / "active"), tmp_path / "publisher"


def _command(source: Path, log: Path, *paths: str) -> list[str]:
    return [
        "bash",
        "-c",
        (
            f'source {shlex.quote(str(PUBLISHER))} && {{ '
            '_publish_remote_url() { printf "%s\\n" "$SPORTSBRAIN_TEST_PUBLISH_REMOTE"; }; '
            'runtime_publish_artifacts "$1" "$2" "$3" "${@:4}"; }'
        ),
        "publisher",
        str(source),
        str(log),
        "auto: runtime artifact",
        *paths,
    ]


def _environment(source: Path, publisher: Path, lock_timeout: str) -> dict[str, str]:
    env = {
        **os.environ,
        "SPORTSBRAIN_PUBLISH_DIR": str(publisher),
        "SPORTSBRAIN_PUBLISH_LOCK_TIMEOUT_SECONDS": lock_timeout,
    }
    # Use the source's actual remote, not an assumed GitHub URL, in isolated tests.
    env["SPORTSBRAIN_TEST_PUBLISH_REMOTE"] = _git(["remote", "get-url", "origin"], source).stdout.strip()
    return env


def _run(
    source: Path, publisher: Path, log: Path, *paths: str, lock_timeout: str = "0"
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        _command(source, log, *paths),
        text=True,
        capture_output=True,
        env=_environment(source, publisher, lock_timeout),
        check=False,
    )


def _start(
    source: Path, publisher: Path, log: Path, *paths: str, lock_timeout: str = "0"
) -> subprocess.Popen[str]:
    return subprocess.Popen(
        _command(source, log, *paths),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=_environment(source, publisher, lock_timeout),
    )


def _pause_next_push(publisher: Path, marker: Path) -> None:
    hook = publisher / ".git" / "hooks" / "pre-push"
    hook.write_text(
        "#!/bin/sh\n"
        f"if [ ! -e {shlex.quote(str(marker))} ]; then\n"
        f"  touch {shlex.quote(str(marker))}\n"
        "  sleep 2\n"
        "fi\n"
    )
    hook.chmod(0o755)


def _wait_for(path: Path) -> None:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {path}")


def _advance_origin(origin: Path, destination: Path, path: str, contents: str) -> None:
    writer = _clone(origin, destination)
    target = writer / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(contents)
    _git(["add", path], writer)
    _git(["commit", "-m", "concurrent runtime update"], writer)
    _git(["push", "origin", "HEAD:main"], writer)


def test_active_head_and_index_remain_unchanged_after_publication(tmp_path: Path):
    origin, active, publisher = _seed(tmp_path)
    signal = active / "docs" / "data" / "signals.json"
    signal.write_text('{"tennis": [{"signal_id": "fresh"}]}\n')
    head_before = _git(["rev-parse", "HEAD"], active).stdout.strip()

    result = _run(active, publisher, tmp_path / "publish.log", "docs/data/signals.json")

    assert result.returncode == 0, result.stderr
    assert _git(["rev-parse", "HEAD"], active).stdout.strip() == head_before
    assert _git(["diff", "--cached", "--name-only"], active).stdout == ""
    assert "docs/data/signals.json" in _git(["status", "--short"], active).stdout
    assert _git(["show", "main:docs/data/signals.json"], origin).stdout == signal.read_text()
    assert _git(["branch", "--show-current"], publisher).stdout.strip() == "runtime-publish"


def test_setup_is_idempotent_and_rejects_active_checkout(tmp_path: Path):
    _, active, publisher = _seed(tmp_path)
    log = tmp_path / "setup.log"
    first = _run(active, publisher, log, "docs/data/signals.json")
    assert first.returncode == 0
    second = _run(active, publisher, log, "docs/data/signals.json")
    assert second.returncode == 0
    rejected = _run(active, active, log, "docs/data/signals.json")
    assert rejected.returncode != 0


def test_allowlist_rejects_non_artifact_paths(tmp_path: Path):
    _, active, publisher = _seed(tmp_path)
    for path in (
        "src/x.py",
        "scripts/x.sh",
        ".github/workflows/x.yml",
        "cloudflare/x.js",
        "models/x.pkl",
        "results/ledger_philip.csv",
        ".env",
        "credentials.json",
        "docs/data/unknown.json",
    ):
        target = active / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("x")
        result = _run(active, publisher, tmp_path / "publish.log", path)
        assert result.returncode != 0, path


def test_allowlist_accepts_all_current_runtime_artifacts(tmp_path: Path):
    origin, active, publisher = _seed(tmp_path)
    artifacts = {
        "docs/data/signals.json": '{"football": []}\n',
        "docs/data/signals_philip.json": '{"football": []}\n',
        "docs/data/tennis_live_scores.json": '[]\n',
        "data/cache/tennis_live_scores.json": '[]\n',
        "data/cache/tennis_suspended.json": '[]\n',
    }
    for path, contents in artifacts.items():
        target = active / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(contents)

    result = _run(active, publisher, tmp_path / "publish.log", *artifacts)

    assert result.returncode == 0, result.stderr
    for path, contents in artifacts.items():
        assert _git(["show", f"main:{path}"], origin).stdout == contents


def test_concurrent_publishers_cannot_mutate_the_publish_checkout_together(tmp_path: Path):
    _, active, publisher = _seed(tmp_path)
    setup = _run(active, publisher, tmp_path / "setup.log", "docs/data/signals.json")
    assert setup.returncode == 0
    signal = active / "docs" / "data" / "signals.json"
    signal.write_text('{"tennis": [{"signal_id": "serialized"}]}\n')
    marker = tmp_path / "push-started"
    _pause_next_push(publisher, marker)

    first = _start(active, publisher, tmp_path / "first.log", "docs/data/signals.json", lock_timeout="2")
    _wait_for(marker)
    second = _run(active, publisher, tmp_path / "second.log", "docs/data/signals.json")
    stdout, stderr = first.communicate(timeout=10)

    assert first.returncode == 0, stderr or stdout
    assert second.returncode != 0
    assert not Path(f"{publisher}.sportsbrain-runtime-publish.lock.d").exists()


def test_remote_advance_is_rebased_and_retried_without_force(tmp_path: Path):
    origin, active, publisher = _seed(tmp_path)
    setup = _run(active, publisher, tmp_path / "setup.log", "docs/data/signals.json")
    assert setup.returncode == 0
    active_head = _git(["rev-parse", "HEAD"], active).stdout.strip()
    signal = active / "docs" / "data" / "signals.json"
    signal.write_text('{"tennis": [{"signal_id": "rebased"}]}\n')
    marker = tmp_path / "push-started"
    _pause_next_push(publisher, marker)

    publication = _start(active, publisher, tmp_path / "publish.log", "docs/data/signals.json")
    _wait_for(marker)
    _advance_origin(origin, tmp_path / "remote-health", "docs/data/health.json", '{"status": "ok"}\n')
    stdout, stderr = publication.communicate(timeout=10)

    assert publication.returncode == 0, stderr or stdout
    assert _git(["show", "main:docs/data/signals.json"], origin).stdout == signal.read_text()
    assert _git(["show", "main:docs/data/health.json"], origin).stdout == '{"status": "ok"}\n'
    helper = PUBLISHER.read_text()
    assert "push --force" not in helper
    assert "for attempt in 1 2 3" in helper
    assert "rebase origin/main" in helper
    assert _git(["rev-parse", "HEAD"], active).stdout.strip() == active_head


def test_remote_conflict_fails_closed_without_touching_active_checkout(tmp_path: Path):
    origin, active, publisher = _seed(tmp_path)
    setup = _run(active, publisher, tmp_path / "setup.log", "docs/data/signals.json")
    assert setup.returncode == 0
    signal = active / "docs" / "data" / "signals.json"
    signal.write_text('{"tennis": [{"signal_id": "local"}]}\n')
    active_head = _git(["rev-parse", "HEAD"], active).stdout.strip()
    marker = tmp_path / "push-started"
    _pause_next_push(publisher, marker)

    publication = _start(active, publisher, tmp_path / "publish.log", "docs/data/signals.json")
    _wait_for(marker)
    _advance_origin(origin, tmp_path / "remote-conflict", "docs/data/signals.json", '{"tennis": [{"signal_id": "remote"}]}\n')
    stdout, stderr = publication.communicate(timeout=10)

    assert publication.returncode != 0, stderr or stdout
    assert _git(["rev-parse", "HEAD"], active).stdout.strip() == active_head
    assert signal.read_text() == '{"tennis": [{"signal_id": "local"}]}\n'
    assert _git(["show", "main:docs/data/signals.json"], origin).stdout == '{"tennis": [{"signal_id": "remote"}]}\n'
    assert _git(["status", "--porcelain"], publisher).stdout == ""


def test_local_wrappers_and_healers_cannot_mutate_active_git_state():
    for name in ("scan_cron.sh", "closing_odds_cron.sh", "live_score_trigger.sh"):
        text = (ROOT / "scripts" / name).read_text()
        assert "runtime_publish_artifacts" in text
        assert "git commit" not in text
        assert "git add" not in text
        assert "git push" not in text
        assert "git rebase" not in text
        assert "reset --hard" not in text

    for name in ("auto_heal_cron.sh", "auto_heal_ai.py"):
        text = (ROOT / "scripts" / name).read_text()
        assert "git_safe_push" not in text


def test_publisher_has_no_active_checkout_git_mutation_command():
    text = PUBLISHER.read_text()
    for operation in ("add", "commit", "push", "rebase", "reset", "checkout", "switch"):
        assert f'git -C "$source_dir" {operation}' not in text
    assert "SPORTSBRAIN_PUBLISH_REMOTE" not in text
