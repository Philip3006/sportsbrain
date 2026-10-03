from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from scripts.tennis_slot_receipt import (
    claim_slot,
    complete_slot,
    derive_expected_slot,
    mark_pre_provider_failed,
    receipt_path,
)


def test_native_schedule_derives_nominal_slot_not_execution_time() -> None:
    now = datetime(2026, 10, 3, 10, 45, tzinfo=timezone.utc)
    assert derive_expected_slot("0 9 * * *", now) == "tennis-scan:2026-10-03T09:00Z"


def test_utc_rollover_derives_previous_day_slot() -> None:
    now = datetime(2026, 10, 4, 0, 20, tzinfo=timezone.utc)
    assert derive_expected_slot("0 23 * * *", now) == "tennis-scan:2026-10-03T23:00Z"


def test_unknown_cron_fails_closed() -> None:
    with pytest.raises(ValueError):
        derive_expected_slot("0 12 * * 1", datetime(2026, 10, 3, tzinfo=timezone.utc))


def test_claim_is_durable_and_duplicate_safe(tmp_path) -> None:
    slot = "tennis-scan:2026-10-03T12:00Z"
    first = claim_slot(
        slot,
        "watchdog_recovery",
        "run-1",
        now=datetime(2026, 10, 3, 12, 16, tzinfo=timezone.utc),
        root=tmp_path,
    )
    assert first["action"] == "run"
    receipt = json.loads(receipt_path(slot, tmp_path).read_text())
    assert receipt["status"] == "CLAIMED"
    assert receipt["provider_consuming_execution"] is False

    second = claim_slot(slot, "native_schedule", "run-2", root=tmp_path)
    assert second["action"] == "noop"


def test_completed_receipt_blocks_late_native_run(tmp_path) -> None:
    slot = "tennis-scan:2026-10-03T12:00Z"
    claim_slot(slot, "watchdog_recovery", "run-1", root=tmp_path)
    complete_slot(slot, root=tmp_path)
    result = claim_slot(slot, "native_schedule", "run-2", root=tmp_path)
    assert result["action"] == "noop"
    receipt = json.loads(receipt_path(slot, tmp_path).read_text())
    assert receipt["status"] == "COMPLETED"
    assert receipt["provider_consuming_execution"] is True


def test_pre_provider_failure_is_terminal_and_never_retried(tmp_path) -> None:
    slot = "tennis-scan:2026-10-03T12:00Z"
    claim_slot(slot, "watchdog_recovery", "run-1", root=tmp_path)
    mark_pre_provider_failed(slot, root=tmp_path)
    retry = claim_slot(slot, "watchdog_recovery", "run-2", root=tmp_path)
    assert retry["action"] == "noop"
    receipt = json.loads(receipt_path(slot, tmp_path).read_text())
    assert receipt["status"] == "PRE_PROVIDER_FAILED"


@pytest.mark.parametrize("status", ["CLAIMED", "COMPLETED"])
def test_authoritative_current_main_receipt_blocks_stale_local_claim(tmp_path, status) -> None:
    slot = "tennis-scan:2026-10-03T12:00Z"
    result = claim_slot(
        slot,
        "native_schedule",
        "run-stale",
        authoritative_receipt={"expected_slot": slot, "status": status},
        root=tmp_path,
    )
    assert result["action"] == "noop"
    assert not receipt_path(slot, tmp_path).exists()


def test_first_claim_runs_when_local_and_authoritative_receipts_are_absent(tmp_path) -> None:
    slot = "tennis-scan:2026-10-03T12:00Z"
    result = claim_slot(slot, "native_schedule", "run-first", root=tmp_path)
    assert result["action"] == "run"


def test_unrelated_authoritative_slot_does_not_block_claim(tmp_path) -> None:
    slot = "tennis-scan:2026-10-03T12:00Z"
    claim_slot("tennis-scan:2026-10-03T09:00Z", "native_schedule", "run-other", root=tmp_path)
    result = claim_slot(slot, "native_schedule", "run-first", root=tmp_path)
    assert result["action"] == "run"


def test_invalid_slot_is_rejected(tmp_path) -> None:
    with pytest.raises(ValueError):
        claim_slot("tennis-scan:2026-10-03T12:01Z", "native_schedule", "run", root=tmp_path)
