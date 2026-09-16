from src.nightshift.acceptance import run_fake_acceptance


def test_fake_executor_acceptance_scenario_passes() -> None:
    result = run_fake_acceptance()
    assert result["status"] == "PASS"
    assert all(result["steps"].values())
