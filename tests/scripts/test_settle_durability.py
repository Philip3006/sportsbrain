"""Temporary-git regression coverage for local settlement durability."""
from __future__ import annotations

import importlib
import subprocess
from pathlib import Path

import pytest

import src.betting.ledger as betting_ledger
import src.notifications.web_dashboard as dashboard
from scripts import settle_bets
from src.betting.private_ledger_durability import (
    PrivateLedgerDurabilityError,
    SettlementLedgerPublisher,
)
from src.notifications import web_push


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _private_repo(tmp_path: Path) -> tuple[Path, Path]:
    remote = tmp_path / "Philip3006" / "sportsbrain-ledger"
    remote.parent.mkdir()
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)

    seed = tmp_path / "seed"
    _git(tmp_path, "clone", str(remote), str(seed))
    _git(seed, "checkout", "-b", "main")
    _git(seed, "config", "user.name", "Test")
    _git(seed, "config", "user.email", "test@example.invalid")
    (seed / "ledger_philip.csv").write_text("match_id,status,pnl\nmatch-1,open,\n")
    _git(seed, "add", "ledger_philip.csv")
    _git(seed, "commit", "-m", "seed")
    _git(seed, "push", "-u", "origin", "main")

    local = tmp_path / "local"
    _git(tmp_path, "clone", str(remote), str(local))
    _git(local, "checkout", "main")
    _git(local, "config", "user.name", "Test")
    _git(local, "config", "user.email", "test@example.invalid")
    return remote, local


def _publisher(local: Path) -> SettlementLedgerPublisher:
    return SettlementLedgerPublisher(local)


def _remote_file(remote: Path, name: str) -> str:
    return subprocess.run(
        ["git", "--git-dir", str(remote), "show", f"main:{name}"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def _fail_command(
    monkeypatch: pytest.MonkeyPatch,
    publisher: SettlementLedgerPublisher,
    command: tuple[str, ...],
) -> None:
    original = publisher._run

    def run(*args: str) -> subprocess.CompletedProcess[str]:
        if args == command:
            return subprocess.CompletedProcess(["git", *args], 1, "", "injected failure")
        return original(*args)

    monkeypatch.setattr(publisher, "_run", run)


def test_successful_publish_requires_remote_containment(tmp_path: Path) -> None:
    remote, local = _private_repo(tmp_path)
    (local / "ledger_philip.csv").write_text("match_id,status,pnl\nmatch-1,won,1.00\n")

    publisher = _publisher(local)
    publisher.publish_settlement_changes(1)

    assert "match-1,won,1.00" in _remote_file(remote, "ledger_philip.csv")
    assert _git(local, "merge-base", "--is-ancestor", "HEAD", "origin/main").returncode == 0


def test_no_settlement_state_is_a_noop(tmp_path: Path) -> None:
    remote, local = _private_repo(tmp_path)
    before = _remote_file(remote, "ledger_philip.csv")

    publisher = _publisher(local)
    commands: list[str] = []
    original = publisher._run

    def run(*args: str) -> subprocess.CompletedProcess[str]:
        commands.append(args[0])
        return original(*args)

    publisher._run = run
    publisher.prepare_for_settlement()
    publisher.publish_settlement_changes(0)

    assert _remote_file(remote, "ledger_philip.csv") == before
    assert _git(local, "status", "--porcelain").stdout == ""
    assert not {"add", "commit", "push"}.intersection(commands)


def test_dry_run_does_not_construct_publisher(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SPORTSBRAIN_LEDGER_DIR", str(tmp_path))
    module = importlib.reload(settle_bets)
    monkeypatch.setattr(module, "SettlementLedgerPublisher", lambda _: pytest.fail("publisher used"))
    monkeypatch.setattr(module, "fetch_scores", dict)
    monkeypatch.setattr(dashboard, "list_known_users", list)

    assert module.settle(dry_run=True) == 0


@pytest.mark.parametrize(
    ("command", "message"),
    [
        (("add", "--", "ledger_*.csv"), "git add failed"),
        (("commit", "-m", "auto: settle 1 bet(s)", "--author=SportsBrain Bot <bot@sportsbrain>"), "git commit failed"),
        (("fetch", "origin", "main", "--quiet"), "git fetch failed"),
    ],
)
def test_git_failures_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    command: tuple[str, ...],
    message: str,
) -> None:
    _, local = _private_repo(tmp_path)
    (local / "ledger_philip.csv").write_text("match_id,status,pnl\nmatch-1,lost,-1.00\n")
    publisher = _publisher(local)
    _fail_command(monkeypatch, publisher, command)

    with pytest.raises(PrivateLedgerDurabilityError, match=message):
        publisher.publish_settlement_changes(1)


def test_rebase_failure_aborts_and_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _, local = _private_repo(tmp_path)
    publisher = _publisher(local)
    _fail_command(monkeypatch, publisher, ("rebase", "origin/main"))

    with pytest.raises(PrivateLedgerDurabilityError, match="rebase"):
        publisher.prepare_for_settlement()


def test_push_failure_is_non_success_and_retry_publishes_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    remote, local = _private_repo(tmp_path)
    (local / "ledger_philip.csv").write_text("match_id,status,pnl\nmatch-1,won,1.00\n")
    publisher = _publisher(local)
    _fail_command(monkeypatch, publisher, ("push", "origin", "HEAD:main"))

    with pytest.raises(PrivateLedgerDurabilityError, match="git push failed"):
        publisher.publish_settlement_changes(1)
    assert "match-1,open" in _remote_file(remote, "ledger_philip.csv")

    _publisher(local).prepare_for_settlement()
    assert _remote_file(remote, "ledger_philip.csv").count("match-1") == 1
    assert "match-1,won,1.00" in _remote_file(remote, "ledger_philip.csv")


def test_remote_containment_failure_is_non_success(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _, local = _private_repo(tmp_path)
    (local / "ledger_philip.csv").write_text("match_id,status,pnl\nmatch-1,won,1.00\n")
    publisher = _publisher(local)
    monkeypatch.setattr(publisher, "_is_contained", lambda: False)

    with pytest.raises(PrivateLedgerDurabilityError, match="containment"):
        publisher.publish_settlement_changes(1)


def test_local_ahead_commit_is_pushed_before_new_settlement(tmp_path: Path) -> None:
    remote, local = _private_repo(tmp_path)
    (local / "ledger_philip.csv").write_text("match_id,status,pnl\nmatch-1,won,1.00\n")
    _git(local, "add", "ledger_philip.csv")
    _git(local, "commit", "-m", "prior settlement")

    _publisher(local).prepare_for_settlement()

    assert "match-1,won,1.00" in _remote_file(remote, "ledger_philip.csv")


def test_unexpected_private_repo_file_fails_closed(tmp_path: Path) -> None:
    _, local = _private_repo(tmp_path)
    (local / "notes.txt").write_text("not financial\n")

    with pytest.raises(PrivateLedgerDurabilityError, match="unexpected local changes"):
        _publisher(local).prepare_for_settlement()


def test_unexpected_staged_file_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _, local = _private_repo(tmp_path)
    (local / "notes.txt").write_text("not financial\n")
    _git(local, "add", "notes.txt")
    publisher = _publisher(local)
    monkeypatch.setattr(publisher, "_worktree_paths", list)

    with pytest.raises(PrivateLedgerDurabilityError, match="unexpected staged files"):
        publisher._stage_commit_rebase_push("auto: settle test")


def test_concurrent_remote_update_is_preserved(tmp_path: Path) -> None:
    remote, local = _private_repo(tmp_path)
    other = tmp_path / "other"
    _git(tmp_path, "clone", str(remote), str(other))
    _git(other, "checkout", "main")
    _git(other, "config", "user.name", "Other")
    _git(other, "config", "user.email", "other@example.invalid")
    (other / "ledger_alice.csv").write_text("match_id,status,pnl\na-1,won,1.00\n")
    _git(other, "add", "ledger_alice.csv")
    _git(other, "commit", "-m", "other settlement")
    _git(other, "push", "origin", "main")

    (local / "ledger_philip.csv").write_text("match_id,status,pnl\nmatch-1,lost,-1.00\n")
    _publisher(local).publish_settlement_changes(1)

    assert "a-1,won,1.00" in _remote_file(remote, "ledger_alice.csv")
    assert "match-1,lost,-1.00" in _remote_file(remote, "ledger_philip.csv")


def test_push_race_rebases_once_and_preserves_remote_update(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    remote, local = _private_repo(tmp_path)
    other = tmp_path / "other"
    _git(tmp_path, "clone", str(remote), str(other))
    _git(other, "checkout", "main")
    _git(other, "config", "user.name", "Other")
    _git(other, "config", "user.email", "other@example.invalid")
    (local / "ledger_philip.csv").write_text("match_id,status,pnl\nmatch-1,won,1.00\n")
    publisher = _publisher(local)
    original = publisher._run
    push_attempted = False

    def run(*args: str) -> subprocess.CompletedProcess[str]:
        nonlocal push_attempted
        if args == ("push", "origin", "HEAD:main") and not push_attempted:
            push_attempted = True
            (other / "ledger_alice.csv").write_text("match_id,status,pnl\na-1,won,1.00\n")
            _git(other, "add", "ledger_alice.csv")
            _git(other, "commit", "-m", "concurrent settlement")
            _git(other, "push", "origin", "main")
            return subprocess.CompletedProcess(["git", *args], 1, "", "remote advanced")
        return original(*args)

    monkeypatch.setattr(publisher, "_run", run)
    publisher.publish_settlement_changes(1)

    assert push_attempted
    assert "match-1,won,1.00" in _remote_file(remote, "ledger_philip.csv")
    assert "a-1,won,1.00" in _remote_file(remote, "ledger_alice.csv")


def test_conflict_fails_closed_without_force_resolution(tmp_path: Path) -> None:
    remote, local = _private_repo(tmp_path)
    other = tmp_path / "other"
    _git(tmp_path, "clone", str(remote), str(other))
    _git(other, "checkout", "main")
    _git(other, "config", "user.name", "Other")
    _git(other, "config", "user.email", "other@example.invalid")
    (other / "ledger_philip.csv").write_text("match_id,status,pnl\nmatch-1,lost,-1.00\n")
    _git(other, "add", "ledger_philip.csv")
    _git(other, "commit", "-m", "remote update")
    _git(other, "push", "origin", "main")

    (local / "ledger_philip.csv").write_text("match_id,status,pnl\nmatch-1,won,1.00\n")
    with pytest.raises(PrivateLedgerDurabilityError, match="rebase"):
        _publisher(local).publish_settlement_changes(1)

    assert "match-1,lost,-1.00" in _remote_file(remote, "ledger_philip.csv")


def test_missing_private_ledger_directory_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(PrivateLedgerDurabilityError, match="unavailable"):
        _publisher(tmp_path / "missing").prepare_for_settlement()


def _settlement_module(monkeypatch: pytest.MonkeyPatch, ledger_dir: Path):
    monkeypatch.setenv("SPORTSBRAIN_LEDGER_DIR", str(ledger_dir))
    module = importlib.reload(settle_bets)
    monkeypatch.setattr(
        module,
        "fetch_scores",
        lambda: {"match-1": {"home_score": 1, "away_score": 0}},
    )
    monkeypatch.setattr(dashboard, "list_known_users", lambda: ["philip"])
    monkeypatch.setattr(module, "write_market_performance", lambda users: None)
    monkeypatch.setattr(web_push, "send_open_bet_reminder", lambda rows: None)
    monkeypatch.setattr(web_push, "send_bankroll_milestone_alert", lambda equity: None)
    monkeypatch.setattr(betting_ledger, "ledger_summary", lambda: {"total_pnl": 0})
    return module


def _write_open_bet(ledger_dir: Path) -> None:
    (ledger_dir / "ledger_philip.csv").write_text(
        "match_id,home,away,market,decimal_odds,stake_amount,status,pnl\n"
        "match-1,Home,Away,home,2.0,10,open,\n"
    )


def _seed_open_bet(local: Path) -> None:
    _write_open_bet(local)
    _git(local, "add", "ledger_philip.csv")
    _git(local, "commit", "-m", "open bet")
    _git(local, "push", "origin", "main")


def test_settlement_entrypoint_persists_only_to_private_remote(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    remote, local = _private_repo(tmp_path)
    _seed_open_bet(local)
    public_ledger = tmp_path / "public-results" / "ledger.csv"
    public_ledger.parent.mkdir()
    public_ledger.write_text("public-ledger-must-not-change\n")

    module = _settlement_module(monkeypatch, local)

    assert module.main([]) == 0
    assert "match-1,Home,Away,home,2.0,10,won,10.0" in _remote_file(remote, "ledger_philip.csv")
    assert public_ledger.read_text() == "public-ledger-must-not-change\n"


def test_settlement_retry_publishes_prior_local_write_without_double_settlement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    remote, local = _private_repo(tmp_path)
    _seed_open_bet(local)
    module = _settlement_module(monkeypatch, local)
    publisher = _publisher(local)
    _fail_command(monkeypatch, publisher, ("push", "origin", "HEAD:main"))
    monkeypatch.setattr(module, "SettlementLedgerPublisher", lambda _: publisher)

    assert module.main([]) == 1
    assert "Total 1 bet(s) settled" not in capsys.readouterr().out

    monkeypatch.setattr(module, "SettlementLedgerPublisher", SettlementLedgerPublisher)
    assert module.main([]) == 0
    remote_ledger = _remote_file(remote, "ledger_philip.csv")
    assert remote_ledger.count("match-1") == 1
    assert "won,10.0" in remote_ledger


def test_settlement_missing_ledger_environment_fails_before_score_fetch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SPORTSBRAIN_LEDGER_DIR", raising=False)
    module = importlib.reload(settle_bets)
    monkeypatch.setattr(module, "fetch_scores", lambda: pytest.fail("score fetch used"))

    assert module.main([]) == 1
