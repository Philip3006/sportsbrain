"""HARD GATE 9 — Financial Writer Governance (TASK-P0D-002-v2).

Verifies that the financial ledger mutation path preserves all P0-A ACK semantics,
remains independent of the generic bot persistence primitive (_bot_commit_push.sh),
and that the ledger now resides exclusively in the PRIVATE repo (Philip3006/sportsbrain-ledger)
via SPORTSBRAIN_LEDGER_DIR — NEVER in the public `results/` directory.

Invariants enforced:
  - Ledger paths resolve into SPORTSBRAIN_LEDGER_DIR, not public results/ (SEC-001).
  - Missing SPORTSBRAIN_LEDGER_DIR raises EnvironmentError immediately (fail-closed).
  - _durable_push() git-add is scoped to `ledger_*.csv` (no results/ prefix in private repo).
  - Settle workflows do not use `|| true` on ledger git-add (ledger staging failure is fatal).
  - Durable authoritative mutation ALWAYS precedes ACK (A).
  - Durable mutation failure → no ACK emitted (B).
  - Retryable/transient failure is NEVER permanently rejected (C).
  - Failure between mutation and ACK is safely replayable without duplicate mutation (D).
  - Duplicate request yields idempotent outcome; no double ledger write (E).
  - Two users share zero ledger state; routing is strictly per-user (F).
  - _bot_commit_push.sh failure cannot alter financial transaction semantics (G).
  - Malformed/forbidden request is rejected with zero ledger mutation (H).
  - Financial git publication stages ONLY ledger paths; no source file can be committed (I).
  - P0-A ACK regression suite (test_lifecycle_* / test_fnd00*) remains green (J).
  - All four financial workflows are classified; none use _bot_commit_push.sh (K).
"""
from __future__ import annotations

import ast
import re
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS_DIR = ROOT / ".github" / "workflows"
CONSUME_SCRIPT = ROOT / "scripts" / "consume_pending_bets.py"

# ── Workflow classification ───────────────────────────────────────────────────
_AUTHORITATIVE_FINANCIAL = frozenset({
    "tennis_settle.yml",
    "tennis_closing_odds.yml",
    "bundesliga2_settle.yml",
})
_CONSUME_WORKFLOW = "consume_pending_bets.yml"
_ALL_FINANCIAL = _AUTHORITATIVE_FINANCIAL | {_CONSUME_WORKFLOW}


# ─────────────────────────────────────────────────────────────────────────────
# P0D-002 NEW TESTS (12 required)
# ─────────────────────────────────────────────────────────────────────────────

def test_1_no_financial_path_resolves_into_public_results(tmp_path, monkeypatch):
    """ledger_path_for('philip') must NOT contain '/results/' from the public repo.

    SEC-001: all ledger I/O must go through SPORTSBRAIN_LEDGER_DIR, never the
    public results/ folder embedded in Philip3006/sportsbrain.
    """
    monkeypatch.setenv("SPORTSBRAIN_LEDGER_DIR", str(tmp_path))
    # Force re-evaluation of config module
    import importlib
    import src.config as cfg
    importlib.reload(cfg)
    path = cfg.ledger_path_for("philip")
    assert "/results/" not in str(path), (
        f"ledger_path_for('philip') must not resolve into the public results/ folder; got {path}"
    )
    assert str(tmp_path) in str(path), (
        f"ledger_path_for('philip') must resolve inside SPORTSBRAIN_LEDGER_DIR ({tmp_path}); got {path}"
    )


def test_2_missing_ledger_dir_fails_closed(monkeypatch):
    """Calling ledger_path_for() without SPORTSBRAIN_LEDGER_DIR raises EnvironmentError.

    Fail-closed invariant: if the private repo is not configured, financial ops
    must fail immediately rather than silently falling back to the public repo.
    """
    monkeypatch.delenv("SPORTSBRAIN_LEDGER_DIR", raising=False)
    import importlib
    import src.config as cfg
    importlib.reload(cfg)
    with pytest.raises(EnvironmentError, match="SPORTSBRAIN_LEDGER_DIR"):
        cfg.ledger_path_for("philip")


def test_3_durable_push_git_add_is_ledger_scoped():
    """_durable_push() git add argument is 'ledger_*.csv' (no 'results/' prefix).

    In the private repo (sportsbrain-ledger), ledger files live at the root level —
    there is no results/ subdirectory. The git add glob must reflect this.
    """
    source = CONSUME_SCRIPT.read_text()
    tree = ast.parse(source)

    git_add_args: list[list[str]] = []

    class _Visitor(ast.NodeVisitor):
        def visit_FunctionDef(self, node):
            if node.name == "_durable_push":
                self._scan_calls(node)
            self.generic_visit(node)

        def _scan_calls(self, node):
            for child in ast.walk(node):
                if not isinstance(child, ast.Call):
                    continue
                func = child.func
                if (
                    isinstance(func, ast.Name) and func.id == "_g"
                    and child.args
                    and isinstance(child.args[0], ast.Constant)
                    and child.args[0].value == "add"
                ):
                    args = [
                        a.value for a in child.args
                        if isinstance(a, ast.Constant) and isinstance(a.value, str)
                    ]
                    git_add_args.append(args)

    _Visitor().visit(tree)

    assert git_add_args, "_durable_push() must contain at least one `git add` call"
    # Must have the authoritative ledger CSV add (no results/ prefix)
    add_targets = [args[-1] for args in git_add_args if len(args) >= 2]
    assert "ledger_*.csv" in add_targets, (
        f"_durable_push() must git-add 'ledger_*.csv' (no results/ prefix); found: {add_targets!r}"
    )
    for args in git_add_args:
        # No git add must use the old results/ prefix
        for arg in args:
            assert "results/" not in arg, (
                f"_durable_push() git add must not use results/ prefix (private repo is flat); got {args!r}"
            )


def test_4_settle_workflows_no_true_on_ledger_add():
    """tennis_settle.yml, tennis_closing_odds.yml, bundesliga2_settle.yml must NOT
    combine `git add.*ledger` with `|| true`.

    `|| true` on ledger staging silently swallows staging failures and allows
    commits to proceed with no ledger data. Ledger staging failure must be fatal.
    """
    violations: list[str] = []
    for wf_name in _AUTHORITATIVE_FINANCIAL:
        wf = WORKFLOWS_DIR / wf_name
        if not wf.exists():
            violations.append(f"{wf_name}: file not found")
            continue
        text = wf.read_text()
        for lineno, line in enumerate(text.splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            # Detect lines that git-add ledger files AND use || true
            if re.search(r"git\s+add.*ledger", stripped) and "|| true" in stripped:
                violations.append(f"{wf_name}:{lineno}: {stripped!r}")
    assert not violations, (
        "Settle workflows must NOT use `|| true` on ledger git-add lines "
        "(ledger staging failure must be fatal):\n"
        + "\n".join(f"  {v}" for v in violations)
    )


def test_5_private_push_before_kv_ack(tmp_path, monkeypatch):
    """When _durable_push succeeds, ACK (_kv_delete_one) is called for each accepted bet."""
    import scripts.consume_pending_bets as cpb
    import src.notifications.web_dashboard as wdb

    push_called = []
    ack_called = []

    monkeypatch.setattr(cpb, "_worker_base", lambda: "https://mock.worker.dev")
    monkeypatch.setattr(cpb, "_token", lambda: "mock-token")
    monkeypatch.setattr(cpb, "_durable_push", lambda added: push_called.append(added) or True)
    monkeypatch.setattr(cpb, "_kv_delete_one", lambda base, headers, bid, user: ack_called.append(bid))
    monkeypatch.setattr(cpb, "_append_rows", lambda rows, user: len(rows))
    monkeypatch.setattr(cpb, "_fetch_validate_user", lambda base, headers, user: (
        [{"match_id": "x", "market": "h2h"}], ["bet-1"], [], []
    ))
    monkeypatch.setattr(cpb, "_process_cancel_requests", lambda *a, **kw: 0)
    monkeypatch.setattr(wdb, "list_known_users", lambda: ["philip"])
    monkeypatch.setattr(wdb, "write_signals_json_all_users", lambda **kw: None)

    # Patch LEDGER_ROOT so _durable_push guard passes
    monkeypatch.setattr(cpb, "_LEDGER_ROOT", tmp_path)

    result = cpb.main()
    assert result == 0, f"main() returned {result} — expected 0 on success"
    assert push_called, "_durable_push was not called"
    assert "bet-1" in ack_called, f"ACK not called for bet-1; ack_called={ack_called}"


def test_6_push_failure_prevents_ack(tmp_path, monkeypatch):
    """When _durable_push fails, ACK (_kv_delete_one) must NOT be called for accepted bets."""
    import scripts.consume_pending_bets as cpb
    import src.notifications.web_dashboard as wdb

    ack_called = []

    monkeypatch.setattr(cpb, "_worker_base", lambda: "https://mock.worker.dev")
    monkeypatch.setattr(cpb, "_token", lambda: "mock-token")
    monkeypatch.setattr(cpb, "_durable_push", lambda added: False)
    monkeypatch.setattr(cpb, "_kv_delete_one", lambda base, headers, bid, user: ack_called.append(bid))
    monkeypatch.setattr(cpb, "_append_rows", lambda rows, user: len(rows))
    monkeypatch.setattr(cpb, "_fetch_validate_user", lambda base, headers, user: (
        [{"match_id": "x", "market": "h2h"}], ["bet-1"], [], []
    ))
    monkeypatch.setattr(cpb, "_process_cancel_requests", lambda *a, **kw: 0)
    monkeypatch.setattr(cpb, "_LEDGER_ROOT", tmp_path)
    monkeypatch.setattr(wdb, "list_known_users", lambda: ["philip"])
    monkeypatch.setattr(wdb, "write_signals_json_all_users", lambda **kw: None)

    result = cpb.main()
    assert result == 1, f"main() returned {result} — expected 1 on push failure"
    accepted_acks = [bid for bid in ack_called if bid == "bet-1"]
    assert not accepted_acks, (
        f"ACK must NOT be called for accepted bets when push fails; got ack_called={ack_called}"
    )


def test_7_replay_after_push_ack_crash_is_idempotent(tmp_path):
    """If push succeeded but ACK failed, retry run succeeds without double-write.

    The duplicate guard in _append_rows ensures no second ledger row is created
    for an already-appended (match_id, market) pair.
    """
    import pandas as pd
    from src.betting.ledger import _FIELDS

    ledger_file = tmp_path / "ledger_philip.csv"

    # Pre-seed ledger with a row that was already appended (but ACK failed)
    existing_row = {f: "" for f in _FIELDS}
    existing_row.update({"match_id": "dup-123", "market": "h2h", "home": "A", "away": "B",
                          "status": "open", "pnl": "0.0"})
    pd.DataFrame([existing_row], columns=_FIELDS).to_csv(ledger_file, index=False)

    # Now try to append the same row again (simulating retry after ACK crash)
    import os
    import importlib
    os.environ["SPORTSBRAIN_LEDGER_DIR"] = str(tmp_path)
    import src.config as cfg
    importlib.reload(cfg)
    import src.betting.ledger as lm
    importlib.reload(lm)

    from scripts.consume_pending_bets import _append_rows
    dup_row = dict(existing_row)
    added = _append_rows([dup_row], user="philip")
    assert added == 0, f"Duplicate row must be skipped; got added={added}"

    df = pd.read_csv(ledger_file)
    assert len(df) == 1, f"Ledger must have exactly 1 row after idempotent replay; got {len(df)}"


def test_8_two_users_are_isolated(tmp_path, monkeypatch):
    """Two users must get different ledger paths — no cross-contamination possible."""
    monkeypatch.setenv("SPORTSBRAIN_LEDGER_DIR", str(tmp_path))
    import importlib
    import src.config as cfg
    importlib.reload(cfg)

    path_alice = cfg.ledger_path_for("alice")
    path_bob = cfg.ledger_path_for("bob")

    assert path_alice != path_bob, (
        f"ledger_path_for('alice') == ledger_path_for('bob') — users must be isolated"
    )
    assert "alice" in path_alice.name, f"alice ledger not namespaced: {path_alice}"
    assert "bob" in path_bob.name, f"bob ledger not namespaced: {path_bob}"
    # Both must be under the private ledger dir
    assert str(tmp_path) in str(path_alice)
    assert str(tmp_path) in str(path_bob)


def test_9_bot_commit_push_not_in_financial_workflows():
    """None of the four financial workflows call _bot_commit_push.sh."""
    violations: list[str] = []
    for wf_name in _ALL_FINANCIAL:
        wf = WORKFLOWS_DIR / wf_name
        if not wf.exists():
            violations.append(f"{wf_name}: file not found")
            continue
        if "_bot_commit_push" in wf.read_text():
            violations.append(f"{wf_name}: references _bot_commit_push.sh (must not)")
    assert not violations, (
        "Financial workflows must NOT use _bot_commit_push.sh:\n"
        + "\n".join(f"  {v}" for v in violations)
    )


def test_10_health_publication_does_not_redefine_ledger_durability():
    """consume_pending_bets.yml health commit step must NOT git-add ledger files.

    Ledger durability is owned by the Python _durable_push() function.
    The workflow's commit step handles health artifacts only.
    """
    wf = WORKFLOWS_DIR / _CONSUME_WORKFLOW
    assert wf.exists(), f"{_CONSUME_WORKFLOW} not found"
    text = wf.read_text()

    commit_block_match = re.search(
        r"name:\s*Commit health artifacts.*?(?=\n\s*-\s*name:|\Z)",
        text, re.DOTALL
    )
    assert commit_block_match, (
        "consume_pending_bets.yml must contain a 'Commit health artifacts' step"
    )
    commit_block = commit_block_match.group(0)

    git_add_lines = [
        line for line in commit_block.splitlines()
        if re.search(r"git\s+add", line) and not line.strip().startswith("#")
    ]
    ledger_adds = [l for l in git_add_lines if "ledger" in l]
    assert not ledger_adds, (
        "consume_pending_bets.yml 'Commit health artifacts' step must NOT git-add "
        f"ledger files — ledger durability is owned by the Python script:\n"
        + "\n".join(f"  {l}" for l in ledger_adds)
    )


def test_11_betting_journal_not_in_public_gitignore_tracked():
    """betting_journal.md must be in .gitignore OR not tracked by git in the public repo.

    SEC-001: betting journal is a financial artifact and must live exclusively in
    the private ledger repo.
    """
    gitignore = ROOT / ".gitignore"
    assert gitignore.exists(), ".gitignore must exist"
    gitignore_text = gitignore.read_text()

    # Check if any pattern in .gitignore covers betting_journal.md
    patterns = [line.strip() for line in gitignore_text.splitlines() if line.strip() and not line.startswith("#")]
    covered = any(
        "betting_journal" in p or "betting_journal.md" in p
        for p in patterns
    )
    assert covered, (
        "betting_journal.md must be covered by a .gitignore pattern to prevent "
        "accidental commit to the public repo. Add 'results/betting_journal.md' to .gitignore."
    )


def test_12_ledger_patterns_gitignored():
    """.gitignore must contain patterns matching ledger_*.csv and ledger_*.db.

    SEC-001: prevents accidental commit of ledger files to Philip3006/sportsbrain (public).
    """
    gitignore = ROOT / ".gitignore"
    assert gitignore.exists(), ".gitignore must exist"
    text = gitignore.read_text()

    lines = [l.strip() for l in text.splitlines() if l.strip() and not l.strip().startswith("#")]

    has_csv = any("ledger_*.csv" in l or "ledger*.csv" in l for l in lines)
    has_db  = any("ledger_*.db" in l or "ledger*.db" in l for l in lines)

    assert has_csv, (
        ".gitignore must contain a pattern matching 'ledger_*.csv' to protect financial data. "
        f"Current patterns: {lines}"
    )
    assert has_db, (
        ".gitignore must contain a pattern matching 'ledger_*.db' to protect financial data. "
        f"Current patterns: {lines}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# PORTED FROM P0D-001 (Invariants A–K)
# ─────────────────────────────────────────────────────────────────────────────

# ── I: _durable_push must NOT reference results/ prefix ──────────────────────

def test_i1_durable_push_git_add_uses_no_results_prefix():
    """_durable_push() must NOT call `git add results/ledger_*.csv`.

    P0D-002: ledger files live at the root of the private repo — git add must
    use `ledger_*.csv` without any subdirectory prefix.
    """
    source = CONSUME_SCRIPT.read_text()
    # Ensure old public-repo path is gone
    assert "results/ledger_*.csv" not in source, (
        "_durable_push() must NOT use 'results/ledger_*.csv' (public-repo path). "
        "Private repo uses 'ledger_*.csv' at root level."
    )


# ── G: _bot_commit_push.sh not in financial consumer script ──────────────────

def test_g1_bot_commit_push_not_imported_or_called_in_financial_script():
    """The financial consumer script must never reference _bot_commit_push.sh."""
    source = CONSUME_SCRIPT.read_text()
    assert "_bot_commit_push" not in source, (
        "consume_pending_bets.py must not reference _bot_commit_push.sh"
    )


def test_g2_bot_assert_staged_safe_not_called_in_financial_path():
    """bot_assert_staged_safe() must not be called from _durable_push."""
    source = CONSUME_SCRIPT.read_text()
    assert "bot_assert_staged_safe" not in source, (
        "consume_pending_bets.py must not call bot_assert_staged_safe"
    )


# ── K: Workflow classification ────────────────────────────────────────────────

def test_k1_authoritative_financial_workflows_do_not_use_bot_primitive():
    """AUTHORITATIVE_FINANCIAL_TRANSACTION workflows must NOT route through _bot_commit_push.sh."""
    violations: list[str] = []
    for wf_name in _AUTHORITATIVE_FINANCIAL:
        wf = WORKFLOWS_DIR / wf_name
        if not wf.exists():
            violations.append(f"{wf_name}: file not found")
            continue
        if "_bot_commit_push" in wf.read_text():
            violations.append(f"{wf_name}: references _bot_commit_push.sh (must not)")
    assert not violations, "\n".join(f"  {v}" for v in violations)


def test_k2_consume_pending_bets_workflow_health_step_does_not_touch_ledger():
    """consume_pending_bets.yml commit step stages ONLY health artifacts, NOT ledger_*.csv."""
    wf = WORKFLOWS_DIR / _CONSUME_WORKFLOW
    assert wf.exists()
    text = wf.read_text()
    commit_block_match = re.search(
        r"name:\s*Commit health artifacts.*?(?=\n\s*-\s*name:|\Z)",
        text, re.DOTALL
    )
    assert commit_block_match, "Must contain 'Commit health artifacts' step"
    commit_block = commit_block_match.group(0)
    git_add_lines = [
        l for l in commit_block.splitlines()
        if re.search(r"git\s+add", l) and not l.strip().startswith("#")
    ]
    ledger_adds = [l for l in git_add_lines if "ledger" in l]
    assert not ledger_adds, (
        "Commit health artifacts step must NOT git-add ledger files:\n"
        + "\n".join(f"  {l}" for l in ledger_adds)
    )


def test_k3_all_four_financial_workflows_exist_and_are_classified():
    """All four financial writer workflows exist."""
    missing = [wf for wf in sorted(_ALL_FINANCIAL) if not (WORKFLOWS_DIR / wf).exists()]
    assert not missing, "Financial workflows not found:\n" + "\n".join(f"  {m}" for m in missing)


def test_k4_authoritative_financial_workflows_have_own_retry_loops():
    """AUTHORITATIVE_FINANCIAL_TRANSACTION workflows must each have their own push retry loop."""
    _RETRY_PATTERN = re.compile(r"for i in 1 2 3 4 5", re.MULTILINE)
    violations: list[str] = []
    for wf_name in _AUTHORITATIVE_FINANCIAL:
        wf = WORKFLOWS_DIR / wf_name
        if not wf.exists():
            violations.append(f"{wf_name}: file not found")
            continue
        if not _RETRY_PATTERN.search(wf.read_text()):
            violations.append(f"{wf_name}: missing independent push retry loop")
    assert not violations, "\n".join(f"  {v}" for v in violations)


# ── E: Duplicate bet → idempotent ────────────────────────────────────────────

def test_e1_duplicate_bet_skipped_by_append_rows(tmp_path, monkeypatch):
    """_append_rows() must skip rows whose (match_id, market) already exists."""
    import pandas as pd
    import os
    os.environ["SPORTSBRAIN_LEDGER_DIR"] = str(tmp_path)
    import importlib
    import src.config as cfg
    importlib.reload(cfg)
    import src.betting.ledger as lm
    importlib.reload(lm)

    from scripts.consume_pending_bets import _append_rows, _match_id
    from src.betting.ledger import _FIELDS

    mid = _match_id("HomeFC", "AwayFC", "2026-08-18")
    first_row = {f: "" for f in _FIELDS}
    first_row.update({"match_id": mid, "market": "h2h", "home": "HomeFC", "away": "AwayFC",
                       "status": "open", "pnl": "0.0"})

    added1 = _append_rows([first_row], user="philip")
    assert added1 == 1, "First insert must add 1 row"

    added2 = _append_rows([first_row], user="philip")
    assert added2 == 0, "Duplicate row must be skipped"

    df = pd.read_csv(tmp_path / "ledger_philip.csv")
    assert len(df) == 1, f"Ledger must contain exactly 1 row; got {len(df)}"


# ── H: Malformed/forbidden request rejection ─────────────────────────────────

def test_h1_missing_source_rejects_with_no_ledger_row():
    """Bet with missing 'source' field is permanently rejected."""
    from scripts.consume_pending_bets import _row_from_bet
    row, reason = _row_from_bet(
        {"match": "Home FC vs Away FC", "market": "h2h", "odds": 2.0,
         "stake_eur": 5.0, "kickoff": "2026-08-20T15:00:00Z"},
        today="2026-08-20",
        bankroll=100.0,
    )
    assert row is None
    assert "source" in reason.lower() or "permanent" in reason.lower()


def test_h2_stake_exceeds_cap_rejects_with_no_ledger_row():
    """Bet with stake > 5% of bankroll is permanently rejected."""
    from scripts.consume_pending_bets import _row_from_bet
    row, reason = _row_from_bet(
        {"match": "Home FC vs Away FC", "market": "h2h", "odds": 2.0,
         "stake_eur": 10.0, "source": "manual", "kickoff": "2026-08-20T15:00:00Z"},
        today="2026-08-20",
        bankroll=100.0,
    )
    assert row is None
    assert "cap" in reason.lower() or "5%" in reason


def test_h3_value_source_without_signal_id_rejects():
    """Bet with source='value' but no signal_id is permanently rejected."""
    from scripts.consume_pending_bets import _row_from_bet
    row, reason = _row_from_bet(
        {"match": "Home FC vs Away FC", "market": "h2h", "odds": 2.0,
         "stake_eur": 5.0, "source": "value", "signal_id": "",
         "model_prob": 0.55, "kickoff": "2026-08-20T15:00:00Z"},
        today="2026-08-20",
        bankroll=100.0,
    )
    assert row is None
    assert "signal_id" in reason.lower() or "reject" in reason.lower()


# ── C: Transient failure is retry, not reject ─────────────────────────────────

def test_c1_bankroll_unavailable_is_retry_not_reject():
    """bankroll=None (infrastructure outage) must return RETRY sentinel."""
    from scripts.consume_pending_bets import _RETRY_PREFIX, _row_from_bet
    row, reason = _row_from_bet(
        {"match": "Home FC vs Away FC", "market": "h2h", "odds": 2.0,
         "stake_eur": 5.0, "source": "manual", "kickoff": "2026-08-20T15:00:00Z"},
        today="2026-08-20",
        bankroll=None,
    )
    assert row is None
    assert reason.startswith(_RETRY_PREFIX), (
        f"bankroll=None must return RETRY sentinel: {reason!r}"
    )


# ── D: Safe replay without double-write ──────────────────────────────────────

def test_d1_durable_push_source_reflects_idempotent_replay_path():
    """_durable_push contains remote-containment check enabling safe replay."""
    source = CONSUME_SCRIPT.read_text()
    assert "merge-base" in source and "--is-ancestor" in source, (
        "_durable_push() must contain 'merge-base --is-ancestor' for remote-containment"
    )


# ── A: Mutation precedes ACK ─────────────────────────────────────────────────

def test_a1_consume_main_appends_before_push_before_ack():
    """main() documents the P0-A lifecycle: append → push → ACK."""
    source = CONSUME_SCRIPT.read_text()
    assert "Append ACCEPTED rows to per-user ledger" in source
    assert "_durable_push" in source
    assert "ACK/delete ACCEPTED pending entries ONLY after push" in source


# ── B: Push failure propagates as main() return 1 ────────────────────────────

def test_b1_push_failure_propagates_as_main_return_1():
    """_durable_push() returning False must cause main() to return 1 without ACK."""
    source = CONSUME_SCRIPT.read_text()
    assert "push_ok = _durable_push(" in source
    assert "if not push_ok:" in source or "if not push_ok" in source
    assert "return 1" in source


# ── J: P0-A regression suite still importable and non-empty ──────────────────

def test_j1_p0a_lifecycle_tests_exist_and_are_importable():
    """The P0-A lifecycle regression suite must exist and contain required tests."""
    test_path = ROOT / "tests" / "scripts" / "test_consume_pending_bets.py"
    assert test_path.exists(), "test_consume_pending_bets.py must exist"
    source = test_path.read_text()
    lifecycle_tests = re.findall(r"def (test_lifecycle_[a-z_]+)\(", source)
    fnd_tests = re.findall(r"def (test_fnd0[0-9]+_[a-z_]+)\(", source)
    assert len(lifecycle_tests) >= 5, (
        f"Expected ≥5 lifecycle tests, found {len(lifecycle_tests)}: {lifecycle_tests}"
    )
    assert len(fnd_tests) >= 5, (
        f"Expected ≥5 FND regression tests, found {len(fnd_tests)}: {fnd_tests}"
    )


def test_j2_financial_governance_sentinel():
    """Sentinel: this test file exists and was loaded by the financial governance gate."""
    assert Path(__file__).exists()
    assert Path(__file__).stem == "test_financial_writer_governance"


# ─────────────────────────────────────────────────────────────────────────────
# Meta: unique test names (BLOCKER 1 correction)
# ─────────────────────────────────────────────────────────────────────────────

def test_meta_no_duplicate_test_names_in_governance_suite():
    """All def test_* names in this module must be globally unique.

    Python silently overwrites earlier definitions when two functions share a name,
    causing pytest to collect only one of them and falsely report the count.
    """
    source = Path(__file__).read_text()
    names: list[str] = re.findall(r"^def (test_\w+)\(", source, re.MULTILINE)
    seen: dict[str, int] = {}
    duplicates: list[str] = []
    for n in names:
        seen[n] = seen.get(n, 0) + 1
        if seen[n] == 2:
            duplicates.append(n)
    assert not duplicates, (
        "Duplicate test function names found in governance suite — "
        "each test must have a unique name or pytest will silently discard one:\n"
        + "\n".join(f"  {n}" for n in duplicates)
    )


# ─────────────────────────────────────────────────────────────────────────────
# Reader migration regression (CEO Review Blocker 1)
# ─────────────────────────────────────────────────────────────────────────────

def test_reader_1_list_known_users_discovers_from_private_ledger_dir(tmp_path, monkeypatch):
    """list_known_users() must discover user slugs from SPORTSBRAIN_LEDGER_DIR,
    never from public results/.
    """
    monkeypatch.setenv("SPORTSBRAIN_LEDGER_DIR", str(tmp_path))
    (tmp_path / "ledger_philip.csv").write_text("status\nopen\n")
    (ROOT / "results").mkdir(parents=True, exist_ok=True)

    import importlib
    import src.config as cfg
    importlib.reload(cfg)
    import src.notifications.web_dashboard as wdb
    importlib.reload(wdb)

    users = wdb.list_known_users()
    assert "philip" in users, (
        f"list_known_users() must discover 'philip' from SPORTSBRAIN_LEDGER_DIR ({tmp_path}); got {users}"
    )


def test_reader_2_list_known_users_source_does_not_glob_public_results():
    """web_dashboard.list_known_users() must NOT glob from ROOT/results/ after P0D-002."""
    source = (ROOT / "src" / "notifications" / "web_dashboard.py").read_text()
    in_func = False
    func_lines: list[str] = []
    for line in source.splitlines():
        if line.startswith("def list_known_users("):
            in_func = True
        elif in_func and line.startswith("def "):
            break
        if in_func:
            func_lines.append(line)
    func_body = "\n".join(func_lines)
    assert '"results"' not in func_body or "glob" not in func_body, (
        'list_known_users() must NOT glob from (ROOT / "results") — '
        "use _resolve_ledger_dir_cfg() for private ledger discovery"
    )


def test_reader_3_outcome_checks_ledger_path_not_hardcoded():
    """src/monitoring/outcome_checks.py must NOT hardcode path to public results/."""
    source = (ROOT / "src" / "monitoring" / "outcome_checks.py").read_text()
    forbidden = 'ROOT / "results" / "ledger_philip.csv"'
    assert forbidden not in source, (
        f"outcome_checks.py must not hardcode {forbidden!r} — use ledger_path_for()"
    )


def test_reader_4_scanner_workflows_have_private_ledger_checkout():
    """Runtime scanner workflows that read ledger data must checkout private ledger."""
    _READER_WORKFLOWS = {
        "tennis_scan.yml",
        "bundesliga2_scan.yml",
        "tennis_live_scan.yml",
        "bundesliga2_live_push.yml",
        "tennis_clv_monitor.yml",
        "weekly_recap.yml",
    }
    violations: list[str] = []
    for wf_name in sorted(_READER_WORKFLOWS):
        wf = WORKFLOWS_DIR / wf_name
        if not wf.exists():
            violations.append(f"{wf_name}: file not found")
            continue
        text = wf.read_text()
        if "sportsbrain-ledger" not in text:
            violations.append(f"{wf_name}: missing private ledger checkout (sportsbrain-ledger)")
        if "SPORTSBRAIN_LEDGER_DIR" not in text:
            violations.append(f"{wf_name}: missing SPORTSBRAIN_LEDGER_DIR env export")
        if "LEDGER_PRIVATE_READ_PAT" not in text:
            violations.append(f"{wf_name}: must use LEDGER_PRIVATE_READ_PAT (read-only)")
    assert not violations, (
        "Scanner workflows must checkout private ledger (read-only) and export "
        "SPORTSBRAIN_LEDGER_DIR:\n" + "\n".join(f"  {v}" for v in violations)
    )


# ─────────────────────────────────────────────────────────────────────────────
# Betting journal runtime regression (CEO Review Blocker 4)
# ─────────────────────────────────────────────────────────────────────────────

def test_journal_1_path_resolves_inside_private_ledger_dir(tmp_path, monkeypatch):
    """betting_journal_path() must resolve inside SPORTSBRAIN_LEDGER_DIR."""
    monkeypatch.setenv("SPORTSBRAIN_LEDGER_DIR", str(tmp_path))
    import importlib
    import src.config as cfg
    importlib.reload(cfg)

    journal_path = cfg.betting_journal_path()
    assert journal_path.is_relative_to(tmp_path), (
        f"betting_journal_path() must be inside SPORTSBRAIN_LEDGER_DIR ({tmp_path}); got {journal_path}"
    )
    public_path = ROOT / "results" / "betting_journal.md"
    assert journal_path != public_path, (
        f"betting_journal_path() must not resolve to public results/ path: {journal_path}"
    )


def test_journal_2_script_does_not_hardcode_public_results():
    """update_betting_journal.py must NOT hardcode JOURNAL_PATH to results/."""
    source = (ROOT / "scripts" / "update_betting_journal.py").read_text()
    assert 'ROOT / "results" / "betting_journal.md"' not in source, (
        "update_betting_journal.py must not hardcode public results/ journal path — "
        "use _betting_journal_path() from src.config"
    )


def test_journal_3_main_writes_to_private_ledger_not_public_results(tmp_path, monkeypatch):
    """update_betting_journal.main() must write betting_journal.md to SPORTSBRAIN_LEDGER_DIR.

    CEO Blocker 4: test must invoke the actual script output path, not manually write
    the file in place of the script. This proves the production code path is correct.
    """
    import json
    monkeypatch.setenv("SPORTSBRAIN_LEDGER_DIR", str(tmp_path))

    # Prepare minimal signal_history.jsonl in a temp cache dir
    cache_dir = tmp_path / "data_cache"
    cache_dir.mkdir()

    import importlib
    import src.config as cfg
    importlib.reload(cfg)
    import scripts.update_betting_journal as ubj
    importlib.reload(ubj)

    # Point the script at our temp signal history (no signals = valid empty case)
    monkeypatch.setattr(ubj, "SIGNAL_HISTORY", cache_dir / "signal_history.jsonl")

    # Provide one minimal settled signal so the journal is non-empty
    signal = {
        "signal_id": "test-sig-001",
        "sport": "tennis",
        "home": "Player A",
        "away": "Player B",
        "market": "h2h",
        "odds": 2.1,
        "model_prob": 0.55,
        "ev": 0.155,
        "stake_eur": 10.0,
        "outcome": "won",
        "pnl": 11.0,
        "signal_date": "2026-08-19",
        "settle_date": "2026-08-19",
    }
    (cache_dir / "signal_history.jsonl").write_text(json.dumps(signal) + "\n")

    # Capture what path the script would write to
    written_paths: list[str] = []
    original_write_text = None

    import pathlib
    original_write = pathlib.Path.write_text

    def _capture_write(self, data, *args, **kwargs):
        written_paths.append(str(self))
        return original_write(self, data, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "write_text", _capture_write)

    # Execute the script's main() with sport=all
    import sys
    monkeypatch.setattr(sys, "argv", ["update_betting_journal.py"])
    ubj.main()

    # The private ledger journal must have been written
    expected_journal = tmp_path / "betting_journal.md"
    assert expected_journal.exists(), (
        f"update_betting_journal.main() must write betting_journal.md to "
        f"SPORTSBRAIN_LEDGER_DIR ({tmp_path}); written_paths={written_paths}"
    )
    content = expected_journal.read_text()
    assert len(content) > 0, "betting_journal.md must not be empty after main()"

    # Public results/betting_journal.md must NOT have been written
    public_journal = ROOT / "results" / "betting_journal.md"
    assert str(public_journal) not in written_paths, (
        f"update_betting_journal.main() must NOT write to public results/: "
        f"found {public_journal} in written_paths={written_paths}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Rollback governance (CEO Review Blocker 4 — no fallback to public results/)
# ─────────────────────────────────────────────────────────────────────────────

def test_rollback_1_resolve_ledger_dir_has_no_fallback():
    """_resolve_ledger_dir() must raise EnvironmentError when env var is unset."""
    import importlib
    import src.config as cfg
    import os
    saved = os.environ.pop("SPORTSBRAIN_LEDGER_DIR", None)
    try:
        importlib.reload(cfg)
        with pytest.raises(EnvironmentError, match="SPORTSBRAIN_LEDGER_DIR"):
            cfg._resolve_ledger_dir()
    finally:
        if saved is not None:
            os.environ["SPORTSBRAIN_LEDGER_DIR"] = saved


def test_rollback_2_config_has_no_results_ledger_fallback():
    """src/config.py must not contain a fallback path pointing to public results/."""
    source = (ROOT / "src" / "config.py").read_text()
    lines = source.splitlines()
    violations = []
    for i, line in enumerate(lines, 1):
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if "results" in stripped and ("ledger" in stripped or "LEDGER" in stripped):
            violations.append(f"config.py:{i}: {stripped!r}")
    assert not violations, (
        "config.py must not reference 'results' near ledger paths:\n"
        + "\n".join(f"  {v}" for v in violations)
    )


def test_rollback_3_list_known_users_degrades_safely_not_silently():
    """list_known_users() when env var unset returns [DEFAULT_USER], never public results/."""
    import importlib
    import src.config as cfg
    import src.notifications.web_dashboard as wdb
    import os

    saved = os.environ.pop("SPORTSBRAIN_LEDGER_DIR", None)
    try:
        importlib.reload(cfg)
        importlib.reload(wdb)
        users = wdb.list_known_users()
        from src.config import DEFAULT_USER
        assert users == [DEFAULT_USER], (
            f"list_known_users() when env var unset must return [{DEFAULT_USER!r}]; got {users}"
        )
    finally:
        if saved is not None:
            os.environ["SPORTSBRAIN_LEDGER_DIR"] = saved


def test_rollback_4_financial_workflows_never_git_add_public_results_ledger():
    """No authoritative financial workflow git-adds ledger files from public results/."""
    violations: list[str] = []
    for wf_name in _ALL_FINANCIAL:
        wf = WORKFLOWS_DIR / wf_name
        if not wf.exists():
            violations.append(f"{wf_name}: file not found")
            continue
        text = wf.read_text()
        for lineno, line in enumerate(text.splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if "results/" in stripped and "ledger" in stripped and re.search(r"git\s+add", stripped):
                violations.append(f"{wf_name}:{lineno}: {stripped!r}")
    assert not violations, (
        "Financial workflows must NOT git-add ledger files from public results/:\n"
        + "\n".join(f"  {v}" for v in violations)
    )


# ─────────────────────────────────────────────────────────────────────────────
# SQLite stale-mirror regression (CEO Review Blocker 3)
# ─────────────────────────────────────────────────────────────────────────────

def test_sqlite_1_count_open_bets_uses_csv_not_stale_sqlite(tmp_path, monkeypatch):
    """count_open_bets() must return CSV-authoritative count even when stale SQLite exists.

    Scenario: CSV has 3 open bets, SQLite mirror has 0 (stale after fresh checkout).
    count_open_bets() must return 3 (CSV truth), never 0 (stale DB).

    P0D-002: SQLite may be stale after a fresh private repo checkout. MAX_ACTIVE_BETS
    enforcement is a risk-critical gate and must never be weakened by stale DB data.
    """
    import sqlite3
    import pandas as pd
    from src.betting.ledger import _FIELDS, count_open_bets

    monkeypatch.setenv("SPORTSBRAIN_LEDGER_DIR", str(tmp_path))
    import importlib
    import src.config as cfg
    importlib.reload(cfg)
    import src.betting.ledger as lm
    importlib.reload(lm)
    from src.betting.ledger import count_open_bets as cob

    ledger_file = tmp_path / "ledger_philip.csv"

    # Write 3 open bets to CSV (authoritative)
    rows = []
    for i in range(3):
        r = {f: "" for f in _FIELDS}
        r.update({"match_id": f"match-{i}", "market": "h2h", "home": f"TeamA{i}",
                  "away": f"TeamB{i}", "status": "open", "pnl": "0.0"})
        rows.append(r)
    pd.DataFrame(rows, columns=_FIELDS).to_csv(ledger_file, index=False)

    # Create stale SQLite with 0 bets (simulates fresh checkout after CSV was updated)
    db_file = tmp_path / "ledger_philip.db"
    conn = sqlite3.connect(str(db_file))
    conn.execute("CREATE TABLE bets (status TEXT)")
    conn.commit()
    conn.close()

    result = cob(ledger_file)
    assert result == 3, (
        f"count_open_bets() must return CSV-authoritative count (3), "
        f"not stale SQLite count (0); got {result}"
    )


def test_sqlite_2_count_open_bets_source_does_not_prefer_sqlite():
    """count_open_bets() source must not contain SQLite fast-path before CSV read.

    Static analysis: the CSV-authoritative correction must eliminate the SQLite
    preference so future refactors cannot accidentally re-introduce it.
    """
    source = (ROOT / "src" / "betting" / "ledger.py").read_text()
    in_func = False
    func_lines: list[str] = []
    for line in source.splitlines():
        if "def count_open_bets(" in line:
            in_func = True
        elif in_func and line.startswith("def "):
            break
        if in_func:
            func_lines.append(line)
    func_body = "\n".join(func_lines)

    # Must not prefer SQLite over CSV
    assert "db_path.exists()" not in func_body, (
        "count_open_bets() must not check db_path.exists() — "
        "SQLite mirror may be stale after a fresh private repo checkout"
    )
    assert "open_db" not in func_body, (
        "count_open_bets() must not call open_db() (SQLite) — use CSV authoritative path"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Bankroll snapshot durability (CEO Review Blocker 2)
# ─────────────────────────────────────────────────────────────────────────────

def test_snapshot_1_durable_push_stages_bankroll_snapshot():
    """_durable_push() must git-add bankroll_snapshot_*.json alongside ledger_*.csv.

    cancel_bet() mutates bankroll_snapshot_*.json; that mutation must survive
    a fresh private-repo checkout — therefore it must be committed to the private repo.
    """
    source = CONSUME_SCRIPT.read_text()
    assert "bankroll_snapshot_*.json" in source, (
        "_durable_push() must git-add 'bankroll_snapshot_*.json' to persist "
        "cancel_bet() snapshot mutations through private repo checkouts"
    )


def test_snapshot_2_reader_workflows_do_not_need_write_permission_for_snapshot():
    """Reader workflows (LEDGER_PRIVATE_READ_PAT) must not mutate bankroll_snapshot_*.json.

    cancel_bet() is only called from consume_pending_bets.py which holds write
    credentials. Reader workflows (tennis_scan, etc.) must never call cancel_bet()
    or get_bankroll_snapshot() — those require write access.
    """
    _READER_SCRIPTS = {
        "scripts/tennis_scan.py",
        "scripts/bundesliga2_scan.py",
        "scripts/tennis_live_push.py",
        "scripts/bundesliga2_live_push.py",
        "scripts/monitor_clv.py",
        "scripts/weekly_recap.py",
    }
    violations: list[str] = []
    for script_path in sorted(_READER_SCRIPTS):
        path = ROOT / script_path
        if not path.exists():
            continue
        source = path.read_text()
        if "cancel_bet" in source:
            violations.append(f"{script_path}: calls cancel_bet() — requires write access")
        if "get_bankroll_snapshot" in source:
            violations.append(f"{script_path}: calls get_bankroll_snapshot() — writes snapshot on week boundary")
    assert not violations, (
        "Reader workflows must not mutate bankroll_snapshot_*.json "
        "(they use read-only credentials):\n"
        + "\n".join(f"  {v}" for v in violations)
    )


def test_snapshot_3_settle_workflows_do_not_speculatively_stage_snapshot():
    """Authoritative settle workflows must NOT stage bankroll_snapshot_*.json.

    P0D-002: Persistence ownership follows mutation ownership. cancel_bet() is ONLY
    called from consume_pending_bets.py (via _durable_push). Settle workflows
    (tennis_settle, tennis_closing_odds, bundesliga2_settle) never call cancel_bet()
    and must not speculatively stage snapshots they did not mutate.
    """
    violations: list[str] = []
    for wf_name in _AUTHORITATIVE_FINANCIAL:
        wf = WORKFLOWS_DIR / wf_name
        if not wf.exists():
            violations.append(f"{wf_name}: file not found")
            continue
        text = wf.read_text()
        if "bankroll_snapshot_*.json" in text:
            violations.append(
                f"{wf_name}: stages bankroll_snapshot_*.json speculatively "
                "(must not — cancel_bet() is owned by consume_pending_bets only)"
            )
    assert not violations, (
        "Settle workflows must NOT git-add bankroll_snapshot_*.json (mutation ownership violation):\n"
        + "\n".join(f"  {v}" for v in violations)
    )


# ─────────────────────────────────────────────────────────────────────────────
# Snapshot fail-closed regressions (CEO Final Correction Item 1)
# ─────────────────────────────────────────────────────────────────────────────

def test_snapshot_4_durable_push_snapshot_exists_git_add_fails_returns_false(tmp_path, monkeypatch):
    """_durable_push() must return False (no ACK) when snapshot exists but git add fails.

    Mutation ownership: snapshot files present in the ledger dir must be staged
    fail-closed. A git-add failure on an existing snapshot must block the ACK.
    """
    import subprocess
    import scripts.consume_pending_bets as cpb

    monkeypatch.setattr(cpb, "_LEDGER_ROOT", tmp_path)

    # Create a snapshot file so the conditional staging branch is taken
    (tmp_path / "bankroll_snapshot_philip.json").write_text('{"bankroll": 100}')

    _fail = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="simulated add failure")

    def _mock_run(args, **kwargs):
        cmd = args[1] if len(args) > 1 else ""
        subcmd = args[2] if len(args) > 2 else ""
        if cmd == "add" and "bankroll_snapshot" in subcmd:
            return _fail
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", _mock_run)
    result = cpb._durable_push(added=1)
    assert result is False, (
        "_durable_push() must return False when snapshot exists and git add fails — "
        "no ACK may be emitted until private ledger is durable"
    )


def test_snapshot_5_durable_push_no_snapshot_is_safe_noop(tmp_path, monkeypatch):
    """_durable_push() must not fail when no bankroll_snapshot_*.json files exist.

    Fresh ledger (first run): no snapshot file present → skip staging, proceed normally.
    This ensures first-run and empty-ledger scenarios do not fail-close unnecessarily.
    """
    import subprocess
    import scripts.consume_pending_bets as cpb

    monkeypatch.setattr(cpb, "_LEDGER_ROOT", tmp_path)
    # No snapshot files in ledger dir

    snapshot_add_attempted = []

    def _mock_run(args, **kwargs):
        subcmd = args[2] if len(args) > 2 else ""
        if "bankroll_snapshot" in subcmd:
            snapshot_add_attempted.append(subcmd)
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", _mock_run)
    cpb._durable_push(added=1)
    assert not snapshot_add_attempted, (
        "_durable_push() must not attempt git add bankroll_snapshot_*.json "
        f"when no snapshot files exist; attempted: {snapshot_add_attempted}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Journal staging governance (CEO Final Correction Item 2)
# ─────────────────────────────────────────────────────────────────────────────

def test_journal_4_tennis_settle_stages_betting_journal_fail_closed():
    """tennis_settle.yml must stage betting_journal.md fail-closed (no || true).

    update_betting_journal.py mutates betting_journal.md on every settle run.
    A || true on the git-add would silently mask staging failures and leave
    a stale journal in the private ledger.
    """
    wf = WORKFLOWS_DIR / "tennis_settle.yml"
    assert wf.exists(), "tennis_settle.yml not found"
    text = wf.read_text()
    # Find the line that stages betting_journal.md
    for lineno, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if "betting_journal.md" in stripped and re.search(r"git.*add", stripped):
            assert "|| true" not in stripped and "2>/dev/null" not in stripped, (
                f"tennis_settle.yml:{lineno}: betting_journal.md staging must be fail-closed "
                f"(no || true / 2>/dev/null); found: {stripped!r}"
            )
            return
    pytest.fail("tennis_settle.yml does not stage betting_journal.md at all")


# ─────────────────────────────────────────────────────────────────────────────
# Journal snapshot private boundary (CEO Final Correction Item 3)
# ─────────────────────────────────────────────────────────────────────────────

def test_journal_snapshot_1_path_resolves_to_private_ledger_dir(tmp_path, monkeypatch):
    """betting_journal_snapshot_path() must resolve inside SPORTSBRAIN_LEDGER_DIR."""
    monkeypatch.setenv("SPORTSBRAIN_LEDGER_DIR", str(tmp_path))
    import importlib
    import src.config as cfg
    importlib.reload(cfg)

    snap_path = cfg.betting_journal_snapshot_path()
    assert snap_path.is_relative_to(tmp_path), (
        f"betting_journal_snapshot_path() must be inside SPORTSBRAIN_LEDGER_DIR ({tmp_path}); "
        f"got {snap_path}"
    )
    assert snap_path.name == "journal_snapshots.jsonl", (
        f"betting_journal_snapshot_path() must be named journal_snapshots.jsonl; got {snap_path.name}"
    )
    assert "/data/cache/" not in str(snap_path), (
        f"betting_journal_snapshot_path() must not resolve to public data/cache/; got {snap_path}"
    )


def test_journal_snapshot_2_main_writes_snapshots_to_private_ledger(tmp_path, monkeypatch):
    """update_betting_journal.main() must write journal_snapshots.jsonl to SPORTSBRAIN_LEDGER_DIR.

    The journal snapshot is private financial performance state (staked/pnl/roi).
    It must never land in the public data/cache/ directory.
    """
    import json
    import pathlib
    monkeypatch.setenv("SPORTSBRAIN_LEDGER_DIR", str(tmp_path))
    cache_dir = tmp_path / "data_cache"
    cache_dir.mkdir()

    import importlib
    import src.config as cfg
    importlib.reload(cfg)
    import scripts.update_betting_journal as ubj
    importlib.reload(ubj)

    monkeypatch.setattr(ubj, "SIGNAL_HISTORY", cache_dir / "signal_history.jsonl")

    signal = {
        "signal_id": "snap-test-001",
        "sport": "tennis",
        "home": "Player A",
        "away": "Player B",
        "market": "h2h",
        "odds": 2.1,
        "model_prob": 0.55,
        "ev": 0.155,
        "stake_eur": 10.0,
        "outcome": "won",
        "pnl": 11.0,
        "signal_date": "2026-08-19",
        "settle_date": "2026-08-19",
    }
    (cache_dir / "signal_history.jsonl").write_text(json.dumps(signal) + "\n")

    # Capture append-write paths to detect what files main() actually wrote during this run
    appended_paths: list[str] = []
    original_open = pathlib.Path.open

    def _capture_open(self, mode="r", *args, **kwargs):
        if "a" in mode:
            appended_paths.append(str(self))
        return original_open(self, mode, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "open", _capture_open)

    import sys
    monkeypatch.setattr(sys, "argv", ["update_betting_journal.py"])
    ubj.main()

    private_snapshot = str(tmp_path / "journal_snapshots.jsonl")
    public_data_cache = str(ROOT / "data" / "cache")

    assert any(p == private_snapshot for p in appended_paths), (
        f"update_betting_journal.main() must append to journal_snapshots.jsonl in "
        f"SPORTSBRAIN_LEDGER_DIR ({tmp_path}); appended to: {appended_paths}"
    )
    public_writes = [p for p in appended_paths if public_data_cache in p and "journal_snapshots" in p]
    assert not public_writes, (
        f"update_betting_journal.main() must NOT write journal_snapshots.jsonl to "
        f"public data/cache/; appended to: {public_writes}"
    )


def test_journal_snapshot_3_public_commit_does_not_stage_journal_snapshots():
    """tennis_settle.yml public commit step must NOT stage data/cache/journal_snapshots.jsonl.

    journal_snapshots.jsonl is private financial state. After P0D-002, it is owned
    by the private ledger repo. Staging it in the public artifact commit would expose
    financial performance data in the public sportsbrain repo.
    """
    wf = WORKFLOWS_DIR / "tennis_settle.yml"
    assert wf.exists(), "tennis_settle.yml not found"
    text = wf.read_text()
    for lineno, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if "journal_snapshots.jsonl" in stripped and "data/cache/" in stripped:
            pytest.fail(
                f"tennis_settle.yml:{lineno}: public commit step must NOT stage "
                f"data/cache/journal_snapshots.jsonl (private financial state); "
                f"found: {stripped!r}"
            )


def test_journal_snapshot_4_gitignore_protects_public_journal_snapshots():
    """data/cache/journal_snapshots.jsonl must be gitignored or untracked.

    After P0D-002, journal_snapshots.jsonl belongs in the private ledger repo.
    A gitignore protection rule must prevent it from accidentally being committed
    to the public sportsbrain repo.
    """
    import subprocess
    # Check that git treats the path as ignored
    result = subprocess.run(
        ["git", "check-ignore", "-q", "data/cache/journal_snapshots.jsonl"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
    )
    # git check-ignore returns 0 if the path is ignored
    assert result.returncode == 0, (
        "data/cache/journal_snapshots.jsonl must be gitignored (private financial state); "
        "ensure .gitignore contains data/cache/journal_snapshots.jsonl"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Bankroll snapshot public-tree protection (P0D-002 Phase 5A.5)
# ─────────────────────────────────────────────────────────────────────────────


def test_bankroll_snapshot_1_not_tracked_in_public_repo():
    """data/bankroll_snapshot*.json must not be tracked in the public sportsbrain repo.

    After P0D-002, bankroll snapshots are authoritative private financial state
    stored exclusively in Philip3006/sportsbrain-ledger.  Any file matching
    data/bankroll_snapshot*.json in the public active tree is a privacy defect.
    """
    import subprocess
    result = subprocess.run(
        ["git", "ls-files", "data/bankroll_snapshot.json", "data/bankroll_snapshot_philip.json"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
    )
    tracked = [line for line in result.stdout.splitlines() if line.strip()]
    assert not tracked, (
        "Public sportsbrain repo must not track bankroll snapshot files under data/. "
        f"Found: {tracked}. These are private financial state; "
        "authoritative copy lives in Philip3006/sportsbrain-ledger."
    )


def test_bankroll_snapshot_2_gitignore_protects_public_data_path():
    """.gitignore must contain patterns blocking data/bankroll_snapshot*.json.

    Prevents reintroduction of bankroll snapshot files into the public active tree.
    """
    gitignore = ROOT / ".gitignore"
    assert gitignore.exists(), ".gitignore must exist"
    text = gitignore.read_text()
    patterns = [line.strip() for line in text.splitlines() if line.strip() and not line.startswith("#")]

    def covers(pattern: str, paths: list[str]) -> bool:
        import fnmatch
        return any(fnmatch.fnmatch(p, pattern) for p in paths)

    targets = ["data/bankroll_snapshot.json", "data/bankroll_snapshot_philip.json"]
    assert any(covers(p, targets) for p in patterns), (
        ".gitignore must contain a pattern covering data/bankroll_snapshot*.json to prevent "
        "accidental reintroduction of private financial state into the public repo. "
        "Add 'data/bankroll_snapshot.json' and 'data/bankroll_snapshot_*.json'."
    )


# ─────────────────────────────────────────────────────────────────────────────
# Private-commit success-gate (P0D-002 Phase 5B.3)
# ─────────────────────────────────────────────────────────────────────────────

def test_private_commit_1_not_always_in_settlement_workflows():
    """Authoritative private-ledger commit/push steps must NOT use `if: always()`.

    If a settlement execution step partially mutates the private working tree and
    then fails, `if: always()` would commit and push that partial state as
    authoritative financial truth.  The private persistence step must execute only
    after the settlement execution step succeeds (GitHub Actions default semantics).

    Covers: bundesliga2_settle.yml, tennis_settle.yml, tennis_closing_odds.yml
    """
    settlement_workflows = [
        ROOT / ".github" / "workflows" / "bundesliga2_settle.yml",
        ROOT / ".github" / "workflows" / "tennis_settle.yml",
        ROOT / ".github" / "workflows" / "tennis_closing_odds.yml",
    ]
    violations = []
    for wf_path in settlement_workflows:
        assert wf_path.exists(), f"Workflow file missing: {wf_path.name}"
        text = wf_path.read_text()
        lines = text.splitlines()
        private_commit_idx = None
        for i, line in enumerate(lines):
            if "Commit private ledger" in line:
                private_commit_idx = i
                break
        if private_commit_idx is None:
            violations.append(f"{wf_path.name}: missing 'Commit private ledger' step")
            continue
        # Check the 5 lines immediately after the step name for `if: always()`
        window = lines[private_commit_idx + 1 : private_commit_idx + 6]
        for line in window:
            stripped = line.strip()
            if stripped == "if: always()":
                violations.append(
                    f"{wf_path.name}: 'Commit private ledger' step uses `if: always()` — "
                    "authoritative private persistence must not run after failed execution"
                )
                break
    assert not violations, (
        "Private-ledger commit/push must be success-gated in all settlement workflows.\n"
        + "\n".join(violations)
    )
