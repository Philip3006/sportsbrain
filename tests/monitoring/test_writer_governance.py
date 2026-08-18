"""P0D-001 — Runtime writer governance deterministic tests.

Invariants verified:
  I1. All paths staged by bot workflows are within the bot path allowlist.
  I2. Source-code paths (src/, scripts/, tests/, .github/, cloudflare/) are
      outside the allowlist and must never be staged by runtime writers.
  I3. No active (non-disabled) runtime workflow uses bare `git push || true`
      or `git push` without retry — concurrent writes must retry safely.
  I4. docs/data/provenance_meta.json is written only by ci_gates.yml (source
      gate), not by any runtime workflow — source_release_sha stays separate.
  I5. Bot writer identity is observable: every workflow that runs `git commit`
      or delegates to `_bot_commit_push.sh` has an observable, fixed bot identity.
      The shared primitive itself enforces identity for all callers.
  I6. (Classification coverage) Every active workflow that can commit/push is
      explicitly classified; any unclassified committing workflow fails this gate.

Finding addressed: P0D-001 (Runtime Writer Governance)

IMPORTANT: Allowlist tests use the actual shell `_bot_permitted()` function
(scripts/_git_safe_push.sh) via subprocess — not a Python mirror.  This
ensures that a change to the shell allowlist that is not reflected in tests
will immediately break this gate.
"""
from __future__ import annotations

import os
import re
import subprocess
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS_DIR = ROOT / ".github" / "workflows"
GIT_SAFE_PUSH = ROOT / "scripts" / "_git_safe_push.sh"
BOT_COMMIT_PUSH = ROOT / "scripts" / "_bot_commit_push.sh"


# ── Shell allowlist bridge ────────────────────────────────────────────────────

def _shell_is_permitted(path: str) -> bool:
    """Call the actual _bot_permitted() from scripts/_git_safe_push.sh.

    This is the production enforcement path — not a Python mirror.
    """
    result = subprocess.run(
        ["bash", "-c",
         f'source "{GIT_SAFE_PUSH}" && _bot_permitted "{path}"'],
        capture_output=True,
    )
    return result.returncode == 0


# ── Workflow helpers ──────────────────────────────────────────────────────────

def _active_workflows() -> list[Path]:
    return [f for f in WORKFLOWS_DIR.glob("*.yml") if f.suffix == ".yml"]


def _runtime_workflows() -> list[Path]:
    return [f for f in _active_workflows() if f.name != "ci_gates.yml"]


def _extract_git_add_paths(workflow_text: str) -> list[str]:
    """Extract path prefixes from all `git add` commands in a workflow.

    Multi-line continuations (backslash at end of line) are joined before
    tokenising. Glob wildcards are resolved to the prefix before the first
    wildcard character so they can be checked against the allowlist.
    """
    joined = re.sub(r"\\\n\s*", " ", workflow_text)
    paths: list[str] = []
    for line in joined.splitlines():
        stripped = line.strip()
        if not stripped.startswith("git add"):
            continue
        clean = re.sub(r"\s*[|&>].*", "", stripped)
        tokens = clean.split()
        for tok in tokens:
            if tok in {"git", "add", "-f", "--", "||", "true", "\\"} or tok.startswith("-"):
                continue
            base = re.split(r"[*?\[]", tok)[0]
            if base and "/" in base:
                paths.append(base)
    return paths


# ── I1/I2: Shell allowlist — representative paths ─────────────────────────────

def test_i1_shell_permitted_runtime_paths():
    """I1: Known permitted runtime output paths all pass the actual shell _bot_permitted()."""
    permitted = [
        "docs/data/signals.json",
        "docs/data/signals_philip.json",
        "docs/data/tennis_live_scores.json",
        "docs/data/health.json",
        "docs/data/bundesliga2_live_scores.json",
        "data/cache/signal_history.jsonl",
        "data/cache/signal_performance.json",
        "data/cache/tennis_serve_snapshots/some.json",
        "data/cache/tennis_stats_history.jsonl",
        "data/cache/bundesliga2_universe.json",
        "data/cache/elo_ratings.json",
        "data/cache/elo_ratings_bl2.json",
        "data/cache/bundesliga2_closing_odds.json",
        "data/cache/bundesliga2_live_scores.json",
        "data/odds_history/tennis/some.json",
        "results/health/tennis_scan.json",
        "results/health/bundesliga2_scan.json",
        "results/health/bundesliga2_closing_odds.json",
        "results/health/bundesliga2_live_push.json",
        "results/ledger_2026.csv",
        "results/ledger_tennis_2026.csv",
        "results/scans/bl2_scan_2026.md",
        "results/tennis_live_signals.json",
        "results/tennis_scan_shadow_challenger.json",
        "results/tennis_scan_2026-08-18.md",
        "results/betting_journal.md",
        "results/tennis_cal_stats.json",
        "models/dc_bundesliga2/model.pkl",
        "models/tennis_lgbm/model.pkl",
        "models/tennis/elo_snapshot.pkl",
        "models/tennis/elo_meta.json",
        "models/tennis_calibrators/clay.pkl",
    ]
    for p in permitted:
        assert _shell_is_permitted(p), (
            f"permitted path failed actual shell _bot_permitted(): {p}"
        )


def test_i2_shell_rejects_source_paths():
    """I2: Source code paths are rejected by the actual shell _bot_permitted() (fail-closed)."""
    rejected = [
        "src/betting/kelly.py",
        "src/notifications/public_serializer.py",
        "scripts/daily_scan.py",
        "scripts/_git_safe_push.sh",
        "scripts/_bot_commit_push.sh",
        "tests/monitoring/test_writer_governance.py",
        ".github/workflows/ci_gates.yml",
        "cloudflare/worker.js",
        "docs/js/app.js",
        "docs/index.html",
        "requirements.txt",
        "ROADMAP.md",
        "CLAUDE.md",
    ]
    for p in rejected:
        assert not _shell_is_permitted(p), (
            f"source path should be rejected by shell _bot_permitted() but passed: {p}"
        )


def test_i1_all_workflow_git_add_paths_permitted():
    """I1: Every path prefix in 'git add' commands across all active workflows
    passes the actual shell _bot_permitted() function.

    Paths extracted from git add lines; glob wildcards resolved to their prefix.
    Uses the real shell function — not a Python mirror.
    """
    violations: list[tuple[str, str]] = []
    for wf in _active_workflows():
        text = wf.read_text()
        for path_prefix in _extract_git_add_paths(text):
            if not _shell_is_permitted(path_prefix) and not any(
                path_prefix.startswith(sd) for sd in ("docs/data/provenance_meta",)
            ):
                violations.append((wf.name, path_prefix))

    assert not violations, (
        "Workflow git add paths rejected by shell _bot_permitted():\n"
        + "\n".join(f"  {wf}: {p}" for wf, p in violations)
    )


# ── I1/I2: bot_assert_staged_safe() integration ───────────────────────────────

def _run_bot_assert(staged_paths: list[tuple[str, str]],
                    expect_permitted: bool) -> subprocess.CompletedProcess:
    """Run bot_assert_staged_safe() in a temp git repo with the given staged files.

    staged_paths: list of (relative_path, content) tuples.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        tmppath = Path(tmpdir)
        env = {**os.environ, "HOME": tmpdir}
        for cmd in [
            ["git", "init"],
            ["git", "config", "user.email", "test@test.com"],
            ["git", "config", "user.name", "Test"],
        ]:
            subprocess.run(cmd, cwd=tmpdir, capture_output=True, env=env)

        for rel, content in staged_paths:
            full = tmppath / rel
            full.parent.mkdir(parents=True, exist_ok=True)
            full.write_text(content)
            subprocess.run(["git", "add", rel], cwd=tmpdir,
                           capture_output=True, env=env)

        return subprocess.run(
            ["bash", "-c",
             f'source "{GIT_SAFE_PUSH}" && bot_assert_staged_safe /dev/null'],
            cwd=tmpdir, capture_output=True, env=env,
        )


def test_i2_bot_assert_staged_safe_fail_closed_on_source_file():
    """I2 integration: bot_assert_staged_safe() must return non-zero (fail-closed)
    when a source-path file is staged in the working tree.
    """
    result = _run_bot_assert(
        [("src/betting/kelly.py", "# forbidden source file")],
        expect_permitted=False,
    )
    assert result.returncode != 0, (
        "bot_assert_staged_safe() must fail-closed when a source file is staged; "
        f"got returncode={result.returncode}"
    )


def test_i1_bot_assert_staged_safe_passes_runtime_path():
    """I1 integration: bot_assert_staged_safe() must return 0 when only
    permitted runtime-data paths are staged.
    """
    result = _run_bot_assert(
        [("docs/data/signals.json", '{"tennis": []}')],
        expect_permitted=True,
    )
    assert result.returncode == 0, (
        "bot_assert_staged_safe() must pass for permitted runtime paths; "
        f"got returncode={result.returncode}\nstderr={result.stderr.decode()}"
    )


def test_i2_bot_assert_staged_safe_mixed_rejects_on_source():
    """I2 integration: if a mix of permitted and forbidden files is staged,
    bot_assert_staged_safe() must still fail-closed.
    """
    result = _run_bot_assert(
        [
            ("docs/data/signals.json", "{}"),
            ("scripts/dangerous.py", "# source file"),
        ],
        expect_permitted=False,
    )
    assert result.returncode != 0, (
        "bot_assert_staged_safe() must fail-closed even with a mix of permitted "
        f"and forbidden staged files; got returncode={result.returncode}"
    )


# ── I3: Concurrent retry safety ───────────────────────────────────────────────

_BARE_PUSH_RE = re.compile(
    r"git push\b.*?\|\|\s*true"
    r"|git push\s*$"
    r"|git push\s+\|\|\s*true",
    re.MULTILINE,
)
_RETRY_LOOP_RE = re.compile(r"for\s+i\s+in\s+1\s+2|_bot_commit_push\.sh", re.MULTILINE)


def test_i3_no_bare_push_in_active_workflows():
    """I3: No active runtime workflow uses bare `git push || true` without retry.

    All bot pushes must be either:
      (a) inside a for-loop retry block, or
      (b) delegated to _bot_commit_push.sh (which includes its own retry).
    """
    violations: list[str] = []
    for wf in _runtime_workflows():
        text = wf.read_text()
        if "git push" not in text:
            continue
        has_retry = bool(_RETRY_LOOP_RE.search(text))
        has_bare = bool(_BARE_PUSH_RE.search(text))
        if has_bare and not has_retry:
            violations.append(wf.name)

    assert not violations, (
        "Workflows with bare 'git push || true' and no retry loop:\n"
        + "\n".join(f"  {wf}" for wf in violations)
    )


# ── I4: Provenance separation ─────────────────────────────────────────────────

def test_i4_provenance_meta_only_in_ci_gates():
    """I4: provenance_meta.json is written only by ci_gates.yml."""
    for wf in _runtime_workflows():
        text = wf.read_text()
        assert "provenance_meta.json" not in text, (
            f"{wf.name} must not write provenance_meta.json "
            "(provenance separation: source_release_sha != runtime commit SHA)"
        )


def test_i4_source_release_sha_not_overwritten_by_runtime():
    """I4: scripts/record_source_release.py is only invoked by ci_gates.yml."""
    for wf in _runtime_workflows():
        text = wf.read_text()
        assert "record_source_release" not in text, (
            f"{wf.name} must not invoke record_source_release — "
            "source release provenance is a CI-gate responsibility only"
        )


# ── I5: Writer identity observable ────────────────────────────────────────────

def test_i5_bot_commit_push_primitive_enforces_identity():
    """I5 (primitive): _bot_commit_push.sh must itself set git config user.name
    'SportsBrain Bot' — enforcing canonical identity for all callers including
    those that omit the config step.
    """
    text = BOT_COMMIT_PUSH.read_text()
    assert 'user.name "SportsBrain Bot"' in text, (
        "_bot_commit_push.sh must set git config user.name to enforce writer identity"
    )
    assert "GITHUB_RUN_ID" in text, (
        "_bot_commit_push.sh must include GITHUB_RUN_ID provenance in commit message"
    )


def test_i5_all_committing_workflows_have_observable_identity():
    """I5: Every workflow that runs `git commit` directly (not delegated) also
    sets `git config user.name` to establish observable writer identity.

    Workflows that delegate to _bot_commit_push.sh are covered by
    test_i5_bot_commit_push_primitive_enforces_identity — identity is enforced
    inside the shared primitive itself.
    """
    violations: list[str] = []
    for wf in _active_workflows():
        text = wf.read_text()
        uses_primitive = "_bot_commit_push.sh" in text
        has_direct_commit = "git commit" in text
        if not has_direct_commit:
            continue
        if uses_primitive:
            # primitive enforces identity — no violation even if inline git commit absent
            continue
        if 'user.name "SportsBrain Bot"' not in text:
            violations.append(wf.name)

    assert not violations, (
        "Workflows running git commit directly without setting git config user.name:\n"
        + "\n".join(f"  {wf}" for wf in violations)
    )


def test_i5_regression_new_helper_caller_requires_identity():
    """I5 regression: a workflow that calls _bot_commit_push.sh but does NOT set
    user.name is still acceptable — but ONLY because the primitive enforces it.
    This test verifies the primitive contains the identity config so the guarantee
    cannot be silently lost by editing _bot_commit_push.sh.
    """
    text = BOT_COMMIT_PUSH.read_text()
    assert 'git config user.name' in text, (
        "Primitive identity guarantee lost — _bot_commit_push.sh no longer sets user.name. "
        "All callers that omit the config step would lose observable identity."
    )


# ── I6: Classification coverage ──────────────────────────────────────────────
#
# Every active workflow that can commit/push is explicitly classified as one of:
#   A. standard runtime writer  — P0D-001, uses _bot_commit_push.sh
#   B. financial/ledger writer  — defer to P0D-002 (keeps inline retry)
#   C. model promotion/training — excluded from P0D-001 (keeps inline retry)
#   D. AI healer/source-authority — P0D-003 (no git commit in current form)
#   E. source-release provenance  — explicit CI exception (ci_gates.yml only)
#
# If a new workflow commits/pushes without being classified here, this test fails.

_CLASS_A_STANDARD_RUNTIME = frozenset({
    # Converted to _bot_commit_push.sh in P0D-001:
    "tennis_scan.yml",
    "tennis_live_scan.yml",
    "tennis_stats_snapshot.yml",
    "tennis_recalibrate.yml",
    "tennis_odds_snapshot.yml",
    "bundesliga2_closing_odds.yml",
    "bundesliga2_live_push.yml",
    "bundesliga2_scan.yml",
})

_CLASS_B_FINANCIAL_LEDGER = frozenset({
    # Write results/ledger_*.csv or are financial pipeline coordinators.
    # Deferred to P0D-002. Do NOT modify financial writer semantics here.
    "tennis_settle.yml",          # ledger + signals
    "tennis_closing_odds.yml",    # ledger (CLV backfill) — treated as financial-sensitive
    "bundesliga2_settle.yml",     # ledger
    "consume_pending_bets.yml",   # financial pipeline coordinator; health-only git step
})

_CLASS_C_MODEL_PROMOTION = frozenset({
    # Write to models/ — excluded from P0D-001.
    "tennis_lgbm_retrain.yml",
    "tennis_elo_refresh.yml",
    "bundesliga2_retrain.yml",
})

_CLASS_D_AI_HEALER = frozenset({
    # No git commit/push currently; P0D-003 scope.
    "cloud_healer.yml",
})

_CLASS_E_SOURCE_PROVENANCE = frozenset({
    "ci_gates.yml",
})

_ALL_CLASSIFIED = (
    _CLASS_A_STANDARD_RUNTIME
    | _CLASS_B_FINANCIAL_LEDGER
    | _CLASS_C_MODEL_PROMOTION
    | _CLASS_D_AI_HEALER
    | _CLASS_E_SOURCE_PROVENANCE
)


def test_i6_no_unclassified_committing_workflows():
    """I6: Every active workflow that contains `git commit`, `git push`, or
    calls `_bot_commit_push.sh` must appear in the classification table above.

    A new committing workflow that is not classified fails this hard gate.
    """
    unclassified: list[str] = []
    for wf in _active_workflows():
        text = wf.read_text()
        is_writer = (
            "git commit" in text
            or "git push" in text
            or "_bot_commit_push.sh" in text
        )
        if is_writer and wf.name not in _ALL_CLASSIFIED:
            unclassified.append(wf.name)

    assert not unclassified, (
        "Unclassified committing workflows — add them to the classification table in "
        "tests/monitoring/test_writer_governance.py (I6):\n"
        + "\n".join(f"  {wf}" for wf in unclassified)
    )


def test_i6_class_a_writers_use_governed_primitive():
    """I6: All Class A (standard runtime) workflows must use _bot_commit_push.sh,
    not inline commit+push.  A Class A workflow that reverts to bare inline push
    fails this gate immediately.
    """
    violations: list[str] = []
    for wf_name in _CLASS_A_STANDARD_RUNTIME:
        wf = WORKFLOWS_DIR / wf_name
        if not wf.exists():
            violations.append(f"{wf_name}: file not found")
            continue
        text = wf.read_text()
        if "_bot_commit_push.sh" not in text:
            violations.append(f"{wf_name}: does not call _bot_commit_push.sh")

    assert not violations, (
        "Class A (standard runtime) workflows missing governed primitive:\n"
        + "\n".join(f"  {v}" for v in violations)
    )


# ── Allowlist shell-existence sentinel ────────────────────────────────────────

def test_shell_allowlist_file_exists_and_contains_permitted_function():
    """Sentinel: scripts/_git_safe_push.sh exists and defines _bot_permitted().
    Fails if the shell allowlist file is accidentally removed or emptied.
    """
    assert GIT_SAFE_PUSH.exists(), (
        f"Shell allowlist script missing: {GIT_SAFE_PUSH}"
    )
    text = GIT_SAFE_PUSH.read_text()
    assert "_bot_permitted()" in text, (
        "_bot_permitted() function missing from scripts/_git_safe_push.sh"
    )
    assert "bot_assert_staged_safe()" in text, (
        "bot_assert_staged_safe() function missing from scripts/_git_safe_push.sh"
    )
    # Sanity: check key families are represented in the shell allowlist
    for family in ["docs/data/", "data/cache/", "results/health/", "models/"]:
        assert family in text, (
            f"Required allowlist path family missing from shell script: {family}"
        )
