#!/usr/bin/env python3
"""Deterministic, offline post-merge verification for the Top-5 train.

The verifier never merges, commits, pushes, resets, checks out, deploys, or
calls a provider.  It archives the selected ``HEAD`` into a temporary
directory, blocks sockets in the child process, and runs the relevant tests
against that disposable snapshot.  The working checkout is therefore not a
test-output target.

Typical use after a train step::

    python3 scripts/post_merge_verifier.py \
        --step 94 \
        --baseline-sha <pre-merge-main-sha> \
        --format json

Use ``--step follow-up`` with ``--test-path`` and ``--required-path`` for a
later receipt or shadow follow-up PR.  The fixed train order is deliberately
independent of health, runtime, result, or cache data.
"""

from __future__ import annotations

import argparse
import ast
import io
import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path

TRAIN_ORDER = ("88", "91", "92", "94", "95")
VOLATILE_PATH_PREFIXES = (
    "data/cache/",
    "docs/data/",
    "models/",
    "results/",
)
MUTATING_GIT_COMMANDS = frozenset(
    {"merge", "commit", "push", "reset", "checkout", "rebase", "cherry-pick"}
)

STEP_TEST_PATTERNS: dict[str, tuple[str, ...]] = {
    "88": ("tests/football/test_top5_*.py", "tests/football/odds/test_therundown.py"),
    "91": ("tests/football/test_app_b4_therundown_top5_shadow_contract.py",),
    "92": ("tests/football/test_top5_polling_planner.py",),
    "94": (
        "tests/football/test_top5_therundown_qualification.py",
        "tests/football/test_top5_therundown_qualification_bridge.py",
    ),
    "95": ("tests/football/test_top5_therundown_shadow_canary.py",),
}

STEP_REQUIRED_PATHS: dict[str, tuple[str, ...]] = {
    "88": (
        "src/football/odds/therundown.py",
        "src/football/provider_cascade/adapters.py",
    ),
    "91": ("src/football/top5_real_shadow_contracts.py",),
    "92": ("src/football/top5_polling_planner.py",),
    "94": (
        "src/football/top5_therundown_qualification.py",
        "src/football/top5_therundown_qualification_bridge.py",
    ),
    "95": ("src/football/top5_therundown_shadow_canary.py",),
}

STEP_REQUIRED_SYMBOLS: dict[str, tuple[str, ...]] = {
    "88": ("TheRundownExperimentalAdapter", "fetch_observations"),
    "91": ("RealShadowExperiment",),
    "92": ("Top5PollingPlannerInputs", "plan_top5_polling"),
    "94": ("TheRundownQualificationStatus", "bridge_therundown_observations"),
    "95": ("TheRundownControlledShadowCanary",),
}


@dataclass(frozen=True)
class Check:
    name: str
    passed: bool
    detail: str


@dataclass(frozen=True)
class CommandResult:
    command: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str


def _normalize_step(step: str) -> str:
    normalized = step.strip().lower().removeprefix("#")
    if normalized == "follow-up":
        return normalized
    if normalized not in TRAIN_ORDER:
        raise ValueError(f"unsupported step {step!r}; expected 88, 91, 92, 94, 95, or follow-up")
    return normalized


def _step_prefix(step: str) -> tuple[str, ...]:
    if step == "follow-up":
        return TRAIN_ORDER
    return TRAIN_ORDER[: TRAIN_ORDER.index(step) + 1]


def _required_paths(step: str, overrides: Sequence[str]) -> tuple[str, ...]:
    if step == "follow-up":
        return tuple(dict.fromkeys(overrides))
    paths: list[str] = []
    for completed in _step_prefix(step):
        paths.extend(STEP_REQUIRED_PATHS[completed])
    paths.extend(overrides)
    return tuple(dict.fromkeys(paths))


def _test_patterns(step: str, overrides: Sequence[str]) -> tuple[str, ...]:
    if step == "follow-up":
        return tuple(overrides) or (
            "tests/football/test_top5_*.py",
            "tests/football/odds/test_therundown.py",
        )
    patterns: list[str] = ["tests/football/test_top5_*.py"]
    for completed in _step_prefix(step):
        patterns.extend(STEP_TEST_PATTERNS[completed])
    patterns.extend(overrides)
    return tuple(dict.fromkeys(patterns))


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


def _head(root: Path) -> str:
    return _git(root, "rev-parse", "HEAD")


def _changed_paths(root: Path, baseline_sha: str, head_sha: str) -> tuple[str, ...]:
    output = _git(root, "diff", "--name-only", f"{baseline_sha}..{head_sha}")
    return tuple(line for line in output.splitlines() if line)


def _definition_counts(root: Path) -> dict[str, int]:
    counts: dict[str, int] = {}
    source_root = root / "src" / "football"
    for path in sorted(source_root.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, SyntaxError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                counts[node.name] = counts.get(node.name, 0) + 1
    return counts


def _read(root: Path, relative: str) -> str:
    return (root / relative).read_text(encoding="utf-8")


def _check_required_paths(root: Path, paths: Sequence[str]) -> Check:
    missing = [path for path in paths if not (root / path).is_file()]
    return Check(
        "required-contract-paths",
        not missing,
        "all required paths present" if not missing else f"missing: {', '.join(missing)}",
    )


def _check_unique_symbols(root: Path, symbols: Sequence[str]) -> Check:
    counts = _definition_counts(root)
    invalid = {symbol: counts.get(symbol, 0) for symbol in symbols if counts.get(symbol) != 1}
    return Check(
        "contract-symbols-exactly-once",
        not invalid,
        "all expected symbols have one definition"
        if not invalid
        else "invalid definition counts: " + json.dumps(invalid, sort_keys=True),
    )


def _check_authority_and_candidate_boundary(root: Path) -> Check:
    contracts = _read(root, "src/football/provider_cascade/contracts.py")
    router = _read(root, "src/football/provider_cascade/router.py")
    rundown = _read(root, "src/football/odds/therundown.py")
    active_ok = (
        'FOOTBALL_PROVIDER_REPERTOIRE = ("the_odds_api",)' in contracts
        and "DEFAULT_PROVIDER_ORDER = FOOTBALL_PROVIDER_REPERTOIRE" in contracts
        and '"the_odds_api": TheOddsAPIAdapter()' in router
        and "therundown" not in contracts.lower()
        and "therundown" not in router.lower()
    )
    candidate_ok = (
        'THERUNDOWN_PROVIDER_NAME = "therundown_experimental"' in rundown
        and "candidate-only" in rundown.lower()
        and "not registered" in rundown.lower()
    )
    return Check(
        "authority-and-candidate-boundary",
        active_ok and candidate_ok,
        "The Odds API is the sole active authority; TheRundown is candidate-only"
        if active_ok and candidate_ok
        else "active authority or candidate-only boundary is inconsistent",
    )


def _check_fetch_observations(root: Path, step: str) -> Check:
    source = _read(root, "src/football/odds/therundown.py")
    tree = ast.parse(source)
    definitions = [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "fetch_observations"
    ]
    bridge_path = root / "src/football/top5_therundown_qualification_bridge.py"
    bridge = bridge_path.read_text(encoding="utf-8") if bridge_path.is_file() else ""
    bridge_ready = step in {"94", "95", "follow-up"}
    passed = len(definitions) == 1 and (not bridge_ready or "fetch_observations" in bridge)
    return Check(
        "fetch-observations-qualification-path",
        passed,
        "one adapter fetch_observations method remains the qualification path"
        if passed
        else "fetch_observations qualification contract is missing or duplicated",
    )


def _check_separation(root: Path, step: str) -> Check:
    qualification_path = root / "src/football/top5_therundown_qualification.py"
    bridge_path = root / "src/football/top5_therundown_qualification_bridge.py"
    canary_path = root / "src/football/top5_therundown_shadow_canary.py"
    qualification = (
        qualification_path.read_text(encoding="utf-8") if qualification_path.is_file() else ""
    )
    bridge = bridge_path.read_text(encoding="utf-8") if bridge_path.is_file() else ""
    canary = canary_path.read_text(encoding="utf-8") if canary_path.is_file() else ""
    qualification_markers = (
        '"production_authority_changed": False',
        '"publication_authorized": False',
        '"production_activation_authorized": False',
    )
    bridge_markers = ('"publication_enabled", False',)
    canary_markers = (
        "publication: bool = False",
        "production_activation: bool = False",
        "scheduler_registered: bool = False",
        "ledger_mutated: bool = False",
    )
    qualification_ready = step in {"94", "95", "follow-up"}
    canary_ready = step in {"95", "follow-up"}
    passed = (
        (not qualification_ready or (qualification and all(item in qualification for item in qualification_markers)))
        and (not qualification_ready or (bridge and all(item in bridge for item in bridge_markers)))
        and (not canary_ready or (canary and all(item in canary for item in canary_markers)))
    )
    return Check(
        "authority-activation-publication-separation",
        passed,
        "authority, activation, publication, scheduler, and ledger flags remain separate and disabled"
        if passed
        else "one or more separation/safety markers are missing",
    )


def _check_no_duplicate_pr93(root: Path) -> Check:
    duplicate_paths = [
        path.relative_to(root).as_posix()
        for base in (root / "src", root / "scripts")
        for path in base.rglob("*")
        if path.is_file() and "pr93" in path.name.lower()
    ]
    return Check(
        "no-duplicate-pr93-implementation",
        not duplicate_paths,
        "no PR #93-specific source implementation exists"
        if not duplicate_paths
        else "PR #93-named source paths found: " + ", ".join(duplicate_paths),
    )


def _check_changed_side_effects(root: Path, changed_paths: Sequence[str]) -> Check:
    path_violations = [
        path
        for path in changed_paths
        if path.startswith(("launchd/", "src/nightshift/"))
        or (path.startswith("scripts/") and "cron" in path.lower())
    ]
    enable_pattern = re.compile(
        r"(?:publication_enabled|production_activation|scheduler_registered|ledger_mutated)\s*[:=]\s*True"
    )
    violations = list(path_violations)
    for relative in changed_paths:
        if not relative.endswith(".py") or not relative.startswith(
            ("src/football/", "scripts/")
        ):
            continue
        text = _read(root, relative)
        tree = ast.parse(text, filename=relative)
        imported_modules = [
            node.module or ""
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
        ] + [
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        ]
        forbidden_import = any(
            re.search(r"(?:betting|cloudflare|wrangler|ledger|scheduler)", module, re.IGNORECASE)
            for module in imported_modules
        )
        forbidden_call = "upload_signals_to_cloud" in text
        if forbidden_import or forbidden_call:
            violations.append(f"forbidden side-effect import/call: {relative}")
        if enable_pattern.search(text):
            violations.append(f"safety flag enabled in production source: {relative}")
    return Check(
        "no-scheduler-publication-betting-mutation-path",
        not violations,
        "no scheduler, publication, betting, ledger, or Cloudflare mutation path in train diff"
        if not violations
        else "; ".join(violations),
    )


def _check_runtime_drift(changed_paths: Sequence[str]) -> Check:
    ignored = [
        path
        for path in changed_paths
        if path.startswith(VOLATILE_PATH_PREFIXES)
    ]
    return Check(
        "runtime-health-drift-is-order-independent",
        True,
        "fixed train order ignores runtime/health/cache paths"
        + (f"; ignored paths: {', '.join(ignored)}" if ignored else ""),
    )


def _offline_sitecustomize(snapshot: Path) -> None:
    (snapshot / "sitecustomize.py").write_text(
        "import socket\n\n"
        "def _blocked(*args, **kwargs):\n"
        "    raise RuntimeError('POST_MERGE_VERIFIER_NETWORK_BLOCKED')\n\n"
        "socket.socket.connect = _blocked\n"
        "socket.socket.connect_ex = _blocked\n"
        "socket.create_connection = _blocked\n"
        "socket.getaddrinfo = _blocked\n",
        encoding="utf-8",
    )


def _snapshot(root: Path) -> tempfile.TemporaryDirectory[str]:
    temporary = tempfile.TemporaryDirectory(prefix="sportsbrain-post-merge-verify-")
    archive = subprocess.run(
        ["git", "archive", "--format=tar", "HEAD"],
        cwd=root,
        check=True,
        capture_output=True,
    ).stdout
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as tar:
        try:
            tar.extractall(temporary.name, filter="data")
        except TypeError:
            tar.extractall(temporary.name)
    snapshot = Path(temporary.name)
    _offline_sitecustomize(snapshot)
    return temporary


def _run_command(
    command: Sequence[str], *, cwd: Path, timeout: int, snapshot: Path
) -> CommandResult:
    environment = os.environ.copy()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["PYTHONNOUSERSITE"] = "1"
    environment["SPORTSBRAIN_POST_MERGE_VERIFIER_OFFLINE"] = "1"
    environment["PYTHONPATH"] = os.pathsep.join(
        item for item in (str(snapshot), environment.get("PYTHONPATH", "")) if item
    )
    result = subprocess.run(
        list(command),
        cwd=cwd,
        env=environment,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    return CommandResult(
        tuple(command), result.returncode, result.stdout[-6000:], result.stderr[-6000:]
    )


def _expand_tests(root: Path, patterns: Sequence[str]) -> tuple[str, ...]:
    paths: list[str] = []
    for pattern in patterns:
        matches = sorted(root.glob(pattern)) if any(char in pattern for char in "*?[") else [root / pattern]
        paths.extend(
            path.relative_to(root).as_posix()
            for path in matches
            if path.is_file()
        )
    return tuple(dict.fromkeys(paths))


def _pytest_summary(stdout: str) -> str:
    lines = [line.strip() for line in stdout.splitlines() if line.strip()]
    return lines[-1] if lines else "no pytest summary"


def verify(
    root: Path,
    *,
    step: str,
    baseline_sha: str | None = None,
    required_paths: Sequence[str] = (),
    test_paths: Sequence[str] = (),
    timeout: int = 600,
) -> dict[str, object]:
    root = root.resolve()
    step = _normalize_step(step)
    head_sha = _head(root)
    baseline = baseline_sha or _git(root, "rev-parse", "HEAD^")
    changed = _changed_paths(root, baseline, head_sha)
    checks: list[Check] = []
    checks.append(_check_required_paths(root, _required_paths(step, required_paths)))
    checks.append(_check_unique_symbols(root, _required_symbols(step)))
    checks.append(_check_authority_and_candidate_boundary(root))
    checks.append(_check_fetch_observations(root, step))
    checks.append(_check_separation(root, step))
    checks.append(_check_no_duplicate_pr93(root))
    checks.append(_check_changed_side_effects(root, changed))
    checks.append(_check_runtime_drift(changed))
    if step == "follow-up":
        checks.append(
            Check(
                "follow-up-explicit-scope",
                bool(required_paths and test_paths),
                "follow-up supplies explicit required paths and tests"
                if required_paths and test_paths
                else "follow-up requires at least one --required-path and --test-path",
            )
        )

    command_results: list[CommandResult] = []
    patterns = _test_patterns(step, test_paths)
    tests: dict[str, object] = {"patterns": patterns, "summary": None}
    with _snapshot(root) as snapshot_name:
        snapshot = Path(snapshot_name)
        compile_result = _run_command(
            [sys.executable, "-m", "compileall", "-q", "src"],
            cwd=snapshot,
            timeout=timeout,
            snapshot=snapshot,
        )
        command_results.append(compile_result)
        checks.append(
            Check(
                "repository-imports-compile",
                compile_result.returncode == 0,
                "compileall passed" if compile_result.returncode == 0 else compile_result.stderr,
            )
        )
        import_modules = [
            "src.football.provider_cascade.contracts",
            "src.football.provider_cascade.router",
            "src.football.odds.therundown",
        ]
        if step in {"91", "92", "94", "95", "follow-up"}:
            import_modules.append("src.football.top5_real_shadow_contracts")
        if step in {"92", "94", "95", "follow-up"}:
            import_modules.append("src.football.top5_polling_planner")
        if step in {"94", "95", "follow-up"}:
            import_modules.extend(
                (
                    "src.football.top5_therundown_qualification",
                    "src.football.top5_therundown_qualification_bridge",
                )
            )
        if step in {"95", "follow-up"}:
            import_modules.append("src.football.top5_therundown_shadow_canary")
        import_code = "import " + ", ".join(import_modules)
        import_result = _run_command(
            [sys.executable, "-c", import_code],
            cwd=snapshot,
            timeout=timeout,
            snapshot=snapshot,
        )
        command_results.append(import_result)
        checks.append(
            Check(
                "repository-imports",
                import_result.returncode == 0,
                "required modules imported" if import_result.returncode == 0 else import_result.stderr,
            )
        )
        test_files = _expand_tests(snapshot, patterns)
        missing_tests = [pattern for pattern in patterns if not _expand_tests(snapshot, (pattern,))]
        if missing_tests:
            tests["summary"] = f"missing test paths: {', '.join(missing_tests)}"
            checks.append(Check("relevant-tests", False, tests["summary"]))
        else:
            pytest_result = _run_command(
                [sys.executable, "-m", "pytest", "-q", *test_files],
                cwd=snapshot,
                timeout=timeout,
                snapshot=snapshot,
            )
            command_results.append(pytest_result)
            tests["summary"] = _pytest_summary(pytest_result.stdout)
            tests["returncode"] = pytest_result.returncode
            checks.append(
                Check(
                    "relevant-tests",
                    pytest_result.returncode == 0,
                    str(tests["summary"]),
                )
            )

    passed = all(check.passed for check in checks)
    return {
        "status": "PASS" if passed else "FAIL",
        "step": step,
        "train_order": TRAIN_ORDER,
        "train_prefix": _step_prefix(step),
        "head_sha": head_sha,
        "baseline_sha": baseline,
        "changed_paths": changed,
        "runtime_data_not_used_for_ordering": True,
        "network_requests": 0,
        "checks": [asdict(check) for check in checks],
        "tests": tests,
        "commands": [asdict(result) for result in command_results],
    }


def _required_symbols(step: str) -> tuple[str, ...]:
    symbols: list[str] = []
    for completed in _step_prefix(step):
        symbols.extend(STEP_REQUIRED_SYMBOLS[completed])
    return tuple(dict.fromkeys(symbols))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--step", required=True, help="88, 91, 92, 94, 95, or follow-up")
    parser.add_argument("--baseline-sha", help="pre-merge main SHA used for the side-effect diff")
    parser.add_argument("--required-path", action="append", default=[])
    parser.add_argument("--test-path", action="append", default=[])
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--format", choices=("text", "json"), default="text")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = verify(
            args.repo_root,
            step=args.step,
            baseline_sha=args.baseline_sha,
            required_paths=args.required_path,
            test_paths=args.test_path,
            timeout=args.timeout,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired, ValueError) as exc:
        result = {"status": "FAIL", "error": f"{type(exc).__name__}: {exc}"}
    if args.format == "json":
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(f"POST-MERGE VERIFIER: {result['status']}")
        for check in result.get("checks", []):
            mark = "PASS" if check["passed"] else "FAIL"
            print(f"[{mark}] {check['name']}: {check['detail']}")
        if "head_sha" in result:
            print(f"HEAD: {result['head_sha']}")
            print(f"BASELINE: {result['baseline_sha']}")
            print(f"TESTS: {result['tests']['summary']}")
    return 0 if result.get("status") == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
