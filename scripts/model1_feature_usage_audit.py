#!/usr/bin/env python3
"""Read-only structural feature-usage audit for persisted Tennis HGB model."""
from __future__ import annotations

from collections import Counter
from pathlib import Path
import json
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.models import tennis_lgbm as tlgbm

MODEL_DIR = ROOT / "models" / "tennis_lgbm"

SERVE = {
    "serve_dom_a", "serve_dom_b", "serve_dom_diff",
    "serve_ace_rate_a", "serve_ace_rate_b", "serve_ace_rate_diff",
    "serve_df_rate_a", "serve_df_rate_b", "serve_df_rate_diff",
    "serve_stats_n_a", "serve_stats_n_b",
    "serve_first_in_a", "serve_first_in_b", "serve_first_in_diff",
    "serve_first_win_a", "serve_first_win_b", "serve_first_win_diff",
    "serve_second_win_a", "serve_second_win_b", "serve_second_win_diff",
    "serve_bp_save_a", "serve_bp_save_b", "serve_bp_save_diff",
    "return_bp_conv_a", "return_bp_conv_b", "return_bp_conv_diff",
}
RANK = {"rank_a", "rank_b", "rank_diff", "rank_log_ratio", "rank_diff_x_bo5"}
UNCERTAINTY = {"elo_uncertainty_a", "elo_uncertainty_b", "elo_confidence"}
ROLLING_PREFIXES = (
    "form_", "h2h_", "rest_", "sets_dropped_", "tb_wr_", "sets_last7d_"
)


def main() -> None:
    wrapped = tlgbm.load(MODEL_DIR)
    estimator = wrapped.model
    cols = list(wrapped.feature_columns)
    if not hasattr(estimator, "_predictors"):
        raise RuntimeError(f"unsupported estimator internals: {type(estimator).__name__}")

    counts: Counter[str] = Counter()
    split_nodes = 0
    trees = 0
    unknown_indexes = []

    for iteration in estimator._predictors:
        for tree in iteration:
            trees += 1
            nodes = tree.nodes
            names = getattr(nodes.dtype, "names", ()) or ()
            if "is_leaf" not in names or "feature_idx" not in names:
                raise RuntimeError(f"unexpected TreePredictor node fields: {names}")
            for node in nodes:
                if bool(node["is_leaf"]):
                    continue
                idx = int(node["feature_idx"])
                split_nodes += 1
                if 0 <= idx < len(cols):
                    counts[cols[idx]] += 1
                else:
                    unknown_indexes.append(idx)

    def group(names):
        return {
            "features": sorted(names),
            "total_split_count": int(sum(counts[n] for n in names)),
            "used_features": {n: int(counts[n]) for n in sorted(names) if counts[n] > 0},
            "unused_features": sorted(n for n in names if counts[n] == 0),
        }

    rolling = {c for c in cols if c.startswith(ROLLING_PREFIXES)}
    result = {
        "estimator": type(estimator).__name__,
        "n_features": len(cols),
        "trees": trees,
        "split_nodes": split_nodes,
        "unknown_feature_indexes": unknown_indexes,
        "serve": group(SERVE & set(cols)),
        "rank": group(RANK & set(cols)),
        "elo_uncertainty": group(UNCERTAINTY & set(cols)),
        "rolling_state": group(rolling),
        "top_split_features": counts.most_common(30),
    }
    print("MODEL1_FEATURE_USAGE=" + json.dumps(result, sort_keys=True))

    # Hard structural assertion for the current training regime: if serve columns
    # were constant, they should have no tree splits. Failing here is useful evidence
    # that the persisted artifact does not match the audited training assumptions.
    if result["serve"]["total_split_count"] != 0:
        raise RuntimeError(
            "Persisted model DOES use serve features; FND-MODEL1-002 must remain high severity "
            "and training-artifact provenance requires investigation."
        )


if __name__ == "__main__":
    main()
