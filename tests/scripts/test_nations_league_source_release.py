from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
SCRIPT = ROOT / "scripts/resolve_nations_league_source_release.py"


def _module():
    spec = importlib.util.spec_from_file_location("nl_source_release", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_workflow_source_release_comes_from_successful_provenance_marker():
    module = _module()
    marker = ROOT / "docs/data/provenance_meta.json"
    expected = json.loads(marker.read_text(encoding="utf-8"))["source_release_sha"]
    actual = module.resolve_source_release_sha(marker, repository_root=ROOT)
    head = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
    ).strip()
    assert actual == expected
    assert actual != head


def test_invalid_marker_is_rejected(tmp_path):
    module = _module()
    marker = tmp_path / "provenance_meta.json"
    marker.write_text(json.dumps({"schema_version": "1", "source_release_sha": "HEAD"}))
    with pytest.raises(ValueError, match="malformed"):
        module.resolve_source_release_sha(marker, repository_root=ROOT)


def test_live_workflow_uses_marker_release_and_one_shared_cutoff():
    workflow = (ROOT / ".github/workflows/nations_league_live_cycle.yml").read_text(
        encoding="utf-8"
    )
    assert "resolve_nations_league_source_release.py" in workflow
    assert "--source-release-sha \"$SOURCE_RELEASE_SHA\"" in workflow
    assert "--source-release-sha \"$(git rev-parse HEAD)\"" not in workflow
    assert workflow.count('date -u +%Y-%m-%dT%H:%M:%SZ') == 1
    assert workflow.count('--as-of \"$CYCLE_AS_OF\"') == 3
