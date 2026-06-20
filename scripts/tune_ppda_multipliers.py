"""
G1 Sprint 1: Grid-Search über PPDA-Lambda-Multiplier-Parameter.

Sucht (z_scale, boost, clip), das bei minimaler Brier-Degradation maximalen
ROI-Gewinn auf den Schlüsselmärkten liefert.

Loss-Funktion (zu maximieren):
    score = Σ_market ROI_delta_pp - λ_brier * Σ_market max(Brier_delta, 0)

mit λ_brier=10 (Brier-Wert in Δ ≈ 0.001-Größenordnung, ROI-Δ in pp 0.5-2).

Output:
  results/audits/g1_ppda_tuning_<date>.json mit Top-10-Grid + Gewinner.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import time
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.stats import poisson

from src.config import MODELS_DIR, RESULTS_DIR
from src.data.international import fetch_international_results, filter_competitive, filter_since
from src.features.ppda import team_rolling_ppda
from src.models import dixon_coles as dc


# Märkte, gegen die wir bewerten (Übersicht aus Markt-Backtest 2026-06-21)
MARKETS = [
    ("1X2_home", "y_home", 0.40),
    ("1X2_draw", "y_draw", 0.35),
    ("1X2_away", "y_away", 0.40),
    ("over_2_5", "y_over_2_5", 0.55),
    ("under_2_5", "y_under_2_5", 0.55),
    ("btts_yes", "y_btts_yes", 0.55),
    ("btts_no", "y_btts_no", 0.55),
]


def _scoreline(lh: float, la: float, rho: float, n: int = 7) -> np.ndarray:
    m = np.zeros((n + 1, n + 1))
    for i in range(n + 1):
        for j in range(n + 1):
            t = dc._tau(i, j, lh, la, rho)
            m[i, j] = poisson.pmf(i, lh) * poisson.pmf(j, la) * t
    s = m.sum()
    if s > 0:
        m /= s
    return m


def _markets_from_matrix(m: np.ndarray) -> dict[str, float]:
    n = m.shape[0]
    p_home = float(np.tril(m, -1).sum())
    p_draw = float(np.trace(m))
    p_away = float(np.triu(m, 1).sum())
    over = btts = 0.0
    for i in range(n):
        for j in range(n):
            if i + j >= 3:
                over += m[i, j]
            if i >= 1 and j >= 1:
                btts += m[i, j]
    return {
        "1X2_home": p_home, "1X2_draw": p_draw, "1X2_away": p_away,
        "over_2_5": float(over), "under_2_5": float(1.0 - over),
        "btts_yes": float(btts), "btts_no": float(1.0 - btts),
    }


def _outcome_flags(hg: int, ag: int) -> dict[str, int]:
    return {
        "y_home":  int(hg > ag),
        "y_draw":  int(hg == ag),
        "y_away":  int(hg < ag),
        "y_over_2_5":  int(hg + ag >= 3),
        "y_under_2_5": int(hg + ag <= 2),
        "y_btts_yes":  int(hg >= 1 and ag >= 1),
        "y_btts_no":   int(hg == 0 or ag == 0),
    }


def _multipliers(ppda_h: float, ppda_a: float, baseline: float,
                 z_scale: float, boost: float, clip: float) -> tuple[float, float]:
    z_h = 0.0 if ppda_h != ppda_h else (baseline - ppda_h) / z_scale
    z_a = 0.0 if ppda_a != ppda_a else (baseline - ppda_a) / z_scale
    mh = float(min(max(1.0 + boost * z_h, 1.0 - clip), 1.0 + clip))
    ma = float(min(max(1.0 + boost * z_a, 1.0 - clip), 1.0 + clip))
    return mh, ma


def _evaluate(records: list[dict], thr_map: dict[str, float]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for name, ykey, thr in MARKETS:
        base_p = np.array([r["base"][name] for r in records])
        adj_p  = np.array([r["adj"][name]  for r in records])
        y      = np.array([r["y"][ykey]    for r in records])
        b_brier = float(np.mean((base_p - y) ** 2))
        a_brier = float(np.mean((adj_p  - y) ** 2))

        def _roi(p, y, t):
            mask = (p > t) & (p >= 0.10) & (p <= 0.90)
            if not mask.any():
                return 0.0, 0
            won = y[mask] == 1
            pnl = np.where(won, (1.0 / p[mask]) - 1.0, -1.0)
            return float(pnl.mean()), int(mask.sum())

        b_roi, b_n = _roi(base_p, y, thr)
        a_roi, a_n = _roi(adj_p,  y, thr)
        out[name] = {
            "brier_base": b_brier, "brier_adj": a_brier,
            "delta_brier": b_brier - a_brier,
            "roi_base": b_roi, "roi_adj": a_roi,
            "delta_roi_pp": (a_roi - b_roi) * 100.0,
            "n_bets_adj": a_n, "n_bets_base": b_n,
        }
    return out


def _score(market_results: dict[str, dict], brier_weight: float = 10.0) -> float:
    """Score zu maximieren: Σ ROI-Δ − brier_weight × Σ max(0, Brier-Degradation)."""
    roi_sum = sum(m["delta_roi_pp"] for m in market_results.values())
    brier_penalty = sum(max(0.0, -m["delta_brier"]) for m in market_results.values())
    return roi_sum - brier_weight * brier_penalty * 100.0  # brier-Δ skaliert (Größenordnung 0.001 → 0.1)


def main(since: str = "2018-01-01", val_since: str = "2023-01-01") -> int:
    print("Lade Daten...")
    all_matches = filter_competitive(fetch_international_results())
    matches = filter_since(all_matches, since)
    print(f"  {len(matches)} Matches seit {since}")

    snap_dir = MODELS_DIR / "dixon_coles"
    snaps = sorted(snap_dir.glob("params_*.pkl"))
    params = dc.load(snaps[-1])
    known_teams = set(params.attack.keys())

    from src.data.statsbomb_ppda import fetch_statsbomb_ppda
    ppda_df = fetch_statsbomb_ppda()
    if ppda_df is None or ppda_df.empty:
        print("⚠️  Kein PPDA-Cache. Abbruch.")
        return 1

    val_cutoff = pd.Timestamp(val_since)
    val_matches = matches[matches["date"] >= val_cutoff].copy()
    print(f"  Val-Set: {len(val_matches)} Matches")

    # Vorberechnung: pro Match Lambdas + PPDA + Baseline-Markets + Outcomes
    print("Vorberechnung (Baseline-Matrizen + PPDA)...")
    t0 = time.time()
    pre: list[dict] = []
    rho = params.rho
    for _, row in val_matches.iterrows():
        home, away = row["home_team"], row["away_team"]
        if home not in known_teams or away not in known_teams:
            continue
        try:
            lh, la = dc._lambdas(home, away, params, neutral=bool(row.get("neutral", False)))
        except Exception:
            continue
        base = _markets_from_matrix(_scoreline(lh, la, rho))
        ppda_h = team_rolling_ppda(home, row["date"], ppda_df)
        ppda_a = team_rolling_ppda(away, row["date"], ppda_df)
        pre.append({
            "lh": lh, "la": la,
            "base": base,
            "ppda_h": ppda_h, "ppda_a": ppda_a,
            "y": _outcome_flags(int(row["home_score"]), int(row["away_score"])),
        })
    print(f"  {len(pre)} Matches vorbereitet in {time.time() - t0:.1f}s")

    # Grid
    grid = [
        (z_scale, boost, clip)
        for z_scale in (3.0, 5.0, 8.0, 12.0)
        for boost in (0.010, 0.025, 0.050)
        for clip in (0.05, 0.10, 0.15)
    ]
    print(f"\nGrid: {len(grid)} Kombinationen")

    thr_map = {n: t for n, _, t in MARKETS}
    grid_results: list[dict] = []
    for k, (z, b, c) in enumerate(grid, 1):
        records = []
        for rec in pre:
            mh, ma = _multipliers(rec["ppda_h"], rec["ppda_a"],
                                  baseline=11.5, z_scale=z, boost=b, clip=c)
            adj = _markets_from_matrix(_scoreline(rec["lh"] * mh, rec["la"] * ma, rho))
            records.append({"base": rec["base"], "adj": adj, "y": rec["y"]})
        markets = _evaluate(records, thr_map)
        s = _score(markets)
        grid_results.append({
            "z_scale": z, "boost": b, "clip": c,
            "score": s,
            "markets": markets,
        })
        print(f"  [{k:>2}/{len(grid)}] z={z:>5.1f} boost={b:.3f} clip={c:.2f}  score={s:+.3f}")

    grid_results.sort(key=lambda r: r["score"], reverse=True)
    winner = grid_results[0]

    summary = {
        "as_of": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "n_matches": len(pre),
        "grid_size": len(grid),
        "winner": {k: winner[k] for k in ("z_scale", "boost", "clip", "score")},
        "winner_markets": winner["markets"],
        "top10": [
            {
                "z_scale": r["z_scale"], "boost": r["boost"], "clip": r["clip"],
                "score": r["score"],
                "delta_roi_sum_pp": sum(m["delta_roi_pp"] for m in r["markets"].values()),
                "delta_brier_sum": sum(m["delta_brier"] for m in r["markets"].values()),
            }
            for r in grid_results[:10]
        ],
        "notes": [
            "Score = Σ ROI-Δ (pp) − 10 × 100 × Σ max(0, Brier-Verschlechterung).",
            "Self-priced Quotes (Sprint 1 — markt-aware kommt in Sprint 2).",
            "Bei mehreren gleichwertigen: kleinstes boost bevorzugen (konservativer).",
        ],
    }

    out_dir = RESULTS_DIR / "audits"
    out_dir.mkdir(parents=True, exist_ok=True)
    today = _dt.date.today().isoformat()
    out_path = out_dir / f"g1_ppda_tuning_{today}.json"
    out_path.write_text(json.dumps(summary, indent=2))

    print("\n" + "=" * 70)
    print("WINNER")
    print("=" * 70)
    w = winner
    print(f"  z_scale={w['z_scale']}  boost={w['boost']}  clip={w['clip']}")
    print(f"  score={w['score']:+.3f}")
    print(f"\nPer-Markt (Winner):")
    for name, m in w["markets"].items():
        print(f"  {name:12} ΔBrier={m['delta_brier']:+.4f}  ΔROI={m['delta_roi_pp']:+.2f}pp")
    print(f"\nBericht: {out_path.relative_to(Path.cwd())}")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2018-01-01")
    ap.add_argument("--val-since", default="2023-01-01")
    args = ap.parse_args()
    raise SystemExit(main(since=args.since, val_since=args.val_since))
