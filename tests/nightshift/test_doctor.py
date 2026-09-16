from __future__ import annotations

from pathlib import Path

from src.nightshift.doctor import run_doctor


def test_doctor_is_machine_readable_and_marks_secrets_redacted(tmp_path: Path) -> None:
    report = run_doctor(
        repo_root=tmp_path, runtime_dir=tmp_path / "runtime", repo_paths={}
    )
    assert report["secrets_redacted"] is True
    assert isinstance(report["checks"], list)
    assert all(
        set(item) >= {"name", "ok", "detail", "severity"} for item in report["checks"]
    )
