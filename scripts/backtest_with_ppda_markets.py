"""
G1 Markt-Erweiterung: Backtest des PPDA-Effekts auf AH / O-U / BTTS / 1X2
über DC-Lambda-Adjustment.

Idee: niedriges Team-PPDA = aggressives Pressing → eigenes Attack-Lambda
leicht boost'en (`src.features.ppda.ppda_lambda_multipliers`). Daraus folgt
veränderte Score-Matrix → veränderte Markt-Wahrscheinlichkeiten in AH+0.5,
Over/Under 2.5, BTTS und 1X2.

Vergleich Baseline-DC vs PPDA-adjustierte-DC pro Markt:
  - Brier (saubere Probability-Quality-Metrik)
  - ROI (self-priced: Quote = 1/p) als konsistenter Vergleichs-Proxy.
    1X2 nutzt football-data Closing-Odds wo verfügbar; AH/O-U/BTTS immer
    self-priced (keine Closing-Quelle im Repo).

Scorer (Anytime Goalscorer) bewusst out-of-scope dieser Iteration:
  → braucht per-Player-xG-Adjustment durch Team-PPDA + Minuten-Annahmen.
  Eigenes Item (siehe Roadmap G1 Follow-up).

Output: results/audits/g1_ppda_markets_<date>.json + Konsole.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
from pathlib import Path

import numpy as np
import pandas as pd

from src.config import MODELS_DIR, RESULTS_DIR
from src.data.international import fetch_international_results, filter_competitive, filter_since
from src.data.football_data_intl import fetch_wc_odds
from src.features.ppda import (
    ppda_lambda_multipliers,
    team_rolling_ppda,
)
from src.models import dixon_coles as dc


# ----------------------------- Helpers -----------------------------

def _load_dc_params():
    snap_dir = MODELS_DIR / "dixon_coles"
    snaps = sorted(snap_dir.glob("params_*.pkl"))
    if not snaps:
        raise RuntimeError("Kein DC-Snapshot — train_dixon_coles.py zuerst.")
    return dc.load(snaps[-1])


def _load_ppda_df() -> pd.DataFrame | None:
    try:
        from src.data.statsbomb_ppda import fetch_statsbomb_ppda
        df = fetch_statsbomb_ppda()
        return df if df is not None and not df.empty else None
    except Exception as e:
        print(f"  Warnung: PPDA-Fetch fehlgeschlagen ({e})")
        return None


def _scoreline_from_lambdas(lh: float, la: float, rho: float, max_goals: int = 7) -> np.ndarray:
    """Re-implementiert dc.predict_scoreline mit übergebenen Lambdas (kein erneutes DC-Lookup)."""
    from scipy.stats import poisson
    matrix = np.zeros((max_goals + 1, max_goals + 1))
    for i in range(max_goals + 1):
        for j in range(max_goals + 1):
            t = dc._tau(i, j, lh, la, rho)
            matrix[i, j] = poisson.pmf(i, lh) * poisson.pmf(j, la) * t
    s = matrix.sum()
    if s > 0:
        matrix /= s
    return matrix


def _markets_from_matrix(m: np.ndarray) -> dict[str, float]:
    """
    Markt-Wahrscheinlichkeiten aus Score-Matrix m[home_goals, away_goals]:
      - p_home / p_draw / p_away (1X2)
      - p_ah_home_plus_0_5  (Home gewinnt oder Unentschieden)
      - p_ah_away_plus_0_5  (Away gewinnt oder Unentschieden = 1 - p_home)
      - p_over_2_5 / p_under_2_5
      - p_btts_yes / p_btts_no
    """
    n = m.shape[0]
    p_home = float(np.tril(m, -1).sum())
    p_draw = float(np.trace(m))
    p_away = float(np.triu(m, 1).sum())

    over_2_5 = 0.0
    btts_yes = 0.0
    for i in range(n):
        for j in range(n):
            if i + j >= 3:
                over_2_5 += m[i, j]
            if i >= 1 and j >= 1:
                btts_yes += m[i, j]

    return {
        "p_home": p_home,
        "p_draw": p_draw,
        "p_away": p_away,
        "p_ah_home_plus_0_5": p_home + p_draw,
        "p_ah_away_plus_0_5": p_away + p_draw,
        "p_over_2_5": float(over_2_5),
        "p_under_2_5": float(1.0 - over_2_5),
        "p_btts_yes": float(btts_yes),
        "p_btts_no": float(1.0 - btts_yes),
    }


def _outcome_flags(hg: int, ag: int) -> dict[str, int]:
    return {
        "y_home":  int(hg > ag),
        "y_draw":  int(hg == ag),
        "y_away":  int(hg < ag),
        "y_ah_home_plus_0_5": int(hg >= ag),       # Home gewinnt oder Unentschieden
        "y_ah_away_plus_0_5": int(ag >= hg),
        "y_over_2_5":  int(hg + ag >= 3),
        "y_under_2_5": int(hg + ag <= 2),
        "y_btts_yes":  int(hg >= 1 and ag >= 1),
        "y_btts_no":   int(hg == 0 or ag == 0),
    }


def _brier_binary(probs: list[float], outcomes: list[int]) -> float:
    if not probs:
        return float("nan")
    p = np.array(probs)
    y = np.array(outcomes)
    return float(np.mean((p - y) ** 2))


def _roi_self_priced(
    probs: list[float],
    outcomes: list[int],
    threshold: float = 0.5,
    min_p: float = 0.10,
    max_p: float = 0.90,
) -> tuple[float, int]:
    """
    Self-priced ROI: wir wetten genau dann, wenn das Modell p>threshold sagt
    UND p im plausiblen Bereich liegt. Quote = 1/p. Stake = 1 Unit pro Bet.
    Returns (roi, n_bets).
    """
    p = np.array(probs)
    y = np.array(outcomes)
    mask = (p > threshold) & (p >= min_p) & (p <= max_p)
    if not mask.any():
        return 0.0, 0
    odds = 1.0 / p[mask]
    won = y[mask] == 1
    pnl = np.where(won, odds - 1.0, -1.0)
    return float(pnl.mean()), int(mask.sum())


def _market_metrics(records: list[dict]) -> dict[str, dict]:
    """Aggregiert pro Markt: Brier + ROI für Baseline und PPDA-adjusted."""
    markets = [
        ("1X2_home",        "p_home",             "y_home",             0.40),
        ("1X2_draw",        "p_draw",             "y_draw",             0.35),
        ("1X2_away",        "p_away",             "y_away",             0.40),
        ("ah_home_plus_0_5","p_ah_home_plus_0_5", "y_ah_home_plus_0_5", 0.55),
        ("ah_away_plus_0_5","p_ah_away_plus_0_5", "y_ah_away_plus_0_5", 0.55),
        ("over_2_5",        "p_over_2_5",         "y_over_2_5",         0.55),
        ("under_2_5",       "p_under_2_5",        "y_under_2_5",        0.55),
        ("btts_yes",        "p_btts_yes",         "y_btts_yes",         0.55),
        ("btts_no",         "p_btts_no",          "y_btts_no",          0.55),
    ]

    out: dict[str, dict] = {}
    for name, pkey, ykey, thr in markets:
        base_p = [r["base"][pkey] for r in records]
        adj_p  = [r["adj"][pkey]  for r in records]
        ys     = [r["y"][ykey]    for r in records]
        b_brier = _brier_binary(base_p, ys)
        a_brier = _brier_binary(adj_p,  ys)
        b_roi, b_n = _roi_self_priced(base_p, ys, threshold=thr)
        a_roi, a_n = _roi_self_priced(adj_p,  ys, threshold=thr)
        out[name] = {
            "baseline_brier": b_brier,
            "ppda_brier": a_brier,
            "delta_brier": b_brier - a_brier,
            "baseline_roi": b_roi,
            "ppda_roi": a_roi,
            "delta_roi_pp": (a_roi - b_roi) * 100.0,
            "baseline_n_bets": b_n,
            "ppda_n_bets": a_n,
        }
    return out


# ----------------------------- Main -----------------------------

def main(since: str = "2018-01-01", val_since: str = "2023-01-01") -> int:
    print("Lade Daten...")
    all_matches = filter_competitive(fetch_international_results())
    matches = filter_since(all_matches, since)
    print(f"  {len(matches)} Matches seit {since}")

    print("DC-Snapshot...")
    params = _load_dc_params()
    known_teams = set(params.attack.keys())

    print("StatsBomb-PPDA...")
    ppda_df = _load_ppda_df()
    if ppda_df is None:
        print("⚠️  Kein PPDA — Abbruch.")
        return 1
    print(f"  {len(ppda_df)} PPDA-Matches geladen.")

    val_cutoff = pd.Timestamp(val_since)
    val_matches = matches[matches["date"] >= val_cutoff].copy()
    print(f"  Val-Set: {len(val_matches)} Matches (seit {val_since})")

    records: list[dict] = []
    skipped = 0
    for _, row in val_matches.iterrows():
        home = row["home_team"]
        away = row["away_team"]
        if home not in known_teams or away not in known_teams:
            skipped += 1
            continue
        match_date = row["date"]
        neutral = bool(row.get("neutral", False))

        try:
            lh, la = dc._lambdas(home, away, params, neutral=neutral)
        except Exception:
            skipped += 1
            continue
        rho = params.rho

        # Baseline-Markt-Wahrscheinlichkeiten
        m_base = _scoreline_from_lambdas(lh, la, rho)
        base_markets = _markets_from_matrix(m_base)

        # PPDA-adjustierte Lambdas
        ppda_h = team_rolling_ppda(home, match_date, ppda_df)
        ppda_a = team_rolling_ppda(away, match_date, ppda_df)
        mh, ma = ppda_lambda_multipliers(ppda_h, ppda_a)
        m_adj = _scoreline_from_lambdas(lh * mh, la * ma, rho)
        adj_markets = _markets_from_matrix(m_adj)

        hg = int(row["home_score"])
        ag = int(row["away_score"])
        records.append({
            "base": base_markets,
            "adj":  adj_markets,
            "y":    _outcome_flags(hg, ag),
            "ppda_h": ppda_h,
            "ppda_a": ppda_a,
            "lambda_mult_h": mh,
            "lambda_mult_a": ma,
        })

    print(f"  Auswertbare Matches: {len(records)}  (übersprungen: {skipped})")
    if not records:
        print("⚠️  Keine auswertbaren Matches — Abbruch.")
        return 1

    market_results = _market_metrics(records)
    avg_mult_h = float(np.mean([r["lambda_mult_h"] for r in records]))
    avg_mult_a = float(np.mean([r["lambda_mult_a"] for r in records]))

    summary = {
        "as_of": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "since": since,
        "val_since": val_since,
        "n_matches": len(records),
        "n_skipped": skipped,
        "avg_lambda_mult_home": avg_mult_h,
        "avg_lambda_mult_away": avg_mult_a,
        "markets": market_results,
        "notes": [
            "Brier: kleiner = besser. ROI: self-priced (Quote=1/p), nicht market-aware.",
            "1X2-LGBM-Backtest separat in scripts/backtest_with_ppda.py.",
            "Scorer-Backtest noch nicht implementiert (per-Player-xG-Pfad fehlt).",
        ],
    }

    out_dir = RESULTS_DIR / "audits"
    out_dir.mkdir(parents=True, exist_ok=True)
    today = _dt.date.today().isoformat()
    out_path = out_dir / f"g1_ppda_markets_{today}.json"
    out_path.write_text(json.dumps(summary, indent=2))

    print("\n" + "=" * 78)
    print(f"G1 PPDA × Märkte — {len(records)} Matches  (avg λ-mult: H={avg_mult_h:.4f}, A={avg_mult_a:.4f})")
    print("=" * 78)
    hdr = f"{'Markt':22} {'Brier-base':>10} {'Brier-PPDA':>10} {'ΔBrier':>10} {'ROI-base':>10} {'ROI-PPDA':>10} {'ΔROI':>10}"
    print(hdr)
    print("-" * len(hdr))
    for name, m in market_results.items():
        print(
            f"{name:22} "
            f"{m['baseline_brier']:>10.4f} "
            f"{m['ppda_brier']:>10.4f} "
            f"{m['delta_brier']:>+10.4f} "
            f"{m['baseline_roi']:>+10.4f} "
            f"{m['ppda_roi']:>+10.4f} "
            f"{m['delta_roi_pp']:>+9.2f}pp"
        )
    print(f"\nBericht: {out_path.relative_to(Path.cwd())}")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2018-01-01")
    ap.add_argument("--val-since", default="2023-01-01")
    args = ap.parse_args()
    raise SystemExit(main(since=args.since, val_since=args.val_since))
