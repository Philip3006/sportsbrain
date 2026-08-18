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
      also sets `git config user.name` to a fixed, recognisable value.

Finding addressed: P0D-001 (Runtime Writer Governance)
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS_DIR = ROOT / ".github" / "workflows"

# ── Python mirror of _bot_permitted() in scripts/_git_safe_push.sh ───────────
# This set of patterns MUST stay in sync with the shell function.
_PERMITTED_PATTERNS: list[str] = [
    r"^docs/data/",
    r"^data/cache/",
    r"^data/live_scores\.json$",
    r"^data/odds_history/",
    r"^results/health/",
    r"^results/ledger",
    r"^results/scans/",
    r"^results/tennis_live_signals\.json$",
    r"^results/tennis_scan_",
    r"^results/betting_journal\.md$",
    r"^results/tennis_cal_stats\.json$",
    r"^models/dc_bundesliga2/",
    r"^models/tennis_lgbm",
    r"^models/tennis/",
    r"^models/tennis_calibrators/",
]

_SOURCE_DIRS: list[str] = [
    "src/",
    "scripts/",
    "tests/",
    ".github/workflows/",
    "cloudflare/",
    "docs/js/",
]


def is_permitted(path: str) -> bool:
    return any(re.match(p, path) for p in _PERMITTED_PATTERNS)


def _active_workflows() -> list[Path]:
    """All non-disabled workflow files."""
    return [f for f in WORKFLOWS_DIR.glob("*.yml") if f.suffix == ".yml"]


def _runtime_workflows() -> list[Path]:
    """Active workflows that are not ci_gates.yml (runtime writers only)."""
    return [f for f in _active_workflows() if f.name != "ci_gates.yml"]


def _extract_git_add_paths(workflow_text: str) -> list[str]:
    """Extract path prefixes from all `git add` commands in a workflow.

    Multi-line continuations (backslash at end of line) are joined before
    tokenising. Glob wildcards are resolved to the prefix before the first
    wildcard character so they can be checked against the allowlist.
    """
    # Join backslash-continued lines so multi-line git add is one logical line.
    joined = re.sub(r"\\\n\s*", " ", workflow_text)

    paths: list[str] = []
    for line in joined.splitlines():
        stripped = line.strip()
        if not stripped.startswith("git add"):
            continue
        # Strip everything after shell redirects / conditionals.
        clean = re.sub(r"\s*[|&>].*", "", stripped)
        tokens = clean.split()
        for tok in tokens:
            # Skip command words, flags, and bare backslashes.
            if tok in {"git", "add", "-f", "--", "||", "true", "\\"} or tok.startswith("-"):
                continue
            # Resolve glob: use prefix up to first wildcard character.
            base = re.split(r"[*?\[]", tok)[0]
            if base and "/" in base:  # only classify path-like tokens
                paths.append(base)
    return paths


# ── I1/I2: Path allowlist ─────────────────────────────────────────────────────

def test_i1_permitted_runtime_paths_pass():
    """I1: Known permitted runtime output paths all pass the allowlist check."""
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
        "data/odds_history/tennis/some.json",
        "results/health/tennis_scan.json",
        "results/health/bundesliga2_scan.json",
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
        assert is_permitted(p), f"permitted path failed allowlist: {p}"


def test_i2_source_paths_rejected():
    """I2: Source code paths are rejected by the bot allowlist (fail-closed)."""
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
        assert not is_permitted(p), f"source path should be rejected but passed: {p}"


def test_i1_all_workflow_git_add_paths_permitted():
    """I1: Every path prefix in 'git add' commands across all active workflows
    is within the bot path allowlist.

    Paths extracted from git add lines; glob wildcards resolved to their prefix
    (e.g. `results/tennis_scan_*.md` → prefix `results/tennis_scan_`).
    """
    violations: list[tuple[str, str]] = []
    for wf in _active_workflows():
        text = wf.read_text()
        for path_prefix in _extract_git_add_paths(text):
            if not is_permitted(path_prefix) and not any(
                path_prefix.startswith(sd) for sd in ("docs/data/provenance_meta",)
            ):
                # provenance_meta.json is written by ci_gates.yml only — covered
                # by I4. It is under docs/data/ which IS permitted; the path_prefix
                # "docs/data/provenance_meta.json" still passes is_permitted().
                violations.append((wf.name, path_prefix))

    assert not violations, (
        "Workflow git add paths outside bot allowlist:\n"
        + "\n".join(f"  {wf}: {p}" for wf, p in violations)
    )


# ── I3: Concurrent retry safety ───────────────────────────────────────────────

# Patterns that indicate a bare (no-retry) push in a workflow.
_BARE_PUSH_RE = re.compile(
    r"git push\b.*?\|\|\s*true"  # git push ... || true
    r"|git push\s*$"             # bare git push at end of line (no retry)
    r"|git push\s+\|\|\s*true",  # git push || true
    re.MULTILINE,
)

# Patterns that indicate a proper retry loop is present.
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
        # Only check the commit/push sections (steps that run git push).
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
    """I4: provenance_meta.json is written only by ci_gates.yml.

    No runtime workflow may commit to docs/data/provenance_meta.json, which
    records the SOURCE release SHA.  Runtime commit SHAs must remain separate
    from the source_release_sha invariant.
    """
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

def test_i5_all_committing_workflows_set_bot_identity():
    """I5: Every workflow that runs `git commit` also sets `git config user.name`
    to establish observable writer identity.
    """
    violations: list[str] = []
    for wf in _active_workflows():
        text = wf.read_text()
        if "git commit" not in text:
            continue
        if 'user.name "SportsBrain Bot"' not in text:
            violations.append(wf.name)

    assert not violations, (
        "Workflows running git commit without setting git config user.name:\n"
        + "\n".join(f"  {wf}" for wf in violations)
    )


# ── Allowlist sync sentinel ───────────────────────────────────────────────────

def test_allowlist_sync_sentinel():
    """The Python _PERMITTED_PATTERNS list is non-empty and covers the minimal
    required path families (docs/data, data/cache, results/health, models).
    Fails if someone accidentally empties the list.
    """
    required_prefixes = [
        r"^docs/data/",
        r"^data/cache/",
        r"^results/health/",
        r"^models/",
    ]
    for prefix in required_prefixes:
        assert any(p.startswith(prefix) for p in _PERMITTED_PATTERNS), (
            f"Required allowlist prefix missing: {prefix}"
        )
