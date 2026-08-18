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
    for args in git_add_args:
        # Must not have results/ prefix — that belongs to the old public-repo path.
        assert args == ["add", "ledger_*.csv"], (
            f"_durable_push() git add must use 'ledger_*.csv' (no results/ prefix in private repo); got {args!r}"
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
# BLOCKER 1 — Private ledger reader migration tests
# ─────────────────────────────────────────────────────────────────────────────

def test_b1_list_known_users_discovers_from_private_ledger_dir(tmp_path, monkeypatch):
    """list_known_users() must discover user slugs from SPORTSBRAIN_LEDGER_DIR,
    never from public results/.

    After P0D-002: ledger files live in the private repo checkout (private-ledger/).
    The public repo's results/ will no longer contain ledger_*.csv.
    """
    monkeypatch.setenv("SPORTSBRAIN_LEDGER_DIR", str(tmp_path))
    # Create ledger file in the private ledger dir
    (tmp_path / "ledger_philip.csv").write_text("status\nopen\n")
    # Ensure public results/ has no ledger (it would be stale/wrong source)
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


def test_b2_list_known_users_source_code_does_not_glob_public_results():
    """web_dashboard.list_known_users() must NOT glob from ROOT/results/.

    Static analysis: after P0D-002 the discovery source must be _resolve_ledger_dir_cfg(),
    never (ROOT / 'results').glob(...).
    """
    source = (ROOT / "src" / "notifications" / "web_dashboard.py").read_text()
    # Extract only the list_known_users function body via line-based search
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

    # Must not contain the old public-results glob pattern
    assert '"results"' not in func_body or "glob" not in func_body, (
        'list_known_users() must NOT glob from (ROOT / "results") — '
        "after P0D-002 use _resolve_ledger_dir_cfg() for private ledger discovery.\n"
        f"Found in function body: {func_body[:500]!r}"
    )


def test_b3_outcome_checks_ledger_path_not_hardcoded_to_public_results():
    """src/monitoring/outcome_checks.py must NOT hardcode ledger path to results/.

    After P0D-002: LEDGER_PATH in outcome_checks.py must resolve from
    ledger_path_for() (which uses SPORTSBRAIN_LEDGER_DIR), never be a literal
    ROOT / 'results' / 'ledger_philip.csv' path.
    """
    source = (ROOT / "src" / "monitoring" / "outcome_checks.py").read_text()
    # The old hardcoded path that must be gone
    forbidden = 'ROOT / "results" / "ledger_philip.csv"'
    assert forbidden not in source, (
        f"outcome_checks.py must not hardcode {forbidden!r}. "
        "Use ledger_path_for() so SPORTSBRAIN_LEDGER_DIR is respected."
    )


def test_b4_scanner_workflows_have_private_ledger_checkout():
    """Runtime scanner workflows that read ledger data must checkout private ledger.

    These workflows call ledger_summary() / write_signals_json_all_users() /
    tennis_live_push.py and therefore require SPORTSBRAIN_LEDGER_DIR to be set.
    """
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
            violations.append(f"{wf_name}: must use LEDGER_PRIVATE_READ_PAT (read-only) not write PAT")
    assert not violations, (
        "Scanner workflows must checkout private ledger (read-only) and export "
        "SPORTSBRAIN_LEDGER_DIR:\n" + "\n".join(f"  {v}" for v in violations)
    )


# ─────────────────────────────────────────────────────────────────────────────
# BLOCKER 2 — Betting journal migration tests
# ─────────────────────────────────────────────────────────────────────────────

def test_bj1_update_betting_journal_writes_to_private_ledger_dir(tmp_path, monkeypatch):
    """update_betting_journal.py must write betting_journal.md to SPORTSBRAIN_LEDGER_DIR,
    never to public results/.

    Runtime test: execute the journal path resolution with a temporary ledger dir and
    verify the output path is inside the private ledger dir.
    """
    monkeypatch.setenv("SPORTSBRAIN_LEDGER_DIR", str(tmp_path))
    import importlib
    import src.config as cfg
    importlib.reload(cfg)

    journal_path = cfg.betting_journal_path()
    assert str(tmp_path) in str(journal_path), (
        f"betting_journal_path() must resolve inside SPORTSBRAIN_LEDGER_DIR ({tmp_path}); got {journal_path}"
    )
    assert "results" not in str(journal_path), (
        f"betting_journal_path() must not contain 'results' (public repo path); got {journal_path}"
    )


def test_bj2_update_betting_journal_script_does_not_hardcode_public_results():
    """update_betting_journal.py must NOT hardcode JOURNAL_PATH to results/.

    Static analysis: after P0D-002, the journal must write through _betting_journal_path()
    (which calls betting_journal_path() from config), never via a literal path.
    """
    source = (ROOT / "scripts" / "update_betting_journal.py").read_text()
    forbidden = 'ROOT / "results" / "betting_journal.md"'
    assert forbidden not in source, (
        f"update_betting_journal.py must not hardcode {forbidden!r}. "
        "Use _betting_journal_path() from src.config."
    )


def test_bj3_public_results_betting_journal_not_written_when_private_ledger_set(tmp_path, monkeypatch):
    """When SPORTSBRAIN_LEDGER_DIR is set, public results/betting_journal.md must not be created.

    Verifies separation: journal stays in private ledger; public results/ is untouched.
    """
    monkeypatch.setenv("SPORTSBRAIN_LEDGER_DIR", str(tmp_path))
    import importlib
    import src.config as cfg
    importlib.reload(cfg)

    journal_path = cfg.betting_journal_path()
    # Write something to the private path
    journal_path.parent.mkdir(parents=True, exist_ok=True)
    journal_path.write_text("test journal content")

    # Verify journal resolves inside our private tmp dir, not the public path.
    public_path = ROOT / "results" / "betting_journal.md"
    assert journal_path != public_path, (
        f"betting_journal_path() resolved to the public results/ path: {journal_path}"
    )
    assert journal_path.is_relative_to(tmp_path), (
        f"betting_journal_path() must resolve inside SPORTSBRAIN_LEDGER_DIR ({tmp_path}); got {journal_path}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# BLOCKER 4 — Rollback governance: no silent fallback to public results/
# ─────────────────────────────────────────────────────────────────────────────

def test_r1_resolve_ledger_dir_has_no_fallback_to_public_results():
    """_resolve_ledger_dir() must raise EnvironmentError when env var is unset.

    There must be NO code path that falls back to ROOT/results/ or any
    public directory. Fail-closed is the only safe rollback posture.
    """
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


def test_r2_no_runtime_fallback_to_public_results_in_config():
    """src/config.py must not contain a fallback path pointing to public results/.

    Static analysis: the fail-closed design requires that _resolve_ledger_dir()
    has no alternative path that points to the public repo.
    """
    source = (ROOT / "src" / "config.py").read_text()
    # Search for any path construction that uses "results" near ledger resolution
    lines = source.splitlines()
    fallback_violations = []
    for i, line in enumerate(lines, 1):
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if "results" in stripped and ("ledger" in stripped or "LEDGER" in stripped):
            # Allowed only if inside a comment block — already excluded above
            fallback_violations.append(f"config.py:{i}: {stripped!r}")
    assert not fallback_violations, (
        "config.py must not reference 'results' near ledger paths — "
        "no fallback to public results/ is permitted:\n"
        + "\n".join(f"  {v}" for v in fallback_violations)
    )


def test_r3_no_runtime_fallback_to_public_results_in_web_dashboard():
    """web_dashboard.py list_known_users() must not fall back to public results/ on EnvironmentError.

    Safe rollback: when SPORTSBRAIN_LEDGER_DIR is missing, return the default user only.
    This is a visible degradation (single-user mode), NOT a silent public-data fallback.
    """
    import importlib
    import src.config as cfg
    import src.notifications.web_dashboard as wdb
    import os

    saved = os.environ.pop("SPORTSBRAIN_LEDGER_DIR", None)
    try:
        importlib.reload(cfg)
        importlib.reload(wdb)
        users = wdb.list_known_users()
        # Must return only the default user, not discover from public results/
        from src.config import DEFAULT_USER
        assert users == [DEFAULT_USER], (
            f"When SPORTSBRAIN_LEDGER_DIR is unset, list_known_users() must return "
            f"only [{DEFAULT_USER!r}] (safe degradation); got {users}"
        )
    finally:
        if saved is not None:
            os.environ["SPORTSBRAIN_LEDGER_DIR"] = saved


def test_r4_rollback_posture_no_financial_writer_with_public_path():
    """No authoritative financial workflow has a fallback ledger path in the public repo.

    Static analysis across all 4 financial workflows: no step should write
    ledger data to `results/` directory of the public repo.
    """
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
            # Flag any line that references both results/ and ledger in a git-add context
            if "results/" in stripped and "ledger" in stripped and re.search(r"git\s+add", stripped):
                violations.append(f"{wf_name}:{lineno}: {stripped!r}")
    assert not violations, (
        "Financial workflows must NOT git-add ledger files from public results/ — "
        "all ledger staging targets the private repo:\n"
        + "\n".join(f"  {v}" for v in violations)
    )
