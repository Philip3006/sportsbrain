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
    assert '--source-release-sha "$SOURCE_RELEASE_SHA"' in workflow
    assert '--source-release-sha "$(git rev-parse HEAD)"' not in workflow
    assert workflow.count("date -u +%Y-%m-%dT%H:%M:%SZ") == 1
    assert workflow.count('--as-of "$CYCLE_AS_OF"') == 3


def test_live_workflow_checkout_has_full_history_for_ancestor_validation():
    workflow = (ROOT / ".github/workflows/nations_league_live_cycle.yml").read_text(
        encoding="utf-8"
    )
    checkout_start = workflow.index("      - uses: actions/checkout@v4")
    setup_python_start = workflow.index("      - uses: actions/setup-python@v5")
    checkout = workflow[checkout_start:setup_python_start]

    assert "actions/checkout@v4" in checkout
    assert "fetch-depth: 0" in checkout
    assert "fetch-depth: 1" not in checkout


def test_live_workflow_installs_dependencies_before_lifecycle():
    workflow = (ROOT / ".github/workflows/nations_league_live_cycle.yml").read_text(
        encoding="utf-8"
    )
    setup_python_start = workflow.index("      - uses: actions/setup-python@v5")
    execute_start = workflow.index("      - name: Execute current LIVE lifecycle")
    setup_and_install = workflow[setup_python_start:execute_start]

    assert "cache: 'pip'" in setup_and_install
    assert "pip install -r requirements.txt" in setup_and_install


def test_live_workflow_propagates_lifecycle_failure_through_tee():
    workflow = (ROOT / ".github/workflows/nations_league_live_cycle.yml").read_text(
        encoding="utf-8"
    )
    execute_start = workflow.index("      - name: Execute current LIVE lifecycle")
    bundle_start = workflow.index("      - name: Rebuild canonical LIVE public bundle")
    publication_start = workflow.index(
        "      - name: Publish through the canonical Worker path when configured"
    )
    lifecycle = workflow[execute_start:bundle_start]
    bundle = workflow[bundle_start:publication_start]

    assert "set -o pipefail" in lifecycle
    assert lifecycle.index("set -o pipefail") < lifecycle.index(
        "python3 scripts/nations_league_live_cycle.py"
    )
    assert "| tee /tmp/nations-league-live-cycle.json" in lifecycle
    assert "if:" not in bundle


def test_live_workflow_adds_no_provider_or_financial_action():
    workflow = (
        (ROOT / ".github/workflows/nations_league_live_cycle.yml")
        .read_text(encoding="utf-8")
        .casefold()
    )

    for forbidden in (
        "the_odds_api",
        "therundown",
        "isports",
        "ledger",
        "bankroll",
        "betting",
    ):
        assert forbidden not in workflow


def test_live_workflow_guards_optional_publication_inside_shell():
    workflow = (ROOT / ".github/workflows/nations_league_live_cycle.yml").read_text(
        encoding="utf-8"
    )
    publication_start = workflow.index(
        "      - name: Publish through the canonical Worker path when configured"
    )
    persistence_start = workflow.index(
        "      - name: Persist changed LIVE state without creating a PR"
    )
    publication = workflow[publication_start:persistence_start]

    assert "on:\n  workflow_dispatch:" in workflow
    assert "- cron: '*/15 * * * *'" in workflow
    assert not any(
        line.lstrip().startswith("if:") and "secrets." in line
        for line in workflow.splitlines()
    )
    assert "SIGNALS_CLOUD_URL: ${{ secrets.SIGNALS_CLOUD_URL }}" in publication
    assert "SIGNALS_API_TOKEN: ${{ secrets.SIGNALS_API_TOKEN }}" in publication
    assert (
        'if [ -z "$SIGNALS_CLOUD_URL" ] || [ -z "$SIGNALS_API_TOKEN" ]; then'
        in publication
    )
    assert (
        'echo "Canonical Worker publication is not configured; skipping."'
        in publication
    )
    assert "exit 0" in publication
    assert "continue-on-error: true" in publication
    assert "republish_signals_to_cloud.py --expected-nl-digest" in publication
    assert "if: ${{ always() }}" in workflow[persistence_start:]
    assert "bash scripts/_bot_commit_push.sh" in workflow[persistence_start:]
