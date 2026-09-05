"""FLAGSHIP-BL1 continuation — Promoted-team policy empirical test.

Evaluates 4 policies on strict DEVELOPMENT OOF (no 2425, no 2526):

  P0: normal DC (baseline)
  P1: Elo-only for promoted-team matches 1-5, DC otherwise
  P2: DC/Elo blend with heavier Elo weight early; converge to DC by match 11
      weight_elo(idx) = max(0, 1 - (idx - 1) / 10)  # 1.0 at idx=1, 0 at idx>=11
  P3: signal suppression for promoted-team matches 1-5 (drop from Brier/LogLoss)
  P4: DC with warm-start prior (league-average params) — implemented via
      dixon_coles.fit(prior_params=league_avg_params)

Reports Brier/LogLoss/ECE per policy on:
  - all matches
  - promoted-match subset
  - seasoned-match subset (should be identical across policies except P4)
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

DATASET_PKL = ROOT / "research" / "bl1" / "dataset" / "bl1_raw.pkl"
RES = ROOT / "research" / "bl1" / "results"
FOLDS = [("2021", "1920"), ("2122", "2021"), ("2223", "2122"), ("2324", "2223")]
DEV_SEASONS = ["1617", "1718", "1819", "1920", "2021", "2122", "2223", "2324"]
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
    out: dict[str, set[str]] = {}
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
    raw_dev = raw[raw["season"].isin(DEV_SEASONS)].copy()
    promoted_map = _identify_promoted(raw_dev)

    # Compute league-average DC params from a single fit on 1617-1920 (safe as-of)
    seed_slice = raw_dev[raw_dev["season"].isin(["1617", "1718", "1819", "1920"])]
    league_avg_params = dixon_coles.fit(seed_slice, phi=BEST_PHI, today=seed_slice["date"].max() + pd.Timedelta(days=1),
                                          regularization=DC_REG, max_iter=1500)
    # Reduce to mean attack/defence for use as a per-team prior seed
    mean_attack = float(np.mean(list(league_avg_params.attack.values())))
    mean_defence = float(np.mean(list(league_avg_params.defence.values())))
    home_adv_seed = float(league_avg_params.home_adv)
    rho_seed = float(league_avg_params.rho)

    rows = []
    per_row_records = []

    for val_season, _ in FOLDS:
        val_df = raw_dev[raw_dev["season"] == val_season].sort_values("date").reset_index(drop=True)
        train_df = raw_dev[raw_dev["date"] < val_df["date"].min()].copy()
        cutoff = val_df["date"].min()

        # P0/P1/P2/P3 use the same DC fit; P4 uses a warm-start with league-average prior.
        dc_baseline = dixon_coles.fit(train_df, phi=BEST_PHI, today=cutoff,
                                        regularization=DC_REG, max_iter=1500)

        # For P4: build a warm-start prior with league-average values for promoted teams.
        # dixon_coles.fit accepts a full DixonColesParams `prior_params` — we can prep
        # one that's identical to dc_baseline for known teams and league-average for promoted.
        prior_p4 = dixon_coles.DixonColesParams(
            attack=dict(dc_baseline.attack),
            defence=dict(dc_baseline.defence),
            home_adv=dc_baseline.home_adv,
            rho=dc_baseline.rho,
            fit_date=cutoff,
        )
        for team in promoted_map.get(val_season, set()):
            prior_p4.attack[team] = mean_attack
            prior_p4.defence[team] = mean_defence
        # P4 re-fits DC with this prior (adds train_df + prior to influence promoted-team pos.)
        # But since promoted teams don't yet appear in train_df, the prior_params attack for
        # them will simply be used as init but they have no training weight — result: they
        # keep the prior. So P4 effectively = manual override of promoted-team DC params.

        # Elo state at cutoff
        elo_train = compute_elo_series(train_df, initial_ratings={}, k_competitive=ELO_K, k_friendly=ELO_K)
        ratings_at_cutoff: dict[str, float] = {}
        for _, r in elo_train.iterrows():
            ratings_at_cutoff[r["home_team"]] = r["elo_home_post"]
            ratings_at_cutoff[r["away_team"]] = r["elo_away_post"]

        # Promoted-team match-index tracker across the val season
        promoted_teams = promoted_map.get(val_season, set())
        promoted_idx: dict[str, int] = {t: 0 for t in promoted_teams}
        ratings = dict(ratings_at_cutoff)

        for _, r in val_df.iterrows():
            home, away = r["home_team"], r["away_team"]

            # Elo-based 1X2 for this match (pre-match state)
            eh = ratings.get(home, ELO_DEFAULT)
            ea = ratings.get(away, ELO_DEFAULT)
            ph_e, pd_e, pa_e = elo_win_probability(eh, ea, neutral=False)
            p_elo = np.array([pa_e, pd_e, ph_e])

            # DC-based 1X2 (P0 baseline). If team unknown → league base rate.
            if home in dc_baseline.attack and away in dc_baseline.attack:
                dc_probs = dixon_coles.predict_match(home, away, dc_baseline)
                p_dc = np.array([dc_probs["p_away"], dc_probs["p_draw"], dc_probs["p_home"]])
            else:
                # For P0 without a known-pair: base-rate fallback (same 44/26/30 used elsewhere)
                p_dc = np.array([0.30, 0.26, 0.44])

            # P4: DC with warm-start prior override for promoted teams.
            # Because prior_p4 assigns mean_attack/mean_defence to promoted teams and dc_baseline
            # skips them, the P4 prediction manually uses those prior values.
            if home in prior_p4.attack and away in prior_p4.attack:
                dc_probs_p4 = dixon_coles.predict_match(home, away, prior_p4)
                p_p4 = np.array([dc_probs_p4["p_away"], dc_probs_p4["p_draw"], dc_probs_p4["p_home"]])
            else:
                p_p4 = p_dc

            # Determine promoted-team status + match index (use MAX of home/away idx)
            is_promoted_match = (home in promoted_teams) or (away in promoted_teams)
            promoted_match_idx = 0
            if home in promoted_teams:
                promoted_match_idx = max(promoted_match_idx, promoted_idx[home] + 1)
            if away in promoted_teams:
                promoted_match_idx = max(promoted_match_idx, promoted_idx[away] + 1)

            # P1: Elo for promoted-team matches with idx 1-5, DC otherwise
            if is_promoted_match and 1 <= promoted_match_idx <= 5:
                p_p1 = p_elo
            else:
                p_p1 = p_dc

            # P2: DC/Elo blend converging by match 11
            if is_promoted_match:
                w_elo = max(0.0, 1.0 - (promoted_match_idx - 1) / 10.0)
                p_p2 = w_elo * p_elo + (1 - w_elo) * p_dc
            else:
                p_p2 = p_dc

            # P3: suppression flag (drop from metrics)
            suppressed_p3 = is_promoted_match and 1 <= promoted_match_idx <= 5

            per_row_records.append({
                "season": val_season, "date": r["date"],
                "home_team": home, "away_team": away, "y": int(r["y"]),
                "is_promoted_match": is_promoted_match,
                "promoted_match_idx": promoted_match_idx,
                "p0_p_away": p_dc[0], "p0_p_draw": p_dc[1], "p0_p_home": p_dc[2],
                "p1_p_away": p_p1[0], "p1_p_draw": p_p1[1], "p1_p_home": p_p1[2],
                "p2_p_away": p_p2[0], "p2_p_draw": p_p2[1], "p2_p_home": p_p2[2],
                "p4_p_away": p_p4[0], "p4_p_draw": p_p4[1], "p4_p_home": p_p4[2],
                "suppressed_p3": suppressed_p3,
            })

            # Update state
            ratings = update_ratings(ratings, home, away, int(r["home_score"]), int(r["away_score"]), k=ELO_K)
            if home in promoted_idx:
                promoted_idx[home] += 1
            if away in promoted_idx:
                promoted_idx[away] += 1

    records = pd.DataFrame(per_row_records)
    records.to_csv(RES / "promoted_policies_oof.csv", index=False)

    # Metrics per policy per subset
    subsets = {
        "all": records,
        "promoted_only": records[records["is_promoted_match"]],
        "seasoned_only": records[~records["is_promoted_match"]],
        "promoted_early_1_5": records[records["is_promoted_match"] & (records["promoted_match_idx"].between(1, 5))],
        "promoted_late_6_34": records[records["is_promoted_match"] & (records["promoted_match_idx"] >= 6)],
    }
    rows = []
    for name, sub in subsets.items():
        y = sub["y"].to_numpy()
        for policy in ("p0", "p1", "p2", "p4"):
            p = sub[[f"{policy}_p_away", f"{policy}_p_draw", f"{policy}_p_home"]].to_numpy()
            rows.append({"subset": name, "policy": policy, "n": len(sub),
                          "brier": _brier(y, p), "logloss": _logloss(y, p)})
        # P3: same as P0 but excludes suppressed rows
        keep = sub[~sub["suppressed_p3"]] if "suppressed_p3" in sub.columns else sub
        if len(keep) > 0:
            y_k = keep["y"].to_numpy()
            p_k = keep[["p0_p_away", "p0_p_draw", "p0_p_home"]].to_numpy()
            rows.append({"subset": name, "policy": "p3_suppress", "n": len(keep),
                          "brier": _brier(y_k, p_k), "logloss": _logloss(y_k, p_k)})

    result = pd.DataFrame(rows)
    result.to_csv(RES / "promoted_policies_metrics.csv", index=False)
    print(result.to_string(index=False, float_format=lambda x: f"{x:.4f}"), flush=True)


if __name__ == "__main__":
    main()
