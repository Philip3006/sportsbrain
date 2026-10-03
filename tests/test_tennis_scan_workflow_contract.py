import json
import re
import subprocess
import sys
from pathlib import Path

WORKFLOW = Path(__file__).parents[1] / ".github" / "workflows" / "tennis_scan.yml"
RECEIPT_CLI = Path(__file__).parents[1] / "scripts" / "tennis_slot_receipt.py"


def test_workflow_keeps_manual_dispatch_separate_from_slot_evidence() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    assert 'if [ "$EVENT_NAME" = "schedule" ]; then' in text
    assert 'TRIGGER_TYPE="native_schedule"' in text
    assert 'TRIGGER_TYPE="watchdog_recovery"' in text
    assert "trigger_type=operator_manual" in text
    assert "watchdog recovery requires expected_slot" in text
    assert "steps.slot.outputs.trigger_type != 'operator_manual'" in text
    assert "all_live" in text


def test_workflow_checks_authoritative_main_receipt_before_claim() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "contents/${RECEIPT_API_PATH}?ref=main" in text
    assert "--authoritative-receipt-json" in text
    assert "authoritative receipt binding invalid" in text


def test_workflow_invokes_exactly_one_parseable_claim_subcommand(
    tmp_path: Path,
) -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    claim_args = text.split("CLAIM_ARGS=(", 1)[1].split(")", 1)[0]
    arg_lines = [line.strip() for line in claim_args.splitlines() if line.strip()]

    assert "claim" not in arg_lines
    assert (
        len(
            re.findall(
                r"(?m)^\s*python scripts/tennis_slot_receipt\.py\s+claim\b", text
            )
        )
        == 1
    )

    slot = "tennis-scan:2026-10-03T12:00Z"
    authoritative_receipt = tmp_path / "authoritative-receipt.json"
    authoritative_receipt.write_text(
        json.dumps({"expected_slot": slot, "status": "COMPLETED"}),
        encoding="utf-8",
    )
    for trigger_type in ("native_schedule", "watchdog_recovery"):
        completed = subprocess.run(
            [
                sys.executable,
                str(RECEIPT_CLI),
                "claim",
                "--expected-slot",
                slot,
                "--trigger-type",
                trigger_type,
                "--run-id",
                "workflow-contract-test",
                "--authoritative-receipt-json",
                str(authoritative_receipt),
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        assert completed.returncode == 0, completed.stderr
        assert (
            json.loads(completed.stdout)["reason"]
            == "authoritative_slot_receipt_exists"
        )


def test_recovery_missing_slot_fails_before_claim_and_manual_path_bypasses_it() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    recovery_guard = text.index('echo "watchdog recovery requires expected_slot"')
    claim_call = text.index("python scripts/tennis_slot_receipt.py claim")
    manual_exit = text.index('echo "expected_slot=" >> "$GITHUB_OUTPUT"')
    receipt_lookup = text.index("AUTH_RECEIPT_URL=")

    assert recovery_guard < claim_call
    assert manual_exit < receipt_lookup < claim_call
    assert 'echo "trigger_type=operator_manual" >> "$GITHUB_OUTPUT"' in text
    assert 'ALL_LIVE_FLAG="--all-live"' in text
