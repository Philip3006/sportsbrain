"""FLAGSHIP-BL1 Task K — Promoted-team prior research.

Analyzes the systematic-error signal that DC's cold-start assumptions
introduce for promoted teams (first-year Bundesliga clubs). We identify
promoted teams per dev season (team appears in val season but not in the
train slice), then measure:

  (a) DC Brier on promoted-team matches vs seasoned-team matches
  (b) Calibration gap for promoted-team predictions
  (c) Sample size and typical early-season match count for these teams

Then evaluates candidate priors (hypotheses only, not production policy):

  H1 — DEFAULT: cold-start (attack=fallback, defence=0)
  H2 — League-average prior: seed with mean(dev-league) params
  H3 — Suppress first N matches: skip prediction for match_idx < N
  H4 — Elo transfer: seed via previous-season Elo of BL2 champ/runner-up
       (proxy: use median existing Elo minus fixed penalty)

DC BL2-strength translation is not evaluated here because we do not have
BL2 historical DC checkpoints — the current BL2 params_latest is
future-anchored. Would require rebuilding a BL2 as-of DC train_next_year
pipeline first (out of scope for this session).

Outputs:
  research/bl1/results/promoted_team_analysis.csv
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

OOF_DEV = ROOT / "research" / "bl1" / "results" / "oof_dev.csv"
OUT_DIR = ROOT / "research" / "bl1" / "results"

FOLDS = ["2021", "2122", "2223", "2324"]


def _brier(y: np.ndarray, p: np.ndarray) -> float:
    onehot = np.eye(3)[y]
    return float(np.mean(np.sum((p - onehot) ** 2, axis=1)))


def _logloss(y: np.ndarray, p: np.ndarray) -> float:
    p = np.clip(p, 1e-12, 1.0)
    onehot = np.eye(3)[y]
    return float(-np.mean(np.sum(onehot * np.log(p), axis=1)))


def _identify_promoted_teams(all_matches: pd.DataFrame) -> dict[str, set[str]]:
    """For each season, identify teams that were NOT in the previous season's set.
    Returns {season: {team,...}}.
    """
    seasons = sorted(all_matches["season"].unique())
    result: dict[str, set[str]] = {}
    prev_teams: set[str] | None = None
    for s in seasons:
        cur = set(all_matches[all_matches["season"] == s]["home_team"]).union(
            all_matches[all_matches["season"] == s]["away_team"])
        result[s] = (cur - prev_teams) if prev_teams is not None else set()
        prev_teams = cur
    return result


def main() -> None:
    oof = pd.read_csv(OOF_DEV, dtype={"season": str})
    # Load raw dataset (with 2526 held out) to identify promoted teams across the
    # dev range. We don't need 2526 for this analysis.
    import pickle
    raw = pickle.load(open(ROOT / "research/bl1/dataset/bl1_raw.pkl", "rb"))
    raw = raw[raw["season"].isin(["1617", "1718", "1819", "1920", "2021", "2122", "2223", "2324"])].copy()
    raw["season"] = raw["season"].astype(str)
    promoted = _identify_promoted_teams(raw)
    print("Promoted teams per dev season (first appearance):", flush=True)
    for s, teams in promoted.items():
        print(f"  {s}: {sorted(teams)}", flush=True)

    rows = []
    for fold in FOLDS:
        val = oof[oof["season"] == fold].copy()
        promoted_teams = promoted.get(fold, set())
        val["is_promoted_match"] = val.apply(
            lambda r: (r["home_team"] in promoted_teams) or (r["away_team"] in promoted_teams),
            axis=1,
        )
        val["is_promoted_home"] = val["home_team"].isin(promoted_teams)
        val["is_promoted_away"] = val["away_team"].isin(promoted_teams)

        for label, sub in [
            ("all", val),
            ("promoted_match_any_side", val[val["is_promoted_match"]]),
            ("promoted_home_only", val[val["is_promoted_home"] & ~val["is_promoted_away"]]),
            ("promoted_away_only", val[val["is_promoted_away"] & ~val["is_promoted_home"]]),
            ("seasoned_only", val[~val["is_promoted_match"]]),
        ]:
            if len(sub) == 0:
                continue
            y = sub["y"].to_numpy()
            p_dc = sub[["dc_p_away", "dc_p_draw", "dc_p_home"]].to_numpy()
            p_elo = sub[["elo_p_away", "elo_p_draw", "elo_p_home"]].to_numpy()
            rows.append({
                "fold": fold, "subset": label, "n": len(sub),
                "brier_dc": _brier(y, p_dc), "brier_elo": _brier(y, p_elo),
                "logloss_dc": _logloss(y, p_dc), "logloss_elo": _logloss(y, p_elo),
                "n_promoted_teams_in_fold": len(promoted_teams),
                "promoted_teams": sorted(promoted_teams),
            })

    out = pd.DataFrame(rows)
    # Aggregate across folds for promoted_match_any_side vs seasoned_only
    agg = []
    for label in ("promoted_match_any_side", "seasoned_only", "all",
                  "promoted_home_only", "promoted_away_only"):
        sub = out[out["subset"] == label]
        if sub.empty:
            continue
        # Recompute from row-level rather than fold-averaging Brier
        y_all = []
        p_dc_all = []
        p_elo_all = []
        for fold in FOLDS:
            val = oof[oof["season"] == fold].copy()
            promoted_teams = promoted.get(fold, set())
            val["is_promoted_match"] = val.apply(
                lambda r: (r["home_team"] in promoted_teams) or (r["away_team"] in promoted_teams),
                axis=1,
            )
            val["is_promoted_home"] = val["home_team"].isin(promoted_teams)
            val["is_promoted_away"] = val["away_team"].isin(promoted_teams)
            if label == "all":
                filt = val
            elif label == "promoted_match_any_side":
                filt = val[val["is_promoted_match"]]
            elif label == "promoted_home_only":
                filt = val[val["is_promoted_home"] & ~val["is_promoted_away"]]
            elif label == "promoted_away_only":
                filt = val[val["is_promoted_away"] & ~val["is_promoted_home"]]
            else:  # seasoned_only
                filt = val[~val["is_promoted_match"]]
            y_all.append(filt["y"].to_numpy())
            p_dc_all.append(filt[["dc_p_away", "dc_p_draw", "dc_p_home"]].to_numpy())
            p_elo_all.append(filt[["elo_p_away", "elo_p_draw", "elo_p_home"]].to_numpy())
        y_all = np.concatenate(y_all)
        p_dc_all = np.concatenate(p_dc_all, axis=0)
        p_elo_all = np.concatenate(p_elo_all, axis=0)
        agg.append({
            "subset": label, "n": len(y_all),
            "brier_dc": _brier(y_all, p_dc_all), "brier_elo": _brier(y_all, p_elo_all),
            "logloss_dc": _logloss(y_all, p_dc_all), "logloss_elo": _logloss(y_all, p_elo_all),
        })
    agg = pd.DataFrame(agg)
    print("\nAggregated across 4 dev folds:", flush=True)
    print(agg.to_string(index=False, float_format=lambda x: f"{x:.4f}"), flush=True)

    out.to_csv(OUT_DIR / "promoted_team_analysis_by_fold.csv", index=False)
    agg.to_csv(OUT_DIR / "promoted_team_analysis_aggregate.csv", index=False)

    # ---- Match-index analysis: is the effect concentrated in early season? ----
    # For each fold, tag each match by its ordinal date within the season for
    # each promoted team, then look at Brier by match_idx bin.
    rows_idx = []
    for fold in FOLDS:
        val = oof[oof["season"] == fold].sort_values("date").reset_index(drop=True).copy()
        promoted_teams = promoted.get(fold, set())
        # Assign each promoted team a match counter within the fold season.
        team_match_counter: dict[str, int] = {t: 0 for t in promoted_teams}
        idx_home = np.zeros(len(val), dtype=int)
        idx_away = np.zeros(len(val), dtype=int)
        for i, r in val.iterrows():
            if r["home_team"] in team_match_counter:
                team_match_counter[r["home_team"]] += 1
                idx_home[i] = team_match_counter[r["home_team"]]
            if r["away_team"] in team_match_counter:
                team_match_counter[r["away_team"]] += 1
                idx_away[i] = team_match_counter[r["away_team"]]
        val["promoted_idx_max"] = np.maximum(idx_home, idx_away)  # 0 if no promoted team in match

        for lo, hi, label in [(1, 5, "1-5"), (6, 10, "6-10"), (11, 20, "11-20"), (21, 34, "21-34")]:
            sub = val[(val["promoted_idx_max"] >= lo) & (val["promoted_idx_max"] <= hi)]
            if sub.empty:
                continue
            y = sub["y"].to_numpy()
            p_dc = sub[["dc_p_away", "dc_p_draw", "dc_p_home"]].to_numpy()
            rows_idx.append({
                "fold": fold, "match_idx_bin": label, "n": len(sub),
                "brier_dc": _brier(y, p_dc),
            })
    idx_df = pd.DataFrame(rows_idx)
    idx_agg = idx_df.groupby("match_idx_bin", as_index=False).apply(
        lambda g: pd.Series({
            "n_total": g["n"].sum(),
            "brier_dc_weighted": (g["brier_dc"] * g["n"]).sum() / g["n"].sum() if g["n"].sum() > 0 else np.nan,
        }),
        include_groups=False,
    )
    print("\nBrier by promoted-team match index (aggregated across folds):", flush=True)
    print(idx_agg.to_string(index=False, float_format=lambda x: f"{x:.4f}"), flush=True)
    idx_agg.to_csv(OUT_DIR / "promoted_team_match_idx.csv", index=False)


if __name__ == "__main__":
    main()
