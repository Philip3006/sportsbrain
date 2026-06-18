"""
Fits Dixon-Coles model on competitive international matches.
Saves model snapshot to models/dixon_coles/params_{date}.pkl

Default: all competitive matches except OFC qualifiers (Oceania).
OFC teams (NZ, Fiji, Samoa…) run up 8-0 scores vs minnows even with phi-decay,
inflating attack ratings. Non-OFC qualifiers (CONMEBOL, UEFA, CAF, AFC, CONCACAF)
provide genuine form signal and are included.

Usage:
  python scripts/train_dixon_coles.py                    # no-OFC qualifier (default)
  python scripts/train_dixon_coles.py --finals-only      # finals + Nations Leagues only
  python scripts/train_dixon_coles.py --all              # all competitive incl. OFC qualifiers
  python scripts/train_dixon_coles.py --since 2018-01-01 # all competitive since date
"""
import argparse
import json
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.config import MODELS_DIR, COMPETITIVE_TOURNAMENTS, HIERARCHICAL_DC_ENABLED, TEAM_CONFEDERATION
from src.data.international import fetch_international_results, filter_competitive
from src.models import dixon_coles
from src.models.elo import compute_elo_series, current_ratings
from src.models.lifecycle import register_trained


def _load_active_params() -> tuple[Path, dixon_coles.DixonColesParams]:
    snap_dir = MODELS_DIR / "dixon_coles"
    snaps = sorted(snap_dir.glob("params_*.pkl"))
    if not snaps:
        raise RuntimeError("No params_*.pkl found in models/dixon_coles/")
    path = snaps[-1]
    return path, dixon_coles.load(path)


def _next_candidate_paths(snap_dir: Path, fit_date: pd.Timestamp) -> tuple[Path, Path]:
    stem = f"{fit_date.strftime('%Y%m%d')}_candidate"
    index = 1
    while True:
        suffix = f"{stem}{index:02d}"
        params_path = snap_dir / f"params_{suffix}.pkl"
        elo_path = snap_dir / f"elo_{suffix}.json"
        if not params_path.exists() and not elo_path.exists():
            return params_path, elo_path
        index += 1


def _candidate_issues(params, prior) -> list[str]:
    issues = dixon_coles.validate_params(params, prior=prior)
    hits = dixon_coles._check_bounds_hit(params)
    issues.extend(
        f"{group} optimizer bound hit: {name}={value:+.3f} ({side})"
        for group, items in hits.items() for name, value, side in items
    )
    return issues

_FINALS_TOURNAMENTS = {t for t in COMPETITIVE_TOURNAMENTS if "qualification" not in t.lower()}

_OFC_TEAMS = {
    "New Zealand", "Fiji", "Vanuatu", "Solomon Islands", "Papua New Guinea",
    "New Caledonia", "Tahiti", "Samoa", "American Samoa", "Tonga",
    "Cook Islands", "Tuvalu", "Micronesia", "Kiribati",
}


def _remove_ofc_qualifiers(df: pd.DataFrame) -> pd.DataFrame:
    qualifier_mask = df["tournament"].str.contains("qualification", case=False, na=False)
    ofc_mask = df["home_team"].isin(_OFC_TEAMS) | df["away_team"].isin(_OFC_TEAMS)
    return df[~(qualifier_mask & ofc_mask)]



def main(since: str | None = None, finals_only: bool = False, all_competitive: bool = False):
    print("Loading data...")
    df = fetch_international_results()
    df = filter_competitive(df)

    if finals_only:
        df = df[df["tournament"].isin(_FINALS_TOURNAMENTS)]
        print(f"  Finals + Nations Leagues only (no qualifiers): {len(df)} matches")
    elif all_competitive:
        if since:
            df = df[df["date"] >= pd.Timestamp(since)]
        print(f"  All competitive matches: {len(df)}")
    else:
        df = _remove_ofc_qualifiers(df)
        if since:
            df = df[df["date"] >= pd.Timestamp(since)]
        print(f"  All competitive excl. OFC qualifiers: {len(df)} matches")

    # Compute Elo series for snapshot — used by scanner at inference time only.
    # DC training does NOT use Elo adjustment (training-time Elo with scale=600
    # caused perverse parameter distortion; inference-only is the safe approach).
    print("Computing Elo series (for inference snapshot)...")
    elo_df = compute_elo_series(df)
    print(f"  Elo series computed: {len(elo_df)} matches")

    prior_path, prior = _load_active_params()
    print(f"  Warm-start from active model {prior_path.name} (fit_date={prior.fit_date.date()})")
    print("Fitting Dixon-Coles model (this may take ~60-120 seconds)...")

    # Retry-on-bound-hit: WC2026_BOOST=1.5 is large enough that a single match
    # can drive the optimizer into a bound. When that happens, refit with a
    # smaller boost rather than ship a saturated snapshot.
    boost_schedule = [None, 1.0, 0.75, 0.5, 0.25]
    params = None
    final_boost = None
    for attempt, boost in enumerate(boost_schedule):
        suffix = f"WC2026_BOOST={boost}" if boost is not None else "default WC2026_BOOST"
        print(f"  Attempt {attempt + 1}/{len(boost_schedule)} ({suffix})")
        params = dixon_coles.fit(
            df, max_iter=2000, prior_params=prior, regularization=0.1,
            wc2026_boost_override=boost,
            cluster_map=TEAM_CONFEDERATION if HIERARCHICAL_DC_ENABLED else None,
            cluster_strength=0.03 if HIERARCHICAL_DC_ENABLED else 0.0,
        )
        attempt_issues = _candidate_issues(params, prior)
        if not attempt_issues:
            final_boost = boost
            print(f"  ✓ bounds and drift clean at attempt {attempt + 1}")
            break
        print("  ⚠️  candidate issues: " + "; ".join(attempt_issues))
    else:
        # No clean fit across schedule — keep last attempt but warn loudly.
        print("  ⚠️⚠️  No clean fit found across boost schedule; keeping last attempt.")
        final_boost = boost_schedule[-1]

    # Phase 1.1: log per-team drift vs prior so we notice if the retrain shifts
    # any team's calibration by more than 0.8 (silent regression risk).
    if prior is not None and params is not None:
        atk_drift = sorted(
            ((t, params.attack[t] - prior.attack.get(t, params.attack[t]))
             for t in params.attack if t in prior.attack),
            key=lambda x: abs(x[1]), reverse=True,
        )[:5]
        def_drift = sorted(
            ((t, params.defence[t] - prior.defence.get(t, params.defence[t]))
             for t in params.defence if t in prior.defence),
            key=lambda x: abs(x[1]), reverse=True,
        )[:5]
        print(f"  Top-5 attack drift vs prior:  "
              + ", ".join(f"{t}={d:+.3f}" for t, d in atk_drift))
        print(f"  Top-5 defence drift vs prior: "
              + ", ".join(f"{t}={d:+.3f}" for t, d in def_drift))
        large = [(t, d) for t, d in atk_drift + def_drift if abs(d) > 0.8]
        if large:
            print(f"  ⚠️  {len(large)} param(s) drifted > 0.8 — review before publishing")
    print(f"  Effective WC2026_BOOST used: {final_boost}")

    snap_dir = MODELS_DIR / "dixon_coles"
    snap_dir.mkdir(parents=True, exist_ok=True)
    out_path, elo_path = _next_candidate_paths(snap_dir, params.fit_date)
    issues = _candidate_issues(params, prior)
    # Rejected candidates remain reproducible audit evidence, but force-saving
    # never changes lifecycle active and cannot make them scanner-eligible.
    dixon_coles.save(params, out_path, prior=prior, force=bool(issues))

    # Save current Elo ratings snapshot for inference (scanner reads this at startup)
    elo_now = current_ratings(elo_df)
    elo_path.write_text(json.dumps(elo_now, indent=2))
    print(f"Saved Elo snapshot: {elo_path} ({len(elo_now)} teams)")

    print(f"Saved: {out_path}")
    print(f"  Teams: {len(params.attack)}")
    print(f"  Home advantage (gamma): {params.home_adv:.4f}")
    print(f"  Rho (low-score correction): {params.rho:.4f}")
    print(f"  Fit date: {params.fit_date.date()}")

    status = register_trained(
        snap_dir,
        out_path,
        issues=issues,
        prior_snapshot=prior_path.name,
        elo_snapshot=elo_path.name,
        effective_wc2026_boost=final_boost,
        training_scope=("finals_only" if finals_only else "all_competitive" if all_competitive else "no_ofc_qualifiers"),
    )
    print(f"  Lifecycle status: {status}")

    top_attack = sorted(params.attack.items(), key=lambda x: x[1], reverse=True)[:8]
    print("\n  Top 8 attack ratings:")
    for team, val in top_attack:
        print(f"    {team}: {val:.4f}")
    return out_path


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--finals-only", action="store_true",
                        help="Use only finals + Nations Leagues (no qualifiers)")
    parser.add_argument("--all", action="store_true",
                        help="Use all competitive matches including OFC qualifiers")
    parser.add_argument("--since", default=None,
                        help="Filter to matches since YYYY-MM-DD")
    args = parser.parse_args()
    main(since=args.since, finals_only=args.finals_only, all_competitive=args.all)
