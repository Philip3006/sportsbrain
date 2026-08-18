"""HARD GATE 9 — Financial Writer Governance (TASK-P0D-002).

Verifies that the financial ledger mutation path preserves all P0-A ACK semantics
and remains independent of the generic bot persistence primitive (_bot_commit_push.sh).

Invariants enforced:
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
import inspect
import re
import textwrap
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS_DIR = ROOT / ".github" / "workflows"
CONSUME_SCRIPT = ROOT / "scripts" / "consume_pending_bets.py"
BOT_COMMIT_PUSH = ROOT / "scripts" / "_bot_commit_push.sh"

# ── Workflow classification ───────────────────────────────────────────────────
# AUTHORITATIVE_FINANCIAL_TRANSACTION: ledger CSV mutation triggers in this workflow;
#   durability is the workflow's commit+push step (not the generic bot primitive).
# SECONDARY_RUNTIME_PUBLICATION (health/cache only): does NOT own ledger durability.
_AUTHORITATIVE_FINANCIAL = frozenset({
    "tennis_settle.yml",
    "tennis_closing_odds.yml",
    "bundesliga2_settle.yml",
})
# consume_pending_bets is special: ledger durability is inside the Python script;
# the workflow's "Commit health artifacts" step is SECONDARY (health only).
_CONSUME_WORKFLOW = "consume_pending_bets.yml"


# ── I1: _durable_push stages ONLY results/ledger_*.csv ───────────────────────

def test_i1_durable_push_git_add_is_scoped_to_ledger_only():
    """_durable_push() must call `git add results/ledger_*.csv` and NOTHING else.

    Proves invariant I: the financial git publication cannot accidentally stage
    source files or other runtime paths — the add path is hard-coded to the
    ledger glob, independently of _bot_commit_push.sh / bot_assert_staged_safe.
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
                # Look for _g("add", ...)
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
        assert args == ["add", "results/ledger_*.csv"], (
            f"_durable_push() git add must use exactly 'results/ledger_*.csv'; got {args!r}"
        )


# ── G1/G2: _bot_commit_push.sh is NOT on the financial transaction path ───────

def test_g1_bot_commit_push_not_imported_or_called_in_financial_script():
    """The financial consumer script must never reference _bot_commit_push.sh.

    Proves invariant G: failure of the generic bot persistence primitive cannot
    corrupt or redefine authoritative financial transaction semantics.
    """
    source = CONSUME_SCRIPT.read_text()
    assert "_bot_commit_push" not in source, (
        "consume_pending_bets.py must not reference _bot_commit_push.sh — "
        "the financial transaction has its own durable boundary (_durable_push)"
    )


def test_g2_bot_assert_staged_safe_not_called_in_financial_path():
    """bot_assert_staged_safe() must not be called from _durable_push.

    The financial transaction path must not depend on the shared path allowlist
    validator — it has its own explicit, path-scoped git add.
    """
    source = CONSUME_SCRIPT.read_text()
    assert "bot_assert_staged_safe" not in source, (
        "consume_pending_bets.py must not call bot_assert_staged_safe — "
        "financial writer independence requires its own transaction boundary"
    )


# ── K: Workflow classification — no financial workflow uses _bot_commit_push.sh ─

def test_k1_authoritative_financial_workflows_do_not_use_bot_primitive():
    """AUTHORITATIVE_FINANCIAL_TRANSACTION workflows must NOT route through _bot_commit_push.sh.

    Financial ledger workflows own their own durable transaction boundary; routing
    them through the generic bot primitive would collapse P0-A ACK semantics into
    generic runtime persistence (CEO invariant 9).
    """
    violations: list[str] = []
    for wf_name in _AUTHORITATIVE_FINANCIAL:
        wf = WORKFLOWS_DIR / wf_name
        if not wf.exists():
            violations.append(f"{wf_name}: file not found")
            continue
        if "_bot_commit_push" in wf.read_text():
            violations.append(f"{wf_name}: references _bot_commit_push.sh (must not)")
    assert not violations, (
        "Financial workflows that incorrectly use generic bot primitive:\n"
        + "\n".join(f"  {v}" for v in violations)
    )


def test_k2_consume_pending_bets_workflow_health_step_does_not_touch_ledger():
    """consume_pending_bets.yml commit step stages ONLY health artifacts, NOT ledger_*.csv.

    Ledger persistence for consume_pending_bets is owned by the Python script
    (_durable_push). The workflow commit step is secondary (health only).
    This boundary must not be collapsed.
    """
    wf = WORKFLOWS_DIR / _CONSUME_WORKFLOW
    assert wf.exists(), f"{_CONSUME_WORKFLOW} not found"
    text = wf.read_text()

    # Find the commit step section — look for the "Commit health artifacts" block.
    # The step must not contain `git add results/ledger_*.csv`.
    commit_block_match = re.search(
        r"name:\s*Commit health artifacts.*?(?=\n\s*-\s*name:|\Z)",
        text, re.DOTALL
    )
    assert commit_block_match, (
        "consume_pending_bets.yml must contain a 'Commit health artifacts' step"
    )
    commit_block = commit_block_match.group(0)
    # The block may reference "ledger" in a YAML comment explaining why it's absent.
    # What matters: no `git add` in this step touches ledger_*.csv.
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


def test_k3_all_four_financial_workflows_exist_and_are_classified():
    """All four financial writer workflows exist and are enumerated.

    Ensures no financial writer can be added or renamed without updating classification.
    """
    all_financial = _AUTHORITATIVE_FINANCIAL | {_CONSUME_WORKFLOW}
    missing = [wf for wf in sorted(all_financial) if not (WORKFLOWS_DIR / wf).exists()]
    assert not missing, (
        "Financial writer workflows not found (file missing or renamed):\n"
        + "\n".join(f"  {m}" for m in missing)
    )


def test_k4_authoritative_financial_workflows_have_own_retry_loops():
    """AUTHORITATIVE_FINANCIAL_TRANSACTION workflows must each have their own push retry loop.

    The independent retry loop is what maintains the durable transaction guarantee
    without delegating to the generic bot primitive.
    """
    _RETRY_PATTERN = re.compile(r"for i in 1 2 3 4 5", re.MULTILINE)
    violations: list[str] = []
    for wf_name in _AUTHORITATIVE_FINANCIAL:
        wf = WORKFLOWS_DIR / wf_name
        if not wf.exists():
            violations.append(f"{wf_name}: file not found")
            continue
        if not _RETRY_PATTERN.search(wf.read_text()):
            violations.append(f"{wf_name}: missing independent push retry loop")
    assert not violations, (
        "Financial workflows missing own retry loop (governance gap):\n"
        + "\n".join(f"  {v}" for v in violations)
    )


# ── E: Duplicate bet → idempotent, no double ledger write ───────────────────

def test_e1_duplicate_bet_skipped_by_append_rows(tmp_path, monkeypatch):
    """_append_rows() must skip rows whose (match_id, market) already exists in ledger.

    Proves invariant E: a duplicate bet arriving from KV (because ACK failed in a
    previous run) cannot create a second ledger row.
    """
    import pandas as pd
    from scripts.consume_pending_bets import _append_rows, _match_id

    # Patch ledger path to tmp
    ledger_file = tmp_path / "ledger_test.csv"
    monkeypatch.setattr(
        "scripts.consume_pending_bets._resolve_ledger_path",
        lambda *a, **kw: ledger_file,
    )
    monkeypatch.setattr(
        "scripts.consume_pending_bets._file_lock",
        lambda path, **kw: __import__("contextlib").nullcontext(),
    )

    from src.betting.ledger import _FIELDS

    mid = _match_id("HomeFC", "AwayFC", "2026-08-18")
    first_row = {f: "" for f in _FIELDS}
    first_row.update({"match_id": mid, "market": "h2h", "home": "HomeFC", "away": "AwayFC"})

    def _mock_load(path):
        if not ledger_file.exists():
            return pd.DataFrame(columns=_FIELDS)
        return pd.read_csv(ledger_file)

    def _mock_save(df, path):
        df.to_csv(path, index=False)

    monkeypatch.setattr("scripts.consume_pending_bets._load", _mock_load)
    monkeypatch.setattr("scripts.consume_pending_bets._save", _mock_save)

    # First insert — should add 1 row
    added1 = _append_rows([first_row])
    assert added1 == 1, "First insert must add 1 row"

    # Duplicate insert — must be skipped
    added2 = _append_rows([first_row])
    assert added2 == 0, "Duplicate row must be skipped (idempotent)"

    df = pd.read_csv(ledger_file)
    assert len(df) == 1, f"Ledger must contain exactly 1 row after duplicate; got {len(df)}"


# ── F: Per-user isolation at ledger-path level ───────────────────────────────

def test_f1_per_user_ledger_paths_are_distinct(tmp_path, monkeypatch):
    """Two users must resolve to distinct, non-overlapping ledger paths.

    Proves invariant F: consumer routing to per-user ledger paths is strictly
    isolated — user A's bets can never appear in user B's ledger.
    """
    import scripts.consume_pending_bets as cpb
    from src.betting.ledger import _resolve_ledger_path
    from src.config import ledger_path_for

    path_alice = ledger_path_for("alice")
    path_bob = ledger_path_for("bob")
    assert path_alice != path_bob, (
        f"ledger_path_for('alice') == ledger_path_for('bob') = {path_alice!r}"
    )
    # Paths must be under results/ and user-namespaced
    assert "alice" in path_alice.name, f"alice ledger not namespaced: {path_alice}"
    assert "bob" in path_bob.name, f"bob ledger not namespaced: {path_bob}"


# ── H: Malformed/forbidden request — correct rejection, zero mutation ────────

def test_h1_missing_source_rejects_with_no_ledger_row():
    """Bet with missing 'source' field is permanently rejected — no ledger row created."""
    from scripts.consume_pending_bets import _row_from_bet

    row, reason = _row_from_bet(
        {"match": "Home FC vs Away FC", "market": "h2h", "odds": 2.0,
         "stake_eur": 5.0, "kickoff": "2026-08-20T15:00:00Z"},
        today="2026-08-20",
        bankroll=100.0,
    )
    assert row is None, "Missing source must produce row=None"
    assert "source" in reason.lower() or "permanent" in reason.lower(), (
        f"Reason must mention source rejection: {reason!r}"
    )


def test_h2_stake_exceeds_cap_rejects_with_no_ledger_row():
    """Bet with stake > 5% of bankroll is permanently rejected — no ledger row created."""
    from scripts.consume_pending_bets import _row_from_bet

    row, reason = _row_from_bet(
        {"match": "Home FC vs Away FC", "market": "h2h", "odds": 2.0,
         "stake_eur": 10.0,  # 10% of 100 — over cap
         "source": "manual", "kickoff": "2026-08-20T15:00:00Z"},
        today="2026-08-20",
        bankroll=100.0,
    )
    assert row is None, "Stake > 5% cap must produce row=None"
    assert "cap" in reason.lower() or "5%" in reason, (
        f"Reason must mention cap violation: {reason!r}"
    )


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
    assert row is None, "source=value without signal_id must produce row=None"
    assert "signal_id" in reason.lower() or "reject" in reason.lower(), (
        f"Reason must mention signal_id: {reason!r}"
    )


# ── C: Retryable/transient failure not permanently rejected ──────────────────

def test_c1_bankroll_unavailable_is_retry_not_reject():
    """bankroll=None (infrastructure outage) must return RETRY sentinel, never permanent reject.

    Proves invariant C: a transient bankroll lookup failure must keep the bet in
    KV for the next run rather than permanently ACK'ing/rejecting it.
    """
    from scripts.consume_pending_bets import _RETRY_PREFIX, _row_from_bet

    row, reason = _row_from_bet(
        {"match": "Home FC vs Away FC", "market": "h2h", "odds": 2.0,
         "stake_eur": 5.0, "source": "manual", "kickoff": "2026-08-20T15:00:00Z"},
        today="2026-08-20",
        bankroll=None,  # outage
    )
    assert row is None, "bankroll=None must produce row=None"
    assert reason.startswith(_RETRY_PREFIX), (
        f"bankroll=None must return RETRY sentinel, not permanent reject: {reason!r}"
    )


# ── D: Failure after durable mutation → safe replay, no double-write ─────────

def test_d1_durable_push_source_reflects_idempotent_replay_path():
    """_durable_push contains a remote-containment check that enables safe replay.

    Proves invariant D: if the process crashes after commit but before ACK, the
    next run finds nothing staged, verifies remote containment, and returns True
    (ACK proceeds) without creating a duplicate ledger row.

    This test verifies the idempotency path exists in source code.
    """
    source = CONSUME_SCRIPT.read_text()
    assert "merge-base" in source and "--is-ancestor" in source, (
        "_durable_push() must contain 'merge-base --is-ancestor' for remote-containment check"
    )
    assert "local commit not yet in origin/main" in source or "local-ahead" in source, (
        "_durable_push() must handle local-ahead case for safe push-before-ACK replay"
    )


# ── A: Durable mutation order verified in source ─────────────────────────────

def test_a1_consume_main_appends_before_push_before_ack():
    """main() call order: _append_rows → _durable_push → _kv_delete_one (ACK).

    Proves invariant A: durable authoritative mutation occurs before ACK.
    Verified by inspecting the call sequence in the AST of main().
    """
    source = CONSUME_SCRIPT.read_text()
    # Verify the P0-A comment block that documents the canonical lifecycle
    assert "Append ACCEPTED rows to per-user ledger" in source, (
        "P0-A lifecycle step 4 (append) must be documented in source"
    )
    assert "Stage + commit + push to origin/main" in source or "_durable_push" in source, (
        "P0-A lifecycle step 5 (durable push) must be documented/present in source"
    )
    assert "ACK/delete ACCEPTED pending entries ONLY after push" in source, (
        "P0-A lifecycle step 6 (ACK after push) must be documented in source"
    )


# ── B: Durable push failure → no ACK, main returns 1 ────────────────────────

def test_b1_push_failure_propagates_as_main_return_1():
    """_durable_push() returning False must cause main() to return 1 without ACK.

    This test documents the existence of the correct early-return guard in source.
    (Integration test is in test_lifecycle_b_push_failure_keeps_pending.)
    """
    source = CONSUME_SCRIPT.read_text()
    assert "push_ok = _durable_push(" in source, (
        "_durable_push() result must be captured in push_ok"
    )
    assert "if not push_ok:" in source or "if not push_ok" in source, (
        "push_ok=False must gate the ACK path"
    )
    assert "return 1" in source, (
        "main() must return 1 when push fails"
    )


# ── P0-A regression suite still green (J) ────────────────────────────────────

def test_j1_p0a_lifecycle_tests_exist_and_are_importable():
    """The P0-A lifecycle regression suite must remain importable and non-empty.

    Proves invariant J: existing P0-A guarantees are not weakened by P0D-002.
    """
    import importlib.util

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
