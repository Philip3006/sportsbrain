from __future__ import annotations

from pathlib import Path

from monitoring.top5.runtime_health import (
    BLOCKED_STATUS,
    READY_STATUS,
    Top5RuntimeEvidence,
    assess_top5_runtime_health,
    run_offline_runtime_health,
)


def _evidence(**overrides: object) -> Top5RuntimeEvidence:
    values: dict[str, object] = {
        "worker_health": "ok",
        "pwa_health": "ok",
        "football_scheduler_state": "inactive",
        "launchd_expectations": {
            "com.sportsbrain.aggregate-health": "active",
            "top5_runtime_scheduler": "not_registered",
        },
        "runtime_writer_state": {"health_writer": "active"},
        "ledger_writer_state": {"top5_ledger_writer": "not_registered"},
        "football_health_artifacts": {"aggregate_health_snapshot": "present"},
        "request_interfaces": {"top5_bulk_request": "present"},
        "quota_interfaces": {"top5_quota_contract": "present"},
    }
    values.update(overrides)
    return Top5RuntimeEvidence.from_mapping(values)


def test_clean_offline_inventory_is_finally_ready() -> None:
    root = Path(__file__).resolve().parents[3]
    report = run_offline_runtime_health(root)

    assert report.status == READY_STATUS
    assert report.ready is True
    assert report.offline_only is True
    assert report.blockers == ()


def test_runtime_dirtiness_is_bounded_and_informational() -> None:
    report = assess_top5_runtime_health(
        _evidence(runtime_dirtiness=("results/health/top5-runtime.json",))
    )

    assert report.status == READY_STATUS
    assert report.warnings == ("runtime dirtiness is informational (1 bounded item(s))",)


def test_unexpected_source_dirtiness_fails_closed() -> None:
    report = assess_top5_runtime_health(
        _evidence(source_dirtiness=("src/football/top5_runtime.py",))
    )

    assert report.status == BLOCKED_STATUS
    assert "unexpected source dirtiness is present" in report.blockers
    assert report.checks["source_clean"] is False


def test_unsafe_ledger_or_runtime_writer_state_fails_closed() -> None:
    report = assess_top5_runtime_health(
        _evidence(
            runtime_writer_state={"health_writer": "paused"},
            ledger_writer_state={"top5_ledger_writer": "active"},
        )
    )

    assert report.status == BLOCKED_STATUS
    assert report.checks["runtime_writers_active"] is False
    assert report.checks["ledger_writers_safe"] is False


def test_any_production_mutation_fails_closed() -> None:
    report = assess_top5_runtime_health(
        _evidence(production_mutations={"publication": 1})
    )

    assert report.status == BLOCKED_STATUS
    assert report.checks["production_untouched"] is False
    assert any("publication=1" in blocker for blocker in report.blockers)


def test_malformed_or_unbounded_mapping_is_blocked() -> None:
    report = assess_top5_runtime_health(
        {
            "runtime_writer_state": {str(index): "active" for index in range(65)},
        }
    )

    assert report.status == BLOCKED_STATUS
    assert report.checks == {"evidence_shape": False}
