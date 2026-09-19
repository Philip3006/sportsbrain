"""Unit regressions for the read-only post-merge verifier."""

from __future__ import annotations

from pathlib import Path

import scripts.post_merge_verifier as verifier


def test_train_order_is_fixed_and_runtime_paths_are_ignored() -> None:
    assert verifier._step_prefix("95") == ("88", "91", "92", "94", "95")
    result = verifier._check_runtime_drift(
        (
            "docs/data/health.json",
            "results/season_report_2026.json",
            "src/football/top5_polling_planner.py",
        )
    )
    assert result.passed is True
    assert "fixed train order" in result.detail


def test_future_follow_up_accepts_explicit_paths_and_tests() -> None:
    assert verifier._required_paths("follow-up", ("src/follow_up.py",)) == (
        "src/follow_up.py",
    )


def test_authority_check_allows_explicit_candidate_repertoire(tmp_path: Path) -> None:
    contracts = tmp_path / "src" / "football" / "provider_cascade" / "contracts.py"
    router = tmp_path / "src" / "football" / "provider_cascade" / "router.py"
    rundown = tmp_path / "src" / "football" / "odds" / "therundown.py"
    contracts.parent.mkdir(parents=True)
    rundown.parent.mkdir(parents=True)
    contracts.write_text(
        'FOOTBALL_PROVIDER_REPERTOIRE = ("the_odds_api",)\n'
        "DEFAULT_PROVIDER_ORDER = FOOTBALL_PROVIDER_REPERTOIRE\n"
        'CANDIDATE_PROVIDER_REPERTOIRE = ("therundown_experimental",)\n'
        "active_authority = False\n",
        encoding="utf-8",
    )
    router.write_text('"the_odds_api": TheOddsAPIAdapter()\n', encoding="utf-8")
    rundown.write_text(
        'THERUNDOWN_PROVIDER_NAME = "therundown_experimental"\n'
        "# candidate-only; not registered\n",
        encoding="utf-8",
    )
    result = verifier._check_authority_and_candidate_boundary(tmp_path)
    assert result.passed is True
    assert verifier._test_patterns("follow-up", ("tests/test_follow_up.py",)) == (
        "tests/test_follow_up.py",
    )


def test_definition_counts_detect_duplicate_contract_symbols(tmp_path: Path) -> None:
    source = tmp_path / "src" / "football" / "duplicate.py"
    source.parent.mkdir(parents=True)
    source.write_text(
        "class Contract: pass\n\ndef Contract(): pass\n", encoding="utf-8"
    )
    assert verifier._definition_counts(tmp_path)["Contract"] == 2


def test_duplicate_pr93_source_path_fails_closed(tmp_path: Path) -> None:
    source = tmp_path / "src" / "football" / "pr93_qualification.py"
    source.parent.mkdir(parents=True)
    source.write_text("# duplicate\n", encoding="utf-8")
    result = verifier._check_no_duplicate_pr93(tmp_path)
    assert result.passed is False


def test_scheduler_word_in_docstring_is_not_an_import_violation(tmp_path: Path) -> None:
    source = tmp_path / "src" / "football" / "safe.py"
    source.parent.mkdir(parents=True)
    source.write_text(
        '"""Does not import a scheduler or mutate a ledger."""\n\n'
        "scheduler_registered = False\n",
        encoding="utf-8",
    )
    result = verifier._check_changed_side_effects(tmp_path, ("src/football/safe.py",))
    assert result.passed is True


def test_ledger_import_is_rejected(tmp_path: Path) -> None:
    source = tmp_path / "src" / "football" / "unsafe.py"
    source.parent.mkdir(parents=True)
    source.write_text("from src.betting import ledger\n", encoding="utf-8")
    result = verifier._check_changed_side_effects(tmp_path, ("src/football/unsafe.py",))
    assert result.passed is False


def test_mutating_git_commands_are_not_part_of_the_verifier_contract() -> None:
    assert verifier.MUTATING_GIT_COMMANDS == {
        "merge",
        "commit",
        "push",
        "reset",
        "checkout",
        "rebase",
        "cherry-pick",
    }


def test_offline_socket_guard_is_explicit(tmp_path: Path) -> None:
    verifier._offline_sitecustomize(tmp_path)
    content = (tmp_path / "sitecustomize.py").read_text(encoding="utf-8")
    assert "POST_MERGE_VERIFIER_NETWORK_BLOCKED" in content
    assert "socket.socket.connect" in content
