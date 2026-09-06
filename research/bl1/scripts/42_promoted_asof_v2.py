"""FLAGSHIP-BL1 CORRECTED — Promoted-team policies with strict as-of priors.

CHANGES vs 41_promoted_policies.py:
- P4 as-of DC prior: for each outer fold F, compute league-average DC
  parameters using ONLY data with season < F. Different mean per fold.
- Added Elo-side policies: E0 default 1500, E1 BL1 league-avg init at
  season start (per-season Elo mean of active teams).
- Reports fold-by-fold promoted sample count.
- Uses oof_dev_v2.csv (DC + Elo strict as-of from 11_walk_forward_v2.py).

Outputs:
  research/bl1/results/promoted_policies_v2_metrics.csv
  research/bl1/results/promoted_policies_v2_by_fold.csv
"""
from __future__ import annotations

import pickle
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from src.models import dixon_coles  # noqa: E402
from src.models.elo import compute_elo_series, elo_win_probability, ELO_DEFAULT, update_ratings  # noqa: E402

RES = ROOT / "research" / "bl1" / "results"
DATASET_PKL = ROOT / "research" / "bl1" / "dataset" / "bl1_raw.pkl"

DEV_SEASONS = ["1617", "1718", "1819", "1920", "2021", "2122", "2223", "2324"]
OUTER_FOLDS = ["2021", "2122", "2223", "2324"]
BEST_PHI = 0.0012
DC_REG = 0.005
ELO_K = 20.0


def _label(row) -> int:
    return 2 if row["home_score"] > row["away_score"] else (1 if row["home_score"] == row["away_score"] else 0)


def _brier(y, p):
    onehot = np.eye(3)[y]
    return float(np.mean(np.sum((p - onehot) ** 2, axis=1)))


def _logloss(y, p):
    p = np.clip(p, 1e-12, 1.0)
    onehot = np.eye(3)[y]
    return float(-np.mean(np.sum(onehot * np.log(p), axis=1)))


def _identify_promoted(all_matches: pd.DataFrame) -> dict[str, set[str]]:
    seasons_sorted = sorted(all_matches["season"].unique())
    out = {}
    prev = None
    for s in seasons_sorted:
        cur = set(all_matches[all_matches["season"] == s]["home_team"]).union(
            all_matches[all_matches["season"] == s]["away_team"])
        out[s] = (cur - prev) if prev is not None else set()
        prev = cur
    return out


def main() -> None:
    with open(DATASET_PKL, "rb") as f:
        raw = pickle.load(f)
    raw["season"] = raw["season"].astype(str)
    raw = raw.dropna(subset=["home_score", "away_score"]).copy()
    raw["y"] = raw.apply(_label, axis=1)
    raw = raw.sort_values(["date", "home_team"], kind="stable").reset_index(drop=True)
    raw_dev = raw[raw["season"].isin(DEV_SEASONS)].copy()
    promoted_map = _identify_promoted(raw_dev)

    # Print fold-by-fold promoted sample counts
    print("Promoted teams per fold + match count:", flush=True)
    for f in OUTER_FOLDS:
        pt = promoted_map.get(f, set())
        fold_df = raw_dev[raw_dev["season"] == f]
        n_promoted_matches = fold_df.apply(
            lambda r: (r["home_team"] in pt) or (r["away_team"] in pt), axis=1
        ).sum()
        print(f"  {f}: promoted_teams={sorted(pt)}, promoted_matches={n_promoted_matches}", flush=True)

    # Per-fold DC snapshot + as-of league-avg prior (P4)
    fold_records = []
    for outer in OUTER_FOLDS:
        val_df = raw_dev[raw_dev["season"] == outer].sort_values(["date", "home_team"], kind="stable").reset_index(drop=True)
        prior = raw_dev[raw_dev["date"] < val_df["date"].min()]
        cutoff = val_df["date"].min()

        # P0: baseline DC (fit on prior only)
        dc_baseline = dixon_coles.fit(prior, phi=BEST_PHI, today=cutoff,
                                        regularization=DC_REG, max_iter=1500)
        mean_atk = float(np.mean(list(dc_baseline.attack.values())))
        mean_def = float(np.mean(list(dc_baseline.defence.values())))

        # P4 as-of: manual override for promoted teams
        dc_p4 = dixon_coles.DixonColesParams(
            attack=dict(dc_baseline.attack),
            defence=dict(dc_baseline.defence),
            home_adv=dc_baseline.home_adv, rho=dc_baseline.rho, fit_date=cutoff,
        )
        for t in promoted_map.get(outer, set()):
            dc_p4.attack[t] = mean_atk
            dc_p4.defence[t] = mean_def

        # Elo state at cutoff — computed cumulatively through prior
        elo_prior = compute_elo_series(prior, initial_ratings={}, k_competitive=ELO_K, k_friendly=ELO_K)
        ratings_at_cutoff: dict[str, float] = {}
        for _, r in elo_prior.iterrows():
            ratings_at_cutoff[r["home_team"]] = r["elo_home_post"]
            ratings_at_cutoff[r["away_team"]] = r["elo_away_post"]

        # BL1 league-average Elo (mean of teams active in prior)
        active_teams = set(prior["home_team"]).union(prior["away_team"])
        elo_league_avg = float(np.mean([ratings_at_cutoff.get(t, ELO_DEFAULT) for t in active_teams]))

        promoted_teams = promoted_map.get(outer, set())
        promoted_idx = {t: 0 for t in promoted_teams}
        ratings_e0 = dict(ratings_at_cutoff)  # E0: default 1500 for promoted
        ratings_e1 = dict(ratings_at_cutoff)  # E1: league-avg for promoted
        for t in promoted_teams:
            ratings_e0[t] = ELO_DEFAULT
            ratings_e1[t] = elo_league_avg

        for _, r in val_df.iterrows():
            home, away = r["home_team"], r["away_team"]

            # DC P0 baseline
            if home in dc_baseline.attack and away in dc_baseline.attack:
                d0 = dixon_coles.predict_match(home, away, dc_baseline)
                p_dc0 = np.array([d0["p_away"], d0["p_draw"], d0["p_home"]])
            else:
                p_dc0 = np.array([0.30, 0.26, 0.44])

            # DC P4 (as-of prior for promoted)
            if home in dc_p4.attack and away in dc_p4.attack:
                d4 = dixon_coles.predict_match(home, away, dc_p4)
                p_dc4 = np.array([d4["p_away"], d4["p_draw"], d4["p_home"]])
            else:
                p_dc4 = p_dc0

            # Elo E0 vs E1
            eh0 = ratings_e0.get(home, ELO_DEFAULT); ea0 = ratings_e0.get(away, ELO_DEFAULT)
            ph_e0, pd_e0, pa_e0 = elo_win_probability(eh0, ea0, neutral=False)
            p_elo0 = np.array([pa_e0, pd_e0, ph_e0])

            eh1 = ratings_e1.get(home, ELO_DEFAULT); ea1 = ratings_e1.get(away, ELO_DEFAULT)
            ph_e1, pd_e1, pa_e1 = elo_win_probability(eh1, ea1, neutral=False)
            p_elo1 = np.array([pa_e1, pd_e1, ph_e1])

            # Promoted status + match index
            is_promoted = (home in promoted_teams) or (away in promoted_teams)
            idx = 0
            if home in promoted_teams:
                idx = max(idx, promoted_idx[home] + 1)
            if away in promoted_teams:
                idx = max(idx, promoted_idx[away] + 1)

            # P1 Elo-only 1-5
            p_p1 = p_elo0 if (is_promoted and 1 <= idx <= 5) else p_dc0
            # P2 blend converging by idx=11
            if is_promoted:
                w = max(0.0, 1.0 - (idx - 1) / 10.0)
                p_p2 = w * p_elo0 + (1 - w) * p_dc0
            else:
                p_p2 = p_dc0
            # P3 suppression flag (used later)
            suppressed = is_promoted and 1 <= idx <= 5

            fold_records.append({
                "fold": outer, "date": r["date"], "home_team": home, "away_team": away, "y": int(r["y"]),
                "is_promoted": is_promoted, "promoted_idx": idx,
                "p0_p_away": p_dc0[0], "p0_p_draw": p_dc0[1], "p0_p_home": p_dc0[2],
                "p1_p_away": p_p1[0], "p1_p_draw": p_p1[1], "p1_p_home": p_p1[2],
                "p2_p_away": p_p2[0], "p2_p_draw": p_p2[1], "p2_p_home": p_p2[2],
                "p4_p_away": p_dc4[0], "p4_p_draw": p_dc4[1], "p4_p_home": p_dc4[2],
                "e0_p_away": p_elo0[0], "e0_p_draw": p_elo0[1], "e0_p_home": p_elo0[2],
                "e1_p_away": p_elo1[0], "e1_p_draw": p_elo1[1], "e1_p_home": p_elo1[2],
                "suppressed_p3": suppressed,
            })

            # Update Elo state
            ratings_e0 = update_ratings(ratings_e0, home, away, int(r["home_score"]), int(r["away_score"]), k=ELO_K)
            ratings_e1 = update_ratings(ratings_e1, home, away, int(r["home_score"]), int(r["away_score"]), k=ELO_K)
            if home in promoted_idx:
                promoted_idx[home] += 1
            if away in promoted_idx:
                promoted_idx[away] += 1

    records = pd.DataFrame(fold_records)
    records.to_csv(RES / "promoted_policies_v2_oof.csv", index=False)

    subsets = {
        "all": records,
        "promoted_only": records[records["is_promoted"]],
        "seasoned_only": records[~records["is_promoted"]],
        "promoted_early_1_5": records[records["is_promoted"] & (records["promoted_idx"].between(1, 5))],
        "promoted_late_6plus": records[records["is_promoted"] & (records["promoted_idx"] >= 6)],
    }
    rows = []
    for name, sub in subsets.items():
        if sub.empty:
            continue
        y = sub["y"].to_numpy()
        for policy in ("p0", "p1", "p2", "p4", "e0", "e1"):
            p = sub[[f"{policy}_p_away", f"{policy}_p_draw", f"{policy}_p_home"]].to_numpy()
            rows.append({"subset": name, "policy": policy, "n": len(sub),
                          "brier": _brier(y, p), "logloss": _logloss(y, p)})
        keep = sub[~sub["suppressed_p3"]] if "suppressed_p3" in sub.columns else sub
        if len(keep) > 0:
            yk = keep["y"].to_numpy()
            pk = keep[["p0_p_away", "p0_p_draw", "p0_p_home"]].to_numpy()
            rows.append({"subset": name, "policy": "p3_suppress", "n": len(keep),
                          "brier": _brier(yk, pk), "logloss": _logloss(yk, pk)})

    result = pd.DataFrame(rows)
    # Deterministic CSV output: round floats to a fixed precision to avoid
    # Python repr instability across runs (contamination-test invariance).
    for col in ("brier", "logloss"):
        if col in result.columns:
            result[col] = result[col].round(10)
    result.to_csv(RES / "promoted_policies_v2_metrics.csv", index=False)
    print("\nPromoted policy comparison (v2, strict as-of priors):", flush=True)
    print(result.to_string(index=False, float_format=lambda x: f"{x:.4f}"), flush=True)


if __name__ == "__main__":
    main()
