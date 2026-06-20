"""
G1 Sprint 3: Scorer-Backtest mit PPDA-Adjustment (Anytime Goalscorer).

Idee: für jeden Spieler im Match ist die Score-Wahrscheinlichkeit aus seinem
rollierenden xG ableitbar (Poisson). PPDA des Gegners moduliert die Chancen-
Erzeugung: hohes Opp-PPDA = wenig Pressing = mehr Raum = mehr Chancen für
unsere Stürmer → xG-Multiplier (opp_ppda/baseline)^alpha.

Pipeline (nur StatsBomb-Open-Coverage: WC + Euro + Copa):
  1. Lade player_xg_df, statsbomb_scorers_df, ppda_df, dc_params
  2. Für jede Match × Player-Kombi:
     - Baseline p_score (xG decay-weighted, DC-defence-adjusted)
     - +PPDA p_score = 1 - exp(-(xG_per_game × ppda_factor))
     - Actual scored bool aus scorers_df
  3. Brier + self-priced ROI (keine echten Scorer-Quoten verfügbar)

Output: results/audits/g1_ppda_scorer_<date>.json
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import math
from pathlib import Path

import numpy as np
import pandas as pd

from src.config import MODELS_DIR, RESULTS_DIR
from src.betting.goalscorer import get_top_goalscorer_predictions
from src.features.ppda import team_rolling_ppda, GLOBAL_FALLBACK_PPDA
from src.models import dixon_coles as dc


def _load_dc():
    snap_dir = MODELS_DIR / "dixon_coles"
    snaps = sorted(snap_dir.glob("params_*.pkl"))
    return dc.load(snaps[-1])


def ppda_xg_multiplier(opp_ppda: float, baseline: float = GLOBAL_FALLBACK_PPDA,
                       alpha: float = 0.5, clip: float = 0.30) -> float:
    """
    Multiplier auf scorer-xG basierend auf Gegner-PPDA.
    Hoher opp_ppda (wenig Pressing) → mult > 1 (mehr Raum, mehr Chancen).
    NaN → 1.0 (neutral). Clip auf ±30% gegen Extreme.
    """
    if not (opp_ppda == opp_ppda) or opp_ppda <= 0:
        return 1.0
    ratio = opp_ppda / baseline
    mult = float(ratio ** alpha)
    return float(min(max(mult, 1.0 - clip), 1.0 + clip))


def main(val_since: str = "2022-11-01") -> int:
    print("Lade Daten...")
    from src.data.statsbomb import fetch_statsbomb_player_xg
    player_xg = fetch_statsbomb_player_xg()
    print(f"  player_xg: {len(player_xg)} player-match Records")

    from src.data.statsbomb_scorers import fetch_statsbomb_scorers
    scorers = fetch_statsbomb_scorers()
    print(f"  scorers:   {len(scorers)} scorer-rows")

    from src.data.statsbomb_ppda import fetch_statsbomb_ppda
    ppda_df = fetch_statsbomb_ppda()
    print(f"  ppda:      {len(ppda_df)} match-rows")

    params = _load_dc()

    if player_xg.empty or ppda_df.empty:
        print("⚠️  Fehlende Quelldaten — Abbruch.")
        return 1

    val_cut = pd.Timestamp(val_since)
    val_matches = ppda_df[ppda_df["date"] >= val_cut].copy()
    print(f"  Val-Matches (PPDA & seit {val_since}): {len(val_matches)}")

    records: list[dict] = []
    for _, mrow in val_matches.iterrows():
        home, away = mrow["home_team"], mrow["away_team"]
        mdate = mrow["date"]
        ppda_h, ppda_a = mrow["home_ppda"], mrow["away_ppda"]

        for team, opp_ppda in ((home, ppda_a), (away, ppda_h)):
            preds = get_top_goalscorer_predictions(
                team, mdate, player_xg, n_games=5, top_n=10, dc_params=params,
            )
            if not preds:
                continue
            for p in preds:
                # Baseline p_score (mit DC-Defence-Adj, ohne PPDA)
                base_p = p["p_score"]
                xg_pg = p["xg_per_game"]

                # +PPDA: xG × Opp-PPDA-Faktor → neue Poisson-p
                mult = ppda_xg_multiplier(opp_ppda)
                adj_p = 1.0 - math.exp(-max(xg_pg * mult, 0.0))

                # Actual: Spieler getroffen?
                mask = (
                    (scorers["team"] == team)
                    & (scorers["date"] == mdate)
                    & (scorers["player"] == p["player"])
                )
                scored = int(scorers[mask]["goals"].sum() >= 1) if not scorers.empty else 0

                records.append({
                    "date": mdate,
                    "team": team,
                    "player": p["player"],
                    "xg_per_game": xg_pg,
                    "opp_ppda": opp_ppda,
                    "ppda_mult": mult,
                    "p_base": base_p,
                    "p_ppda": adj_p,
                    "scored": scored,
                })

    if not records:
        print("⚠️  Keine Scorer-Records — Abbruch (Val-Set hat keine Cover-Matches).")
        return 1

    df = pd.DataFrame(records)
    print(f"\n  Auswertbare Scorer-Records: {len(df)}  "
          f"(unique Spieler={df['player'].nunique()}, getroffen={int(df['scored'].sum())})")

    def _brier(p: pd.Series, y: pd.Series) -> float:
        return float(((p - y) ** 2).mean())

    def _roi_self(p: pd.Series, y: pd.Series, threshold: float, min_p: float = 0.05,
                  max_p: float = 0.80) -> tuple[float, int]:
        m = (p > threshold) & (p >= min_p) & (p <= max_p)
        if not m.any():
            return 0.0, 0
        odds = 1.0 / p[m]
        won = y[m] == 1
        pnl = np.where(won, odds - 1.0, -1.0)
        return float(pnl.mean()), int(m.sum())

    brier_base = _brier(df["p_base"], df["scored"])
    brier_ppda = _brier(df["p_ppda"], df["scored"])
    roi_base, n_base = _roi_self(df["p_base"], df["scored"], threshold=0.30)
    roi_ppda, n_ppda = _roi_self(df["p_ppda"], df["scored"], threshold=0.30)

    summary = {
        "as_of": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "val_since": val_since,
        "n_records": len(df),
        "n_players": int(df["player"].nunique()),
        "n_scored": int(df["scored"].sum()),
        "brier_baseline": brier_base,
        "brier_ppda": brier_ppda,
        "delta_brier": brier_base - brier_ppda,
        "roi_baseline_self_priced": roi_base,
        "roi_ppda_self_priced": roi_ppda,
        "delta_roi_pp": (roi_ppda - roi_base) * 100.0,
        "n_bets_baseline": n_base,
        "n_bets_ppda": n_ppda,
        "avg_ppda_mult": float(df["ppda_mult"].mean()),
        "notes": [
            "Scorer-Quoten sind nicht im Repo verfügbar → ROI ist self-priced (Quote=1/p).",
            "Brier ist der primäre Indikator; ROI nur als Konsistenz-Check.",
            "PPDA-XG-Multiplier: (opp_ppda/11.5)^0.5, clipped ±30%.",
        ],
    }

    out_dir = RESULTS_DIR / "audits"
    out_dir.mkdir(parents=True, exist_ok=True)
    today = _dt.date.today().isoformat()
    out_path = out_dir / f"g1_ppda_scorer_{today}.json"
    out_path.write_text(json.dumps(summary, indent=2))

    print("\n" + "=" * 60)
    print("G1 Scorer-Backtest — Zusammenfassung")
    print("=" * 60)
    print(f"  Records:           {len(df)}  (geschossen: {int(df['scored'].sum())})")
    print(f"  avg PPDA-mult:     {float(df['ppda_mult'].mean()):.4f}")
    print(f"  Brier Baseline:    {brier_base:.4f}")
    print(f"  Brier +PPDA:       {brier_ppda:.4f}")
    print(f"  Δ Brier:           {brier_base - brier_ppda:+.4f} "
          f"{'✅' if brier_base - brier_ppda >= 0.0005 else '⚠️ '}")
    print(f"  ROI Baseline (self): {roi_base:+.4f}  (n={n_base})")
    print(f"  ROI +PPDA (self):    {roi_ppda:+.4f}  (n={n_ppda})")
    print(f"  Δ ROI (pp):        {(roi_ppda - roi_base) * 100:+.2f}pp")
    print(f"\nBericht: {out_path.relative_to(Path.cwd())}")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--val-since", default="2022-11-01")
    args = ap.parse_args()
    raise SystemExit(main(val_since=args.val_since))
