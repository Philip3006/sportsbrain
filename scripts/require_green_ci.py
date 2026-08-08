#!/usr/bin/env python3
"""
CEO-Roadmap O0-b — Scanner Pre-Check Guard.

Verlangt, dass für die aktuelle Commit-SHA ein abgeschlossener
ci_gates-Run mit conclusion=success existiert. Sonst FAIL CLOSED.

Zielzustand:
  production HEAD SHA == successfully validated CI SHA

Fail-closed für alle nicht-eindeutig-grünen Zustände:
  - missing (kein Run für diese SHA)
  - queued / in_progress / waiting
  - failure / cancelled / timed_out / action_required / neutral / skipped
  - stale / uneindeutig

Aufruf (in Workflow-Step):
    - name: Require green ci_gates for HEAD SHA (Phase-0 Gate)
      env:
        GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}
      run: python3 scripts/require_green_ci.py

Lokal:
    python3 scripts/require_green_ci.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Iterable

WORKFLOW = os.environ.get("CI_GATES_WORKFLOW", "ci_gates.yml")

# Statuses / conclusions, die als "grün" gelten. Bewusst eng gehalten.
SUCCESS_CONCLUSION = "success"
COMPLETED_STATUS = "completed"

# Alles hier drin = "läuft noch", explizit fail-closed
ACTIVE_STATUSES = frozenset({
    "queued", "in_progress", "waiting", "requested", "pending",
})


def decide(runs: Iterable[dict]) -> tuple[str, str]:
    """Entscheidet über die Guard-Logik für eine Liste von ci_gates-Runs.

    Args:
        runs: Liste der `workflow_runs` von der GitHub-API für head_sha=SHA.

    Returns:
        (verdict, reason). verdict ∈ {"success", "missing", "in_progress", "failed"}.
    """
    runs = list(runs)
    if not runs:
        return ("missing", "kein ci_gates-Run für diese SHA gefunden")

    # Grün, sobald mindestens ein completed+success existiert
    for r in runs:
        if (r.get("status") == COMPLETED_STATUS
                and r.get("conclusion") == SUCCESS_CONCLUSION):
            return ("success", f"run {r.get('id')} conclusion=success")

    # Kein Success. Läuft noch etwas?
    for r in runs:
        if r.get("status") in ACTIVE_STATUSES:
            return ("in_progress", f"run {r.get('id')} status={r.get('status')}")

    # Alles completed, aber nichts erfolgreich
    concl = next(
        (r.get("conclusion") for r in runs if r.get("status") == COMPLETED_STATUS),
        "unknown",
    )
    return ("failed", f"ci_gates completed with conclusion={concl}")


def _resolve_repo() -> str:
    repo = os.environ.get("GITHUB_REPOSITORY")
    if repo:
        return repo
    remote = subprocess.check_output(
        ["git", "config", "--get", "remote.origin.url"], text=True
    ).strip()
    # git@github.com:Owner/Repo.git   or   https://github.com/Owner/Repo(.git)
    for pfx in ("git@github.com:", "https://github.com/"):
        if remote.startswith(pfx):
            remote = remote[len(pfx):]
            break
    remote = remote.removesuffix(".git")
    return remote


def _resolve_sha() -> str:
    sha = os.environ.get("GITHUB_SHA")
    if sha:
        return sha
    return subprocess.check_output(
        ["git", "rev-parse", "HEAD"], text=True
    ).strip()


def _fetch_runs(repo: str, sha: str) -> list[dict]:
    """Liest ci_gates-Runs für head_sha via gh CLI. Fail-closed bei Fehler."""
    url = (
        f"/repos/{repo}/actions/workflows/{WORKFLOW}/runs"
        f"?head_sha={sha}&per_page=20"
    )
    result = subprocess.run(
        ["gh", "api", url],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"gh api fehlgeschlagen: {result.stderr.strip()}")
    payload = json.loads(result.stdout or "{}")
    return list(payload.get("workflow_runs", []))


def _last_green_run_sha(repo: str) -> str | None:
    """Gibt die head_sha des letzten erfolgreichen ci_gates-Runs auf main zurück."""
    url = (
        f"/repos/{repo}/actions/workflows/{WORKFLOW}/runs"
        f"?branch=main&status=success&per_page=5"
    )
    result = subprocess.run(
        ["gh", "api", url],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        return None
    payload = json.loads(result.stdout or "{}")
    runs = payload.get("workflow_runs", [])
    if runs:
        return runs[0].get("head_sha")
    return None


def _is_ancestor(repo: str, ancestor_sha: str, descendant_sha: str) -> bool:
    """True wenn ancestor_sha ein Ancestor von descendant_sha ist (via GitHub API).

    Nutzt die Compare-API statt lokalem git, weil GH-Actions-Checkouts
    per Default shallow sind und git merge-base dann falsch-negative liefert.
    """
    if ancestor_sha == descendant_sha:
        return True
    result = subprocess.run(
        ["gh", "api", f"/repos/{repo}/compare/{ancestor_sha}...{descendant_sha}"],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        return False
    payload = json.loads(result.stdout or "{}")
    # "ahead" = descendant ist vor ancestor (ancestor ist Ancestor von descendant)
    # "identical" = selbe SHA
    return payload.get("status") in ("ahead", "identical")


def main() -> int:
    try:
        repo = _resolve_repo()
        sha = _resolve_sha()
    except Exception as e:  # noqa: BLE001 — fail-closed guard: any error must abort
        print(f"[require-green-ci] FAIL-CLOSED: Kontext nicht bestimmbar: {e}",
              file=sys.stderr)
        return 1

    print(f"[require-green-ci] repo={repo} sha={sha[:8]} workflow={WORKFLOW}")

    try:
        runs = _fetch_runs(repo, sha)
    except Exception as e:  # noqa: BLE001 — fail-closed guard: any error must abort
        print(f"[require-green-ci] FAIL-CLOSED: API-Fehler: {e}", file=sys.stderr)
        return 1

    verdict, reason = decide(runs)
    if verdict == "success":
        print(f"[require-green-ci] ✓ ci_gates GREEN for {sha[:8]} — {reason}")
        return 0

    # Kein ci_gates-Run für HEAD SHA (z.B. data-only auto-commit, paths-excluded).
    # Prüfe ob HEAD ein Descendant des letzten grünen ci_gates-Runs ist.
    if verdict == "missing":
        print(f"[require-green-ci] sha={sha[:8]} hat keinen ci_gates-Run "
              f"(paths-excluded?) — suche letzten grünen Run auf main …")
        try:
            green_sha = _last_green_run_sha(repo)
        except Exception as e:  # noqa: BLE001
            print(f"[require-green-ci] FAIL-CLOSED: API-Fehler: {e}", file=sys.stderr)
            return 1

        if not green_sha:
            print(
                "[require-green-ci] FAIL-CLOSED [no-green-run]: "
                "kein erfolgreicher ci_gates-Run auf main gefunden",
                file=sys.stderr,
            )
            return 1

        if _is_ancestor(repo, green_sha, sha):
            print(
                "[require-green-ci] ✓ ci_gates GREEN (inherited) — "
                f"HEAD {sha[:8]} ist Descendant von grünem Run {green_sha[:8]}"
            )
            return 0

        print(f"[require-green-ci] FAIL-CLOSED [not-descendant]: "
              f"HEAD {sha[:8]} ist KEIN Descendant von grünem Run {green_sha[:8]}",
              file=sys.stderr)
        return 1

    print(f"[require-green-ci] FAIL-CLOSED [{verdict}] for {sha[:8]}: {reason}",
          file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
