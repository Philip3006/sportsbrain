from __future__ import annotations

import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import monitoring.top5.governed_runtime_evidence as evidence_module
import monitoring.top5.governed_runtime_state as state_module
from monitoring.top5.governed_runtime_evidence import (
    ARTIFACT_SCHEMA,
    observe_governed_runtime,
    verify_artifact_digest,
)
from src.football.top5_activation_route_state import (
    DurableTop5ProductionRouteStateStore,
    TOP5_ROUTE_STATE_PATH,
    TOP5_ROUTE_STORE_SCHEMA,
)
from src.football.top5_durable_activation import _sha
from src.football.top5_final_acceptance_public import _validate_runtime
from src.runtime import paths

SOURCE_SHA = "a" * 40
RUNTIME_SHA = "b" * 64


def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    path.chmod(0o600)


def _git_repo(path: Path) -> None:
    path.mkdir()
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(
        ["git", "-C", str(path), "config", "user.email", "test@example.invalid"],
        check=True,
    )
    subprocess.run(["git", "-C", str(path), "config", "user.name", "test"], check=True)
    (path / "tracked.txt").write_text("clean", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "tracked.txt"], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "fixture"], check=True)


def _route_state() -> dict[str, object]:
    current_route = DurableTop5ProductionRouteStateStore._disabled_route()
    body: dict[str, object] = {
        "schema_version": TOP5_ROUTE_STORE_SCHEMA,
        "revision": 0,
        "current_route": current_route,
        "records": {},
        "consumed_nonces": {},
        "audit_history": [],
    }
    body["state_digest"] = _sha(body)
    return body


def _state(runtime_root: Path, publisher_root: Path, **overrides: object) -> dict[str, object]:
    state_path = runtime_root / evidence_module.STATE_RELATIVE_PATH
    state: dict[str, object] = {
        "schema_version": evidence_module.STATE_SCHEMA,
        "runtime_root_role": "governed-runtime",
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "source_release_sha": SOURCE_SHA,
        "source_release_resolution": {
            "source": str(state_path),
            "method": "operator-release-record",
            "resolved_sha": SOURCE_SHA,
        },
        "runtime_data_sha": RUNTIME_SHA,
        "runtime_data_resolution": {
            "source": str(state_path),
            "method": "operator-runtime-generation-record",
            "resolved_sha": RUNTIME_SHA,
        },
        "active_provider_order": ["the_odds_api"],
        "provider_authority": "the_odds_api",
        "publisher_workspace_root": str(publisher_root),
        "health_authority": "governed",
        "health_source": "operator-health-state",
        "health_status": "ok",
        "activation_state": "DISABLED",
        "scheduler_state": "not_registered",
        "no_bet": True,
        "publication_enabled": False,
    }
    state.update(overrides)
    return state


@pytest.fixture
def valid_observation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path, Path]:
    runtime_root = tmp_path / "runtime-state"
    publisher_root = tmp_path / "publisher"
    source_root = tmp_path / "source"
    _git_repo(publisher_root)
    _git_repo(source_root)
    _write_json(runtime_root / TOP5_ROUTE_STATE_PATH, _route_state())
    _write_json(
        runtime_root / evidence_module.STATE_RELATIVE_PATH,
        _state(runtime_root, publisher_root),
    )
    monkeypatch.setattr(evidence_module, "governed_runtime_root", lambda: runtime_root)
    monkeypatch.setattr(evidence_module, "ROOT", source_root)
    return runtime_root, publisher_root, source_root


def test_external_governed_runtime_root_is_required_and_rejects_test_or_checkout_roots(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv(paths.RUNTIME_STATE_ENV, raising=False)
    monkeypatch.setattr(paths, "DEFAULT_RUNTIME_STATE_DIR", tmp_path / "runtime-state")
    with pytest.raises(RuntimeError):
        paths.governed_runtime_root()

    monkeypatch.setenv(paths.RUNTIME_STATE_ENV, str(paths.ROOT))
    with pytest.raises(RuntimeError):
        paths.governed_runtime_root()

    rejected = tmp_path / "tests" / "runtime-state"
    rejected.mkdir(parents=True)
    monkeypatch.setenv(paths.RUNTIME_STATE_ENV, str(rejected))
    with pytest.raises(RuntimeError):
        paths.governed_runtime_root()


def test_missing_route_state_blocks(valid_observation: tuple[Path, Path, Path]) -> None:
    runtime_root, _, _ = valid_observation
    (runtime_root / TOP5_ROUTE_STATE_PATH).unlink()
    result = observe_governed_runtime()
    assert result["status"] == "BLOCKED"


def test_the_odds_api_observed_authority_passes_and_b1_consumes_payload(
    valid_observation: tuple[Path, Path, Path],
) -> None:
    payload = observe_governed_runtime()
    assert payload["status"] == "READY"
    assert payload["schema_version"] == ARTIFACT_SCHEMA
    assert payload["provider_authority"] == "the_odds_api"
    assert payload["active_provider_order"] == ["the_odds_api"]
    assert payload["source_release_sha"] == SOURCE_SHA
    assert payload["runtime_data_sha"] == RUNTIME_SHA
    assert payload["source_release_sha"] != payload["runtime_data_sha"]
    assert payload["no_bet"] is True
    assert payload["publication_enabled"] is False
    assert payload["activation_state"] == "DISABLED"
    assert payload["scheduler_state"] == "not_registered"
    assert verify_artifact_digest(payload)
    _validate_runtime(
        payload,
        now=datetime.now(timezone.utc),
        model={"source_sha": SOURCE_SHA},
    )


def test_candidate_provider_authority_and_order_are_blocked(
    valid_observation: tuple[Path, Path, Path],
) -> None:
    runtime_root, publisher_root, _ = valid_observation
    path = runtime_root / evidence_module.STATE_RELATIVE_PATH
    _write_json(
        path,
        _state(runtime_root, publisher_root, provider_authority="therundown_experimental"),
    )
    assert observe_governed_runtime()["status"] == "BLOCKED"

    _write_json(
        path,
        _state(
            runtime_root,
            publisher_root,
            active_provider_order=["the_odds_api", "therundown_experimental"],
        ),
    )
    assert observe_governed_runtime()["status"] == "BLOCKED"


def test_dirty_source_and_publisher_block(
    valid_observation: tuple[Path, Path, Path],
) -> None:
    _, publisher_root, source_root = valid_observation
    (source_root / "dirty.txt").write_text("dirty", encoding="utf-8")
    assert observe_governed_runtime()["status"] == "BLOCKED"

    (source_root / "dirty.txt").unlink()
    (publisher_root / "dirty.txt").write_text("dirty", encoding="utf-8")
    assert observe_governed_runtime()["status"] == "BLOCKED"


def test_observer_generates_fresh_timestamp_and_ignores_input_timestamp(
    valid_observation: tuple[Path, Path, Path],
) -> None:
    runtime_root, publisher_root, _ = valid_observation
    path = runtime_root / evidence_module.STATE_RELATIVE_PATH
    _write_json(
        path,
        _state(runtime_root, publisher_root, captured_at="2000-01-01T00:00:00Z"),
    )
    before = datetime.now(timezone.utc)
    payload = observe_governed_runtime()
    after = datetime.now(timezone.utc)
    captured = datetime.fromisoformat(str(payload["captured_at"]))
    assert before <= captured <= after
    assert payload["captured_at"] != "2000-01-01T00:00:00Z"


def test_digest_round_trip_and_observer_does_not_write(
    valid_observation: tuple[Path, Path, Path],
) -> None:
    runtime_root, publisher_root, source_root = valid_observation
    state_path = runtime_root / evidence_module.STATE_RELATIVE_PATH
    route_path = runtime_root / TOP5_ROUTE_STATE_PATH
    before = {
        path: path.read_bytes()
        for path in (state_path, route_path)
    }
    before_files = sorted(path.relative_to(runtime_root) for path in runtime_root.rglob("*"))
    payload = observe_governed_runtime()
    after_files = sorted(path.relative_to(runtime_root) for path in runtime_root.rglob("*"))
    assert payload["status"] == "READY"
    assert verify_artifact_digest(payload)
    assert before == {path: path.read_bytes() for path in (state_path, route_path)}
    assert before_files == after_files
    assert not (route_path.with_suffix(route_path.suffix + ".lock")).exists()
    assert source_root.is_dir() and publisher_root.is_dir()


def test_observer_uses_only_bounded_local_git_checks(
    valid_observation: tuple[Path, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []
    real_run = evidence_module.subprocess.run

    def wrapped_run(*args: object, **kwargs: object) -> object:
        command = args[0] if args else kwargs.get("args")
        assert isinstance(command, list)
        calls.append(command)
        return real_run(*args, **kwargs)

    monkeypatch.setattr(evidence_module.subprocess, "run", wrapped_run)
    assert observe_governed_runtime()["status"] == "READY"
    assert calls
    assert all(command[:3] == ["git", "-C", command[2]] for command in calls)
    assert all("status" in command for command in calls)


def test_fresh_underlying_state_is_bound_into_b1_payload(
    valid_observation: tuple[Path, Path, Path],
) -> None:
    runtime_root, publisher_root, _ = valid_observation
    state_path = runtime_root / evidence_module.STATE_RELATIVE_PATH
    state = _state(runtime_root, publisher_root)
    _write_json(state_path, state)
    payload = observe_governed_runtime()
    assert payload["status"] == "READY"
    assert payload["runtime_state_observed_at"] == state["observed_at"]
    assert datetime.fromisoformat(str(payload["captured_at"])) >= datetime.fromisoformat(
        str(payload["runtime_state_observed_at"])
    )
    _validate_runtime(
        payload,
        now=datetime.now(timezone.utc),
        model={"source_sha": SOURCE_SHA},
    )


@pytest.mark.parametrize(
    "observed_at",
    [
        datetime.now(timezone.utc) - timedelta(seconds=901),
        datetime.now(timezone.utc) + timedelta(seconds=60),
    ],
)
def test_stale_or_future_underlying_state_blocks(
    valid_observation: tuple[Path, Path, Path], observed_at: datetime
) -> None:
    runtime_root, publisher_root, _ = valid_observation
    _write_json(
        runtime_root / evidence_module.STATE_RELATIVE_PATH,
        _state(runtime_root, publisher_root, observed_at=observed_at.isoformat()),
    )
    result = observe_governed_runtime()
    assert result["status"] == "BLOCKED"
    assert "blockers" in result


def test_old_state_cannot_become_fresh_by_wrapping_it_now(
    valid_observation: tuple[Path, Path, Path],
) -> None:
    runtime_root, publisher_root, _ = valid_observation
    old_state = _state(
        runtime_root,
        publisher_root,
        observed_at=(datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(),
        captured_at=datetime.now(timezone.utc).isoformat(),
    )
    _write_json(runtime_root / evidence_module.STATE_RELATIVE_PATH, old_state)
    result = observe_governed_runtime()
    assert result["status"] == "BLOCKED"


def test_old_source_release_is_allowed_when_active_state_observation_is_fresh(
    valid_observation: tuple[Path, Path, Path],
) -> None:
    runtime_root, publisher_root, _ = valid_observation
    payload = observe_governed_runtime()
    assert payload["status"] == "READY"
    assert payload["source_release_resolution"]["method"] == "operator-release-record"
    assert payload["runtime_data_sha"] == RUNTIME_SHA
    stored = json.loads(
        (runtime_root / evidence_module.STATE_RELATIVE_PATH).read_text(encoding="utf-8")
    )
    assert payload["runtime_state_observed_at"] == stored["observed_at"]


def test_state_producer_generates_timestamp_atomically_and_keeps_private_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime_root = tmp_path / "runtime-state"
    monkeypatch.setattr(state_module, "governed_runtime_root", lambda: runtime_root)
    state = {
        "provider_authority": "the_odds_api",
        "active_provider_order": ["the_odds_api"],
        "runtime_data_sha": RUNTIME_SHA,
        "publisher_workspace_root": str(tmp_path / "publisher"),
    }
    path = state_module.write_governed_runtime_state(state)
    stored = json.loads(path.read_text(encoding="utf-8"))
    assert path == runtime_root / state_module.STATE_RELATIVE_PATH
    assert path.stat().st_mode & 0o777 == 0o600
    observed_at = datetime.fromisoformat(stored["observed_at"])
    assert datetime.now(timezone.utc) - observed_at < timedelta(seconds=5)
    assert "observed_at" not in state
    assert not list(path.parent.glob(f".{path.name}.*.tmp"))
    with pytest.raises(state_module.GovernedRuntimeStateWriteError):
        state_module.write_governed_runtime_state({**state, "observed_at": "2000-01-01T00:00:00Z"})
