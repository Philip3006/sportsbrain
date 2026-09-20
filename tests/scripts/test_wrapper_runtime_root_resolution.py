"""Focused contracts for launchd wrappers resolving their own checkout root."""
from __future__ import annotations

import re
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
WRAPPER_NAMES = (
    "aggregate_health_cron.sh",
    "closing_odds_cron.sh",
    "live_score_trigger.sh",
    "odds_refresh_cron.sh",
    "prematch_scan_cron.sh",
    "scan_cron.sh",
    "settle_cron.sh",
)
HISTORICAL_ROOT = "/Users/philiprassillier/sportsbrain"
DEDICATED_RUNTIME_ROOT = "/Users/philiprassillier/sportsbrain-runtime-current-dac"
SCRIPT_DIR_LINE = 'SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"'
SPORTSBRAIN_DIR_LINE = 'SPORTSBRAIN_DIR="$(cd -- "$SCRIPT_DIR/.." && pwd -P)"'
SOURCE_RE = re.compile(r'^(?:source|\.) "\$SPORTSBRAIN_DIR/([^"]+)"$')


def _wrapper_source(name: str) -> str:
    return (ROOT / "scripts" / name).read_text()


def _root_prefix(source: str) -> str:
    lines = source.splitlines()
    script_index = lines.index(SCRIPT_DIR_LINE)
    root_index = lines.index(SPORTSBRAIN_DIR_LINE)
    assert script_index < root_index
    return "\n".join(lines[: root_index + 1]) + "\n"


def _run_copied_probe(
    name: str,
    source: str,
    tmp_path: Path,
    body: str,
) -> tuple[Path, subprocess.CompletedProcess[str]]:
    checkout = tmp_path / "synthetic-checkout"
    scripts = checkout / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    caller = tmp_path / "caller"
    caller.mkdir()

    wrapper = scripts / name
    shutil.copy2(ROOT / "scripts" / name, wrapper)
    wrapper.write_text(_root_prefix(source) + body)
    wrapper.chmod(0o755)

    result = subprocess.run(
        ["bash", str(wrapper)],
        cwd=caller,
        text=True,
        capture_output=True,
        check=False,
    )
    return checkout, result


@pytest.mark.parametrize("name", WRAPPER_NAMES)
def test_wrappers_have_self_location_root_and_no_machine_checkout_literals(name: str) -> None:
    source = _wrapper_source(name)

    assert HISTORICAL_ROOT not in source
    assert DEDICATED_RUNTIME_ROOT not in source
    assert SCRIPT_DIR_LINE in source
    assert SPORTSBRAIN_DIR_LINE in source


@pytest.mark.parametrize("name", WRAPPER_NAMES)
def test_copied_wrapper_resolves_synthetic_checkout(name: str, tmp_path: Path) -> None:
    checkout, result = _run_copied_probe(
        name,
        _wrapper_source(name),
        tmp_path,
        'printf \'%s\\n%s\\n\' "$SCRIPT_DIR" "$SPORTSBRAIN_DIR"\n',
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [str(checkout / "scripts"), str(checkout)]
    assert HISTORICAL_ROOT not in result.stdout


@pytest.mark.parametrize("name", WRAPPER_NAMES)
def test_copied_wrapper_sources_helpers_from_synthetic_checkout(name: str, tmp_path: Path) -> None:
    source = _wrapper_source(name)
    source_lines = []
    relative_targets = []
    for line in source.splitlines():
        stripped = line.strip()
        match = SOURCE_RE.fullmatch(stripped)
        if match:
            source_lines.append(stripped)
            relative_targets.append(match.group(1))

    assert relative_targets
    checkout = tmp_path / "synthetic-checkout"
    for relative_target in relative_targets:
        target = checkout / relative_target
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            f'printf \'%s\\n\' {shlex.quote(relative_target)} >> "$PROBE_LOG"\n'
        )

    probe_log = checkout / "probe.log"
    body = (
        f"PROBE_LOG={shlex.quote(str(probe_log))}\n"
        + "\n".join(source_lines)
        + '\nprintf \'%s\\n\' "root=$SPORTSBRAIN_DIR"\n'
    )
    _, result = _run_copied_probe(name, source, tmp_path, body)

    assert result.returncode == 0, result.stderr
    assert probe_log.read_text().splitlines() == relative_targets
    assert result.stdout.splitlines() == [f"root={checkout}"]


@pytest.mark.parametrize("name", WRAPPER_NAMES)
def test_wrapper_safety_contract_sentinels_remain(name: str) -> None:
    source = _wrapper_source(name)
    required = {
        "aggregate_health_cron.sh": (
            'health_start "aggregate_health"',
            'health_finish "aggregate_health" "$EXIT_CODE"',
            'exit "$EXIT_CODE"',
        ),
        "closing_odds_cron.sh": (
            'source "$SPORTSBRAIN_DIR/scripts/_require_main_branch.sh"',
            "runtime_publish_staged_artifacts",
        ),
        "live_score_trigger.sh": (
            "LIVE_EXIT=${PIPESTATUS[0]}",
            'health_finish "live_score_push" "$EXIT_CODE"',
            "runtime_publish_staged_artifacts",
        ),
        "odds_refresh_cron.sh": (
            'exec /Library/Frameworks/Python.framework/Versions/3.13/bin/python3 scripts/refresh_odds.py',
        ),
        "prematch_scan_cron.sh": (
            "SETTLE_EXIT=$?",
            'health_finish "prematch_scan" "$SETTLE_EXIT"',
        ),
        "scan_cron.sh": (
            "SETTLE_EXIT=$?",
            'health_finish "daily_scan" "$SETTLE_EXIT"',
        ),
        "settle_cron.sh": (
            'health_finish "settle" "$EXIT_CODE"',
            "scripts/settle_bets.py",
        ),
    }[name]

    for marker in required:
        assert marker in source, marker
    for forbidden in ("git add", "git commit", "git push", "git rebase", "reset --hard"):
        assert forbidden not in source, forbidden


@pytest.mark.parametrize("name", WRAPPER_NAMES)
def test_wrapper_shell_syntax(name: str) -> None:
    result = subprocess.run(
        ["bash", "-n", str(ROOT / "scripts" / name)],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
