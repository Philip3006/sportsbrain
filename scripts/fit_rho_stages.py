"""
Empirical Rho-Factor fitting per tournament stage (walk-forward, no leakage).

For each historical tournament edition, uses the DC snapshot trained on data
BEFORE the edition's start date (built by scripts/build_dc_snapshots.py).
That removes the temporal-leakage problem of using current-day DC params
to predict 1998 matches.

Per stage (group, R16, QF, SF, final) we grid-search rho_factor ∈ [0.0, 1.5]
minimizing multiclass Brier on the pooled stage subset across all editions.
Stages with n<40 matches use Bayesian shrinkage to the group-stage factor
(alpha = 40/(40+n)).

Run: python3 scripts/fit_rho_stages.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.config import MODELS_DIR
from src.data.international import fetch_international_results
from src.ensemble.calibration import brier_score_multiclass
from src.models import dixon_coles as dc


TOURNAMENTS = ("FIFA World Cup", "UEFA Euro", "Copa América", "Copa America")

_STAGE_LAYOUT_WC = {
    1: "final",
    2: "third_place",
    3: "sf",  4: "sf",
    5: "qf",  6: "qf",  7: "qf",  8: "qf",
    9: "r16", 10: "r16", 11: "r16", 12: "r16",
    13: "r16", 14: "r16", 15: "r16", 16: "r16",
}
_STAGE_LAYOUT_NO3P = {
    1: "final",
    2: "sf",  3: "sf",
    4: "qf",  5: "qf",  6: "qf",  7: "qf",
    8: "r16", 9: "r16", 10: "r16", 11: "r16",
    12: "r16", 13: "r16", 14: "r16", 15: "r16",
}


def _tag_edition(edition: pd.DataFrame, has_third_place: bool) -> pd.Series:
    n = len(edition)
    layout = _STAGE_LAYOUT_WC if has_third_place else _STAGE_LAYOUT_NO3P
    stages = []
    for i in range(n):
        rank_from_end = n - i
        stages.append(layout.get(rank_from_end, "group"))
    return pd.Series(stages, index=edition.index)


def tag_stages_and_editions(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy().sort_values("date").reset_index(drop=True)
    df["stage"] = "group"
    df["edition_tag"] = ""

    for t in TOURNAMENTS:
        mask = df["tournament"] == t
        if not mask.any():
            continue
        sub = df[mask].sort_values("date")
        gap = sub["date"].diff().dt.days.fillna(99999) > 60
        edition_id = gap.cumsum().astype(int)
        for eid, grp in sub.groupby(edition_id):
            grp_sorted = grp.sort_values("date")
            year = int(grp_sorted["date"].min().year)
            tag = t.replace(" ", "").replace("é", "e").replace("É", "E")
            edition_tag = f"{tag}_{year}"
            df.loc[grp_sorted.index, "edition_tag"] = edition_tag
            df.loc[grp_sorted.index, "stage"] = _tag_edition(
                grp_sorted, has_third_place=(t == "FIFA World Cup")
            ).values

    return df


def load_snapshots() -> dict[str, "dc.DixonColesParams"]:
    snap_dir = MODELS_DIR / "dixon_coles" / "snapshots"
    out: dict[str, "dc.DixonColesParams"] = {}
    for p in sorted(snap_dir.glob("params_*.pkl")):
        tag = p.stem.replace("params_", "")
        out[tag] = dc.load(p)
    return out


def outcome_label(row) -> int:
    hg, ag = int(row["home_score"]), int(row["away_score"])
    if hg > ag:
        return 2
    if hg < ag:
        return 0
    return 1


def predict_probs(matches: pd.DataFrame, snapshots: dict, rho_factor: float) -> np.ndarray:
    """Predict (N, 3) per match using the matching edition snapshot."""
    out = np.zeros((len(matches), 3))
    for i, (_, r) in enumerate(matches.iterrows()):
        params = snapshots.get(r["edition_tag"])
        if params is None:
            out[i] = [1/3, 1/3, 1/3]
            continue
        rho_target = params.rho * rho_factor
        try:
            p = dc.predict_match(
                r["home_team"], r["away_team"], params,
                neutral=bool(r.get("neutral", True)),
                rho_override=rho_target,
            )
            out[i] = [p["p_away"], p["p_draw"], p["p_home"]]
        except Exception:
            out[i] = [1/3, 1/3, 1/3]
    return out


def fit_stage(matches: pd.DataFrame, snapshots: dict, grid: np.ndarray) -> tuple[float, float, dict]:
    y = np.array([outcome_label(r) for _, r in matches.iterrows()])
    best_f, best_b = 1.0, float("inf")
    curve = {}
    for f in grid:
        probs = predict_probs(matches, snapshots, f)
        b = brier_score_multiclass(probs, y)
        curve[float(f)] = float(b)
        if b < best_b:
            best_b, best_f = b, float(f)
    return best_f, best_b, curve


def main():
    print("Loading historical tournament data...")
    df = fetch_international_results()
    df = df[df["tournament"].isin(TOURNAMENTS)]
    df = df[df["date"] >= pd.Timestamp("1998-01-01")]
    print(f"  {len(df)} matches")

    print("Tagging stages + editions...")
    df = tag_stages_and_editions(df)
    print("  Stage counts:")
    for s, n in df["stage"].value_counts().items():
        print(f"    {s:12s} n={n}")

    print("Loading walk-forward snapshots...")
    snapshots = load_snapshots()
    print(f"  {len(snapshots)} snapshots loaded")
    have_snap = df["edition_tag"].isin(snapshots.keys())
    if not have_snap.all():
        missing = df.loc[~have_snap, "edition_tag"].unique()
        print(f"  WARNING: no snapshot for editions: {missing}")
    df = df[have_snap].reset_index(drop=True)
    print(f"  {len(df)} matches with snapshot available")

    grid = np.round(np.arange(0.0, 1.55, 0.05), 2)
    print(f"\nFitting rho_factor per stage (grid 0.0 → 1.5, step 0.05)...")

    results: dict[str, dict] = {}
    stages_ordered = ["group", "r16", "qf", "sf", "third_place", "final"]
    default_factor = {"group": 1.10, "r16": 0.75, "qf": 0.75, "sf": 0.75,
                      "third_place": 0.75, "final": 0.75}

    group_subset = df[df["stage"] == "group"]
    group_best_f, group_best_b, _ = fit_stage(group_subset, snapshots, grid)
    print(f"\n  group:        n={len(group_subset):4d}  best={group_best_f:.2f} "
          f"(Brier {group_best_b:.4f})  baseline=1.10")
    results["group"] = {"n": int(len(group_subset)), "fitted": group_best_f,
                        "shrunk": group_best_f, "alpha": 0.0,
                        "brier_fitted": group_best_b,
                        "baseline": default_factor["group"]}

    SHRINK_PRIOR_N = 40
    for s in [x for x in stages_ordered if x != "group"]:
        sub = df[df["stage"] == s]
        n = len(sub)
        if n == 0:
            continue
        f, b, _ = fit_stage(sub, snapshots, grid)
        alpha = SHRINK_PRIOR_N / (SHRINK_PRIOR_N + n)
        shrunk = alpha * group_best_f + (1 - alpha) * f
        y = np.array([outcome_label(r) for _, r in sub.iterrows()])
        b_default = brier_score_multiclass(predict_probs(sub, snapshots, default_factor[s]), y)
        b_shrunk = brier_score_multiclass(predict_probs(sub, snapshots, shrunk), y)
        print(f"  {s:12s}: n={n:4d}  best={f:.2f} (Brier {b:.4f})  "
              f"shrunk={shrunk:.2f} (Brier {b_shrunk:.4f}, α={alpha:.2f})  "
              f"vs default {default_factor[s]:.2f} (Brier {b_default:.4f})")
        results[s] = {"n": int(n), "fitted": f, "shrunk": shrunk, "alpha": alpha,
                      "brier_fitted": b, "brier_shrunk": b_shrunk,
                      "brier_default": b_default, "baseline": default_factor[s]}

    out_path = MODELS_DIR / "dixon_coles" / "rho_stages.json"
    out_path.write_text(json.dumps(results, indent=2, default=float))
    print(f"\nSaved: {out_path}")

    print("\nRecommended rho_factor (shrunk) for predict_match_staged():")
    for s in stages_ordered:
        if s in results:
            print(f"  {s:12s} → {results[s]['shrunk']:.2f}")


if __name__ == "__main__":
    main()
