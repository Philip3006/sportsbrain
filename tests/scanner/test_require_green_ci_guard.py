"""
CEO-Roadmap O0-b — Regressionstests für Scanner Pre-Check Guard.

Deckt die 4 vom CEO explizit geforderten Szenarien ab (Race-Condition-Test):

  Commit A grün                       → success
  Commit B gepusht, CI läuft noch     → fail-closed (in_progress)
  Commit B CI failure                 → fail-closed (failed)
  Commit B CI success                 → success

Zusätzlich:
  Kein Run für SHA (missing)          → fail-closed
  gh api liefert Fehler               → fail-closed via subprocess-Mock
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "require_green_ci.py"


@pytest.fixture(scope="module")
def guard():
    """Lädt require_green_ci.py als Modul (kein sys.exit beim Import)."""
    spec = importlib.util.spec_from_file_location("require_green_ci", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run(status: str, conclusion: str | None, rid: int = 1) -> dict:
    return {"id": rid, "status": status, "conclusion": conclusion}


class TestDecideLogic:
    """decide() ist die reine Entscheidungslogik — testbar ohne Netz."""

    # ── vom CEO explizit geforderte 4 Race-Szenarien ─────────────────

    def test_commit_a_green_allows_production(self, guard):
        runs = [_run("completed", "success", 100)]
        verdict, reason = guard.decide(runs)
        assert verdict == "success", reason

    def test_commit_b_ci_still_running_aborts(self, guard):
        runs = [_run("in_progress", None, 200)]
        verdict, reason = guard.decide(runs)
        assert verdict == "in_progress", reason

    def test_commit_b_ci_failure_aborts(self, guard):
        runs = [_run("completed", "failure", 300)]
        verdict, reason = guard.decide(runs)
        assert verdict == "failed", reason

    def test_commit_b_ci_success_allows_production(self, guard):
        runs = [_run("completed", "success", 400)]
        verdict, reason = guard.decide(runs)
        assert verdict == "success", reason

    # ── weitere fail-closed Zustände ─────────────────────────────────

    def test_missing_run_aborts(self, guard):
        verdict, _ = guard.decide([])
        assert verdict == "missing"

    @pytest.mark.parametrize("s", ["queued", "waiting", "requested", "pending"])
    def test_active_status_aborts(self, guard, s):
        verdict, _ = guard.decide([_run(s, None)])
        assert verdict == "in_progress"

    @pytest.mark.parametrize("c", ["cancelled", "timed_out",
                                    "action_required", "neutral", "skipped"])
    def test_non_success_conclusion_aborts(self, guard, c):
        verdict, _ = guard.decide([_run("completed", c)])
        assert verdict == "failed"

    # ── Kombinationen: mindestens ein Success gewinnt ────────────────

    def test_any_success_run_wins(self, guard):
        """Bei mehreren Runs für dieselbe SHA reicht ein Success."""
        runs = [
            _run("completed", "failure", 1),
            _run("completed", "success", 2),  # re-run grün
        ]
        verdict, _ = guard.decide(runs)
        assert verdict == "success"

    def test_active_plus_failure_still_aborts(self, guard):
        """Ein laufender Re-Run neben Failure zählt als in_progress (fail-closed)."""
        runs = [
            _run("completed", "failure", 1),
            _run("in_progress", None, 2),
        ]
        verdict, _ = guard.decide(runs)
        assert verdict == "in_progress"


class TestFailClosedOnApiError:
    """gh-API-Fehler → main() exit 1 (fail-closed)."""

    def test_main_exits_1_on_api_failure(self, guard, monkeypatch):
        def _boom(repo, sha):
            raise RuntimeError("gh api fehlgeschlagen: 500")
        monkeypatch.setattr(guard, "_fetch_runs", _boom)
        monkeypatch.setattr(guard, "_resolve_repo", lambda: "test/repo")
        monkeypatch.setattr(guard, "_resolve_sha", lambda: "deadbeef")
        assert guard.main() == 1

    def test_main_exits_1_on_missing_run(self, guard, monkeypatch):
        monkeypatch.setattr(guard, "_fetch_runs", lambda r, s: [])
        monkeypatch.setattr(guard, "_resolve_repo", lambda: "test/repo")
        monkeypatch.setattr(guard, "_resolve_sha", lambda: "deadbeef")
        assert guard.main() == 1

    def test_main_exits_0_on_green(self, guard, monkeypatch):
        monkeypatch.setattr(guard, "_fetch_runs",
                            lambda r, s: [_run("completed", "success")])
        monkeypatch.setattr(guard, "_resolve_repo", lambda: "test/repo")
        monkeypatch.setattr(guard, "_resolve_sha", lambda: "deadbeef")
        assert guard.main() == 0
