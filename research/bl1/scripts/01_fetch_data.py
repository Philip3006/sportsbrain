"""FLAGSHIP-BL1-001 — Dataset acquisition.

Fetches all 10 target seasons of German Bundesliga (D1) via the existing
src.data.football_data.fetch_season loader. Writes the raw per-season pickles
to the worktree-local cache (data/cache/) and a consolidated frame to
research/bl1/dataset/bl1_raw.pkl. Reports coverage/anomalies per season.

Research only. Isolated worktree. No production writes.
"""
from __future__ import annotations

import pickle
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

import pandas as pd  # noqa: E402

from src.data.football_data import fetch_season  # noqa: E402

TARGET_SEASONS = [
    "1617", "1718", "1819", "1920", "2021",
    "2122", "2223", "2324", "2425", "2526",
]

RESEARCH_DATASET_DIR = ROOT / "research" / "bl1" / "dataset"
RESEARCH_DATASET_DIR.mkdir(parents=True, exist_ok=True)


def _pin_close_present(row) -> bool:
    return all(pd.notna(row.get(c)) for c in ("ps_close_home", "ps_close_draw", "ps_close_away"))


def _pin_open_present(row) -> bool:
    return all(pd.notna(row.get(c)) for c in ("ps_open_home", "ps_open_draw", "ps_open_away"))


def main() -> None:
    frames = {}
    audit_rows = []

    for season in TARGET_SEASONS:
        df = fetch_season("D1", season)
        if df is None or df.empty:
            audit_rows.append({
                "season": season, "n_matches": 0, "status": "MISSING",
                "n_teams": 0, "date_min": None, "date_max": None,
                "pin_close_pct": 0.0, "pin_open_pct": 0.0,
                "missing_score": 0, "dup_fixtures": 0,
            })
            continue

        # Basic coverage
        n = len(df)
        teams = sorted(set(df["home_team"]).union(df["away_team"]))
        date_min = df["date"].min()
        date_max = df["date"].max()

        # Pinnacle coverage
        has_close_cols = all(c in df.columns for c in ("ps_close_home", "ps_close_draw", "ps_close_away"))
        has_open_cols = all(c in df.columns for c in ("ps_open_home", "ps_open_draw", "ps_open_away"))
        pin_close_pct = (df.apply(_pin_close_present, axis=1).sum() / n) if has_close_cols else 0.0
        pin_open_pct = (df.apply(_pin_open_present, axis=1).sum() / n) if has_open_cols else 0.0

        # Anomalies
        missing_score = df[["home_score", "away_score"]].isna().any(axis=1).sum()
        dup_key = df[["date", "home_team", "away_team"]].astype(str).agg("|".join, axis=1)
        dup_count = int(dup_key.duplicated().sum())

        audit_rows.append({
            "season": season,
            "n_matches": n,
            "status": "OK" if n >= 200 and missing_score == 0 else "REVIEW",
            "n_teams": len(teams),
            "date_min": str(date_min.date()) if pd.notna(date_min) else None,
            "date_max": str(date_max.date()) if pd.notna(date_max) else None,
            "pin_close_pct": round(pin_close_pct, 4),
            "pin_open_pct": round(pin_open_pct, 4),
            "missing_score": int(missing_score),
            "dup_fixtures": dup_count,
            "has_close_cols": has_close_cols,
            "has_open_cols": has_open_cols,
            "teams": teams,
        })
        df["season"] = season
        frames[season] = df
        print(f"[{season}] n={n} teams={len(teams)} pin_close={pin_close_pct:.1%} pin_open={pin_open_pct:.1%} dup={dup_count} missing_score={missing_score}")

    # Consolidated
    if frames:
        all_df = pd.concat(frames.values(), ignore_index=True).sort_values(["date", "home_team"]).reset_index(drop=True)
        out_pkl = RESEARCH_DATASET_DIR / "bl1_raw.pkl"
        with open(out_pkl, "wb") as f:
            pickle.dump(all_df, f)
        print(f"\nConsolidated: {len(all_df):,} matches -> {out_pkl}")

    # Audit summary
    audit_df = pd.DataFrame(audit_rows)
    audit_out = RESEARCH_DATASET_DIR / "audit_seasons.csv"
    audit_df.drop(columns=["teams"], errors="ignore").to_csv(audit_out, index=False)
    print(f"Audit summary -> {audit_out}")

    # Team-turnover analysis
    prev_teams = None
    turnover = []
    for r in audit_rows:
        if r["n_matches"] == 0:
            continue
        cur = set(r["teams"])
        if prev_teams is not None:
            promoted = sorted(cur - prev_teams)
            relegated = sorted(prev_teams - cur)
            turnover.append({"season": r["season"], "promoted_in": promoted, "relegated_out": relegated,
                             "n_new": len(promoted), "n_out": len(relegated)})
        prev_teams = cur
    with open(RESEARCH_DATASET_DIR / "team_turnover.pkl", "wb") as f:
        pickle.dump(turnover, f)
    print(f"Team-turnover -> {RESEARCH_DATASET_DIR / 'team_turnover.pkl'}")


if __name__ == "__main__":
    main()
