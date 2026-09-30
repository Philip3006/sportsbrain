"""The active manual runner must bind only to the v1.1 contract."""

from scripts import nations_league_v1_1_forward_shadow as runner


def test_successor_runner_imports_only_v11_contract():
    assert runner.predict_from_input_state.__module__ == (
        "src.analysis.nations_league_forward_input"
    )
    assert runner.build_shadow_settlement.__module__ == (
        "src.analysis.nations_league_v1_1"
    )
    assert runner.calculate_forward_metrics.__module__ == (
        "src.analysis.nations_league_v1_1"
    )


def test_raw_prediction_inputs_are_not_cli_options():
    parser = runner._parser()
    try:
        parser.parse_args(
            [
                "predict",
                "--training",
                "training.json",
                "--input-provenance",
                "proof.json",
                "--phase",
                "initial",
                "--store",
                "store.jsonl",
            ]
        )
    except SystemExit as exc:
        assert exc.code == 2
    else:
        raise AssertionError("raw prediction flags must be rejected")
