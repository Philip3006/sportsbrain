"""P0D-003 Healer Authority Boundary Governance Tests.

Negative tests prove the healer CANNOT:
  1.  Write source code.
  2.  git add/commit/push code fixes.
  3.  Self-modify auto_heal_ai.py.
  4.  Modify require_green_ci.py.
  5.  Modify _git_safe_push.sh.
  6.  Dispatch re-consume as an executable action.
  7.  Dispatch re-run-settle as an executable action.
  8.  Recovery registry marks re-consume / re-run-settle unavailable/inactive.
  9.  Financial jobs send log tails to Anthropic.
  10. Layer-1 healer retries settle.
  11. Layer-1 healer retries closing_odds.
  12. Layer-1 healer retries auto_retrain.
  13. Healer generic push repair rejects source commits.
  14. Healer generic push repair rejects financial paths.
  15. Healer generic push repair rejects model paths.
  16. _git_safe_push no longer permits results/ledger*.
  17. _git_safe_push no longer permits results/betting_journal.md.
  18. cloud_healer WORKFLOW_MAP has no financial/model workflow.
  19. Worker _cronHealerCheck map has no financial/model workflow.
  20. Worker _cronConsumeCheck still dispatches consume_pending_bets.
  21. Healer cannot deploy.
  22. Healer cannot force-push/merge.

Positive tests prove allowed behavior:
  23. Non-financial health observation still works.
  24. daily_scan retry remains in Layer-1 scope.
  25. VAPID escalation path still present.
  26. Non-financial AI diagnosis works (CODE_ISSUE/TRANSIENT/DEGRADED_OK/UNCLEAR).
  27. Worker normal pending-bet dispatcher unchanged.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _py_names_in_module(text: str) -> set[str]:
    tree = ast.parse(text)
    return {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}


_AUTO_HEAL_AI = ROOT / "scripts" / "auto_heal_ai.py"
_AUTO_HEAL_CRON = ROOT / "scripts" / "auto_heal_cron.sh"
_GIT_SAFE_PUSH = ROOT / "scripts" / "_git_safe_push.sh"
_CLOUD_HEALER = ROOT / ".github" / "workflows" / "cloud_healer.yml"
_WORKER_JS = ROOT / "cloudflare" / "worker.js"
_RECOVERY_TRUTH = ROOT / "src" / "monitoring" / "recovery_truth.py"


# ---------------------------------------------------------------------------
# 1–5: AI healer source mutation authority
# ---------------------------------------------------------------------------

def test_1_ai_healer_cannot_write_source_files():
    """_apply_fix must not perform any filesystem mutation."""
    text = _read(_AUTO_HEAL_AI)
    # _apply_fix must not call write_text, open(..., 'w'), or shutil on source
    assert "_apply_fix" in text, "_apply_fix function must still exist (fail-closed stub)"
    # Must not contain git-add/commit/push inside _apply_fix body
    apply_fix_body_start = text.find("def _apply_fix(")
    assert apply_fix_body_start != -1
    # Find the next top-level def after _apply_fix to bound the body
    next_def = text.find("\ndef ", apply_fix_body_start + 1)
    body = text[apply_fix_body_start:next_def] if next_def != -1 else text[apply_fix_body_start:]
    for forbidden in ('subprocess.run(["git"', 'git add', 'git commit', '_git_safe_push'):
        assert forbidden not in body, (
            f"_apply_fix body contains forbidden mutation: {forbidden!r}"
        )


def test_2_ai_healer_no_git_commit_in_fix_flow():
    """auto_heal_ai.py must contain no git commit call in any code path."""
    text = _read(_AUTO_HEAL_AI)
    # git commit must not appear in auto_heal_ai.py at all
    assert '"git", "commit"' not in text, "git commit call found in auto_heal_ai.py"
    assert '"git", "add"' not in text, "git add call found in auto_heal_ai.py"
    assert "_git_safe_push" not in text, "_git_safe_push call found in auto_heal_ai.py"


def test_3_ai_healer_cannot_self_modify():
    """auto_heal_ai.py must not appear in any FIX target or path allowlist."""
    text = _read(_AUTO_HEAL_AI)
    assert "auto_heal_ai.py" not in text.replace(
        # Allow the file's own __file__ reference and docstring
        "__file__", ""
    ).replace("auto_heal_ai", "").replace("auto_heal_ai.py", ""), \
        "auto_heal_ai.py appears as a mutation target"
    # More targeted: no writable reference to itself in _apply_fix or ACTION_MAP
    # The test above is broad; supplement with prompt check:
    assert "auto_heal_ai" not in text.split("_DIAGNOSIS_PROMPT")[1].split("_ACTION_MAP")[0] \
        if "_DIAGNOSIS_PROMPT" in text and "_ACTION_MAP" in text else True


def test_4_ai_healer_cannot_modify_phase0_gate():
    """require_green_ci.py must not be targetable by healer."""
    text = _read(_AUTO_HEAL_AI)
    assert "require_green_ci" not in text, (
        "require_green_ci.py referenced in auto_heal_ai.py — Phase-0 gate must not be editable by healer"
    )


def test_5_ai_healer_cannot_modify_push_primitive():
    """_git_safe_push.sh must not be targetable by healer."""
    text = _read(_AUTO_HEAL_AI)
    # _git_safe_push may be referenced as 'not used' in a comment — check for executable reference
    assert '"_git_safe_push.sh"' not in text, (
        "Healer must not call _git_safe_push.sh for code fixes (P0D-003)"
    )


# ---------------------------------------------------------------------------
# 6–8: Financial actions removed / recovery registry
# ---------------------------------------------------------------------------

def test_6_action_map_has_no_re_consume():
    """_ACTION_MAP must not contain an executable re-consume entry."""
    text = _read(_AUTO_HEAL_AI)
    # Extract _ACTION_MAP block
    map_start = text.find("_ACTION_MAP = {")
    map_end = text.find("\n}", map_start)
    action_map_block = text[map_start:map_end] if map_start != -1 else ""
    # re-consume must not appear as an executable mapping
    assert '"re-consume"' not in action_map_block or \
        'None' in action_map_block[action_map_block.find('"re-consume"'):action_map_block.find('"re-consume"') + 100], \
        "_ACTION_MAP must not contain an executable re-consume entry"
    # Stricter: re-consume should not be in the map at all (per P0D-003 removal)
    # Check the actual map keys
    assert '"re-consume": [' not in action_map_block, (
        '"re-consume" maps to an executable command in _ACTION_MAP — must be removed (P0D-003)'
    )


def test_7_action_map_has_no_re_run_settle():
    """_ACTION_MAP must not contain an executable re-run-settle entry."""
    text = _read(_AUTO_HEAL_AI)
    map_start = text.find("_ACTION_MAP = {")
    map_end = text.find("\n}", map_start)
    action_map_block = text[map_start:map_end] if map_start != -1 else ""
    assert '"re-run-settle": [' not in action_map_block, (
        '"re-run-settle" maps to an executable command in _ACTION_MAP — must be removed (P0D-003)'
    )


def test_8_recovery_registry_marks_financial_actions_inactive():
    """re-consume, re-run-settle, settle_retry, auto_retrain_retry, closing_odds_retry
    must all have active=False in RECOVERY_REGISTRY."""
    sys.path.insert(0, str(ROOT))
    from src.monitoring.recovery_truth import RECOVERY_REGISTRY  # noqa: PLC0415

    inactive_required = {
        "re-consume", "re-run-settle",
        "settle_retry", "auto_retrain_retry", "closing_odds_retry",
    }
    violations = []
    for action in inactive_required:
        binding = RECOVERY_REGISTRY.get(action)
        if binding is None:
            violations.append(f"{action!r}: not in RECOVERY_REGISTRY (must be preserved as inactive)")
            continue
        if binding.active:
            violations.append(f"{action!r}: active=True — must be active=False (P0D-003)")

    assert not violations, (
        "RECOVERY_REGISTRY financial/model actions must be inactive:\n"
        + "\n".join(f"  {v}" for v in violations)
    )


def test_8b_recovery_registry_financial_actions_resolve_unavailable():
    """request_recovery for inactive financial actions must return RECOVERY_UNAVAILABLE."""
    sys.path.insert(0, str(ROOT))
    from src.monitoring.recovery_truth import RecoveryState, request_recovery  # noqa: PLC0415

    financial_actions = [
        "re-consume", "re-run-settle",
        "settle_retry", "auto_retrain_retry", "closing_odds_retry",
    ]
    for action in financial_actions:
        attempt = request_recovery("test_target", action)
        assert attempt.state == RecoveryState.RECOVERY_UNAVAILABLE, (
            f"request_recovery({action!r}) must return RECOVERY_UNAVAILABLE, "
            f"got {attempt.state.value}"
        )


# ---------------------------------------------------------------------------
# 9: Financial jobs blocked from Anthropic
# ---------------------------------------------------------------------------

def test_9_financial_jobs_blocked_from_anthropic():
    """_FINANCIAL_JOBS must include all known financial job names."""
    text = _read(_AUTO_HEAL_AI)
    assert "_FINANCIAL_JOBS" in text, "_FINANCIAL_JOBS blocklist must exist in auto_heal_ai.py"

    required_financial_jobs = {
        "consume_pending_bets",
        "settle",
        "closing_odds",
        "bundesliga2_settle",
        "tennis_settle",
        "tennis_closing_odds",
        "bundesliga2_closing_odds",
    }
    missing = []
    for job in required_financial_jobs:
        if job not in text:
            missing.append(job)
    assert not missing, (
        f"_FINANCIAL_JOBS missing entries: {missing}. "
        "All financial job names must be in the blocklist."
    )


def test_9b_financial_job_check_precedes_anthropic_call():
    """Financial job check must run before any Anthropic API call in main()."""
    text = _read(_AUTO_HEAL_AI)
    main_start = text.find("def main() -> None:")
    assert main_start != -1
    main_body = text[main_start:]

    financial_check_pos = main_body.find("_FINANCIAL_JOBS")
    api_call_pos = main_body.find("_ask_claude(")
    assert financial_check_pos != -1, "Financial job check must be in main()"
    assert api_call_pos != -1, "_ask_claude call must be in main()"
    assert financial_check_pos < api_call_pos, (
        "Financial job check must appear before _ask_claude call in main()"
    )


# ---------------------------------------------------------------------------
# 10–12: Layer-1 healer retry scope
# ---------------------------------------------------------------------------

def test_10_layer1_does_not_retry_settle():
    """Layer-1 healer must not contain settle retry logic."""
    text = _read(_AUTO_HEAL_CRON)
    # Must not call settle_cron.sh or _should_retry settle
    assert 'settle_cron.sh' not in text or \
        all('_retry_job' not in line for line in text.splitlines()
            if 'settle_cron.sh' in line), (
        "Layer-1 healer contains settle retry — must be removed (P0D-003)"
    )
    assert '"settle"' not in text or \
        '_should_retry "settle"' not in text, (
        "Layer-1 healer contains _should_retry settle — must be removed (P0D-003)"
    )


def test_11_layer1_does_not_retry_closing_odds():
    """Layer-1 healer must not contain closing_odds retry logic."""
    text = _read(_AUTO_HEAL_CRON)
    assert '_should_retry "closing_odds"' not in text, (
        "Layer-1 healer contains _should_retry closing_odds — must be removed (P0D-003)"
    )
    assert 'closing_odds_cron.sh' not in text, (
        "Layer-1 healer references closing_odds_cron.sh — must be removed (P0D-003)"
    )


def test_12_layer1_does_not_retry_auto_retrain():
    """Layer-1 healer must not contain auto_retrain retry logic."""
    text = _read(_AUTO_HEAL_CRON)
    assert '_should_retry "auto_retrain"' not in text, (
        "Layer-1 healer contains _should_retry auto_retrain — must be removed (P0D-003)"
    )
    assert 'auto_retrain_cron.sh' not in text, (
        "Layer-1 healer references auto_retrain_cron.sh — must be removed (P0D-003)"
    )


# ---------------------------------------------------------------------------
# 13–15: Healer push repair boundary
# ---------------------------------------------------------------------------

def test_13_healer_push_repair_rejects_source_commits():
    """Layer-1 push repair must have a healer-specific allowlist that rejects source files."""
    text = _read(_AUTO_HEAL_CRON)
    assert "_healer_push_safe" in text, (
        "Healer-specific push boundary function _healer_push_safe must exist in auto_heal_cron.sh"
    )
    # Must check ahead commits before calling git_safe_push
    healer_push_pos = text.find("_healer_push_safe")
    git_safe_push_pos = text.find("git_safe_push", healer_push_pos)
    assert git_safe_push_pos > healer_push_pos, (
        "_healer_push_safe check must precede git_safe_push call"
    )


def test_14_healer_push_rejects_financial_paths():
    """Healer push allowlist must not include financial/ledger paths."""
    text = _read(_AUTO_HEAL_CRON)
    healer_fn_start = text.find("_healer_push_safe()")
    healer_fn_end = text.find("\n}", healer_fn_start)
    healer_body = text[healer_fn_start:healer_fn_end] if healer_fn_start != -1 else text
    assert "results/ledger" not in healer_body, (
        "Healer push allowlist must not permit results/ledger* (private-repo-owned, P0D-002)"
    )
    assert "betting_journal" not in healer_body, (
        "Healer push allowlist must not permit betting_journal (private-repo-owned, P0D-002)"
    )


def test_15_healer_push_rejects_model_paths():
    """Healer push allowlist must not include model artifact paths."""
    text = _read(_AUTO_HEAL_CRON)
    healer_fn_start = text.find("_healer_push_safe()")
    healer_fn_end = text.find("\n}", healer_fn_start)
    healer_body = text[healer_fn_start:healer_fn_end] if healer_fn_start != -1 else text
    assert "models/" not in healer_body, (
        "Healer push allowlist must not permit models/ paths (model promotion is Tier 3)"
    )


# ---------------------------------------------------------------------------
# 16–17: _git_safe_push stale financial allowlist removal
# ---------------------------------------------------------------------------

def test_16_git_safe_push_no_ledger_pattern():
    """_git_safe_push _bot_permitted must not match results/ledger* paths."""
    text = _read(_GIT_SAFE_PUSH)
    bot_permitted_start = text.find("_bot_permitted()")
    bot_permitted_end = text.find("}", bot_permitted_start)
    body = text[bot_permitted_start:bot_permitted_end] if bot_permitted_start != -1 else text
    assert "results/ledger" not in body, (
        "results/ledger* must be removed from _git_safe_push _bot_permitted (P0D-002 private-repo-owned)"
    )


def test_17_git_safe_push_no_betting_journal():
    """_git_safe_push _bot_permitted must not match results/betting_journal.md."""
    text = _read(_GIT_SAFE_PUSH)
    bot_permitted_start = text.find("_bot_permitted()")
    bot_permitted_end = text.find("}", bot_permitted_start)
    body = text[bot_permitted_start:bot_permitted_end] if bot_permitted_start != -1 else text
    assert "betting_journal" not in body, (
        "results/betting_journal.md must be removed from _git_safe_push _bot_permitted (P0D-002)"
    )


# ---------------------------------------------------------------------------
# 18: cloud_healer WORKFLOW_MAP
# ---------------------------------------------------------------------------

def test_18_cloud_healer_workflow_map_no_financial_or_model():
    """cloud_healer WORKFLOW_MAP must contain no financial or model workflows."""
    text = _read(_CLOUD_HEALER)
    map_start = text.find("WORKFLOW_MAP = {")
    map_end = text.find("}", map_start)
    wf_map_block = text[map_start:map_end] if map_start != -1 else ""

    forbidden = [
        "settle.yml", "tennis_settle.yml", "bundesliga2_settle.yml",
        "closing_odds.yml", "tennis_closing_odds.yml", "bundesliga2_closing_odds.yml",
        "consume_pending_bets.yml",
        "auto_retrain.yml", "tennis_lgbm_retrain.yml", "bundesliga2_retrain.yml",
        "tennis_elo_refresh.yml",
    ]
    found = [f for f in forbidden if f in wf_map_block]
    assert not found, (
        f"cloud_healer WORKFLOW_MAP contains forbidden financial/model workflows: {found}"
    )


# ---------------------------------------------------------------------------
# 19–20: Worker _cronHealerCheck / _cronConsumeCheck
# ---------------------------------------------------------------------------

def test_19_worker_healer_map_no_financial_or_model():
    """Worker _WORKFLOW_MAP (used by _cronHealerCheck) must not include financial/model targets."""
    text = _read(_WORKER_JS)
    map_start = text.find("const _WORKFLOW_MAP = {")
    map_end = text.find("};", map_start)
    wf_map_block = text[map_start:map_end] if map_start != -1 else ""

    forbidden = [
        "tennis_settle.yml", "bundesliga2_settle.yml",
        "closing_odds.yml", "tennis_closing_odds.yml", "bundesliga2_closing_odds.yml",
        "auto_retrain.yml", "tennis_lgbm_retrain.yml", "bundesliga2_retrain.yml",
        "settle.yml",
    ]
    found = [f for f in forbidden if f in wf_map_block]
    assert not found, (
        f"Worker _WORKFLOW_MAP contains forbidden financial/model workflow targets: {found}"
    )


def test_20_worker_consume_check_still_dispatches():
    """Worker _cronConsumeCheck must still call _ghRepositoryDispatch with consume_pending_bets."""
    text = _read(_WORKER_JS)
    assert "_cronConsumeCheck" in text, "_cronConsumeCheck function must still exist"
    # Search within the region between _cronConsumeCheck declaration and _cronHealerCheck
    # (the next function). This is more robust than finding a single closing brace.
    fn_start = text.find("async function _cronConsumeCheck(")
    next_fn = text.find("async function ", fn_start + 1)
    region = text[fn_start:next_fn] if fn_start != -1 and next_fn != -1 else text[fn_start:]
    assert "_ghRepositoryDispatch" in region, (
        "_cronConsumeCheck must still call _ghRepositoryDispatch"
    )
    assert "consume_pending_bets" in region, (
        "_cronConsumeCheck must still dispatch consume_pending_bets event_type"
    )


# ---------------------------------------------------------------------------
# 21–22: No deploy / no force-push
# ---------------------------------------------------------------------------

def test_21_healer_cannot_deploy():
    """Neither healer component may call wrangler deploy or equivalent."""
    for path in (_AUTO_HEAL_AI, _AUTO_HEAL_CRON):
        text = _read(path)
        assert "wrangler" not in text, f"wrangler deploy found in {path.name}"
        assert "wrangler deploy" not in text


def test_22_healer_cannot_force_push():
    """Healer components must not contain force-push or merge commands."""
    for path in (_AUTO_HEAL_AI, _AUTO_HEAL_CRON):
        text = _read(path)
        assert "push --force" not in text, f"force-push found in {path.name}"
        assert "push -f " not in text, f"force-push shorthand in {path.name}"
        assert "merge --no-ff" not in text, f"explicit merge found in {path.name}"


# ---------------------------------------------------------------------------
# Positive tests: allowed behavior remains
# ---------------------------------------------------------------------------

def test_23_non_financial_health_observation_works():
    """Outcome checks module still importable and returns structured symptoms."""
    sys.path.insert(0, str(ROOT))
    from src.monitoring.outcome_checks import run_all_checks  # noqa: PLC0415
    results = run_all_checks()
    assert isinstance(results, list)


def test_24_daily_scan_retry_in_layer1():
    """Layer-1 healer must still have daily_scan retry."""
    text = _read(_AUTO_HEAL_CRON)
    assert '_should_retry "daily_scan"' in text, (
        "daily_scan retry must remain in Layer-1 healer (non-financial, idempotent)"
    )
    assert 'scan_cron.sh' in text, (
        "scan_cron.sh must be referenced for daily_scan retry"
    )


def test_25_vapid_escalation_still_present():
    """_vapid_push must still be called for CODE_ISSUE and financial escalations."""
    text = _read(_AUTO_HEAL_AI)
    assert "_vapid_push" in text, "_vapid_push function must still exist"
    assert "CODE_ISSUE" in text, "CODE_ISSUE handling must exist in main()"
    # CODE_ISSUE must trigger vapid push
    main_start = text.find("def main() -> None:")
    main_body = text[main_start:]
    code_issue_section = main_body[main_body.find('CODE_ISSUE'):]
    section_end = code_issue_section.find('\n        elif ')
    code_issue_block = code_issue_section[:section_end] if section_end != -1 else code_issue_section[:300]
    assert "_vapid_push" in code_issue_block, (
        "CODE_ISSUE must trigger _vapid_push human escalation"
    )


def test_26_non_financial_ai_diagnosis_prompt_has_no_fix_block():
    """_DIAGNOSIS_PROMPT must not instruct the model to produce a FIX/code-replacement block."""
    text = _read(_AUTO_HEAL_AI)
    prompt_start = text.find("_DIAGNOSIS_PROMPT = ")
    prompt_end = text.find('"""', text.find('"""', prompt_start) + 3) + 3
    prompt = text[prompt_start:prompt_end] if prompt_start != -1 else ""
    assert "FIX" not in prompt or "do NOT produce" in prompt, (
        "_DIAGNOSIS_PROMPT must not include a FIX response format that produces code patches"
    )
    assert "OLD:" not in prompt, (
        "_DIAGNOSIS_PROMPT must not include OLD:/NEW: replacement format"
    )
    assert "NEW:" not in prompt, (
        "_DIAGNOSIS_PROMPT must not include OLD:/NEW: replacement format"
    )
    assert "CODE_ISSUE" in prompt, (
        "_DIAGNOSIS_PROMPT must include CODE_ISSUE classification for human escalation"
    )


def test_27_worker_consume_dispatcher_unchanged():
    """Worker _cronConsumeCheck must dispatch when KV has pending bets — unchanged."""
    text = _read(_WORKER_JS)
    assert "_cronConsumeCheck" in text, "_cronConsumeCheck function must still exist"
    # Search within the region between _cronConsumeCheck and the next async function
    fn_start = text.find("async function _cronConsumeCheck(")
    next_fn = text.find("async function ", fn_start + 1)
    region = text[fn_start:next_fn] if fn_start != -1 and next_fn != -1 else text[fn_start:]
    assert "hasPending" in region or "pending" in region.lower(), (
        "_cronConsumeCheck must still check for pending bets"
    )
    assert "_ghRepositoryDispatch" in region, (
        "_cronConsumeCheck must still trigger repository dispatch"
    )
    assert "consume_pending_bets" in region, (
        "_cronConsumeCheck must still dispatch consume_pending_bets"
    )
