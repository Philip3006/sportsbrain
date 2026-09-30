"""The active manual runner must bind only to the v1.1 contract."""

from scripts import nations_league_v1_1_forward_shadow as runner


def test_successor_runner_imports_only_v11_contract():
    assert runner.build_forward_shadow_prediction.__module__ == (
        "src.analysis.nations_league_v1_1"
    )
    assert runner.build_shadow_settlement.__module__ == (
        "src.analysis.nations_league_v1_1"
    )
    assert runner.calculate_forward_metrics.__module__ == (
        "src.analysis.nations_league_v1_1"
    )
