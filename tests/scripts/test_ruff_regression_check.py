import json
from types import SimpleNamespace

from scripts import ruff_regression_check


def _run_gate(tmp_path, monkeypatch, diagnostics):
    baseline = tmp_path / ".ruff_baseline.json"
    baseline.write_text(
        json.dumps([{"filename": "src/existing.py", "code": "UP017"}]),
        encoding="utf-8",
    )
    monkeypatch.setattr(ruff_regression_check, "BASELINE_PATH", baseline)
    monkeypatch.setattr(
        ruff_regression_check.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=1,
            stdout=json.dumps(diagnostics),
            stderr="",
        ),
    )
    return ruff_regression_check.main()


def test_new_path_rule_violation_fails_regression_gate(tmp_path, monkeypatch, capsys):
    result = _run_gate(
        tmp_path,
        monkeypatch,
        [
            {"filename": "/repo/src/existing.py", "code": "UP017"},
            {"filename": "/repo/src/new.py", "code": "UP017"},
        ],
    )

    assert result == 1
    assert "src/new.py: +1x UP017" in capsys.readouterr().err


def test_baseline_or_reduced_violation_count_passes(tmp_path, monkeypatch):
    result = _run_gate(
        tmp_path,
        monkeypatch,
        [{"filename": "/repo/src/existing.py", "code": "UP017"}],
    )

    assert result == 0
