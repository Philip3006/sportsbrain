from pathlib import Path

WORKFLOW = Path(__file__).parents[1] / ".github" / "workflows" / "tennis_scan.yml"


def test_workflow_keeps_manual_dispatch_separate_from_slot_evidence() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    assert 'if [ "$EVENT_NAME" = "schedule" ]; then' in text
    assert 'TRIGGER_TYPE="native_schedule"' in text
    assert 'TRIGGER_TYPE="watchdog_recovery"' in text
    assert 'trigger_type=operator_manual' in text
    assert "watchdog recovery requires expected_slot" in text
    assert "steps.slot.outputs.trigger_type != 'operator_manual'" in text
    assert "all_live" in text


def test_workflow_checks_authoritative_main_receipt_before_claim() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "contents/${RECEIPT_API_PATH}?ref=main" in text
    assert "--authoritative-receipt-json" in text
    assert "authoritative receipt binding invalid" in text
