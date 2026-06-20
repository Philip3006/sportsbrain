"""
Roadmap G1 Gate: PPDA-Shadow-Feature Backtest.

Trainiert LightGBM zweimal auf identischem Train/Val-Split:
  Run A: ohne PPDA-Features (Baseline)
  Run B: mit PPDA-Features (force_ppda=True)

Misst Brier-Score und einen simplen Tournament-ROI auf dem Val-Set. Gate für
I5 (Live-Scharfschaltung):
    Brier-Improvement >= 0.001  UND  ROI-Improvement >= 0.5pp

Scope laut User-Festlegung Phase 4:
  Tournament + Friendly + Nations League + Liga.
Wir trainieren wie `scripts/train_lgbm.py` (filter_competitive lässt das durch),
nutzen aber `--since` zur Größen-Kontrolle des Backtests.

Output:
  results/audits/g1_ppda_backtest_<date>.json
  + Konsolen-Zusammenfassung.
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
from src.features.builder import build_training_matrix
from src.models import dixon_coles as dc
from src.models.elo import compute_elo_series
from src.models import lgbm_model
from src.ensemble.calibration import (
    brier_score_multiclass,
    calibrate,
    fit_isotonic,
)


def _load_dc_params():
    snap_dir = MODELS_DIR / "dixon_coles"
    snaps = sorted(snap_dir.glob("params_*.pkl"))
    if not snaps:
        raise RuntimeError("Kein DC-Snapshot — bitte zuerst train_dixon_coles.py laufen lassen.")
    return dc.load(snaps[-1])


def _load_ppda_df() -> pd.DataFrame | None:
    try:
        from src.data.statsbomb_ppda import fetch_statsbomb_ppda
        ppda = fetch_statsbomb_ppda()
        if ppda is None or ppda.empty:
            print("  Warnung: StatsBomb-PPDA leer — Backtest würde gegen reine Prior-Werte laufen.")
            return None
        print(f"  StatsBomb-PPDA: {len(ppda)} Matches geladen.")
        return ppda
    except Exception as e:
        print(f"  Warnung: PPDA-Fetch fehlgeschlagen ({e}) — Backtest abgebrochen.")
        return None


def _train_and_eval(
    X: pd.DataFrame,
    y: pd.Series,
    train_mask: np.ndarray,
    val_mask: np.ndarray,
    label: str,
) -> dict[str, float]:
    X_train, y_train = X[train_mask].fillna(0.0), y[train_mask]
    X_val, y_val = X[val_mask].fillna(0.0), y[val_mask]
    print(f"\n  [{label}] Features={X.shape[1]} · Train={len(X_train)} · Val={len(X_val)}")

    model = lgbm_model.train(
        X_train, y_train,
        eval_set=(X_val, y_val),
        early_stopping_rounds=50,
    )
    val_probs = lgbm_model.predict_proba(model, X_val)
    brier_raw = brier_score_multiclass(val_probs, y_val.values)

    cals = [fit_isotonic(val_probs, y_val.values, i) for i in range(3)]
    cal_probs = calibrate(val_probs, cals)
    brier_cal = brier_score_multiclass(cal_probs, y_val.values)

    # Vereinfachter ROI-Proxy: argmax-Pick auf Val mit fixem €10-Stake, decimal odds
    # angenähert über die kalibrierte Modell-Probability (1/p). Dies ist nicht
    # market-aware, gibt aber ein *konsistentes* Vergleichsmaß zwischen Run A/B.
    picks = np.argmax(cal_probs, axis=1)
    correct = (picks == y_val.values).astype(int)
    inv_p = 1.0 / np.clip(cal_probs[np.arange(len(picks)), picks], 0.05, 0.95)
    pnl = correct * (inv_p - 1.0) - (1 - correct) * 1.0
    roi = float(pnl.mean()) if len(pnl) else 0.0

    return {
        "brier_raw": float(brier_raw),
        "brier_cal": float(brier_cal),
        "roi_proxy": roi,
        "n_val": int(len(X_val)),
        "n_features": int(X.shape[1]),
    }


def main(since: str = "2018-01-01", val_since: str = "2023-01-01") -> int:
    print("Lade Daten...")
    all_matches = filter_competitive(fetch_international_results())
    matches = filter_since(all_matches, since)
    print(f"  {len(matches)} Matches seit {since}")

    print("Elo-Series...")
    elo_series = compute_elo_series(all_matches)

    print("DC-Snapshot...")
    dc_params = _load_dc_params()
    dc_snapshot_map = {pd.Timestamp("2000-01-01"): dc_params}

    print("StatsBomb-PPDA...")
    ppda_df = _load_ppda_df()
    if ppda_df is None:
        print("⚠️  Abbruch ohne PPDA-Daten.")
        return 1

    val_cutoff = pd.Timestamp(val_since)
    train_mask = (matches["date"].values < val_cutoff)
    val_mask = (matches["date"].values >= val_cutoff)

    print("\nRun A — Baseline (ohne PPDA)...")
    X_a, y = build_training_matrix(matches, all_matches, elo_series, dc_snapshot_map)
    res_a = _train_and_eval(X_a, y, train_mask, val_mask, label="A: no-PPDA")

    print("\nRun B — mit PPDA-Shadow-Feature...")
    X_b, _ = build_training_matrix(
        matches, all_matches, elo_series, dc_snapshot_map,
        ppda_df=ppda_df, force_ppda=True,
    )
    res_b = _train_and_eval(X_b, y, train_mask, val_mask, label="B: +PPDA")

    brier_improvement = res_a["brier_cal"] - res_b["brier_cal"]
    roi_improvement = res_b["roi_proxy"] - res_a["roi_proxy"]
    gate_brier = brier_improvement >= 0.001
    gate_roi = roi_improvement >= 0.005
    gate = gate_brier and gate_roi

    summary = {
        "as_of": _dt.datetime.utcnow().isoformat() + "Z",
        "since": since,
        "val_since": val_since,
        "baseline": res_a,
        "with_ppda": res_b,
        "brier_improvement": brier_improvement,
        "roi_improvement_pp": roi_improvement * 100.0,
        "gate_brier_passed": gate_brier,
        "gate_roi_passed": gate_roi,
        "i5_gate_passed": gate,
    }

    out_dir = RESULTS_DIR / "audits"
    out_dir.mkdir(parents=True, exist_ok=True)
    today = _dt.date.today().isoformat()
    out_path = out_dir / f"g1_ppda_backtest_{today}.json"
    out_path.write_text(json.dumps(summary, indent=2))

    print("\n" + "=" * 60)
    print("G1 PPDA Backtest — Zusammenfassung")
    print("=" * 60)
    print(f"  Brier (Baseline):  {res_a['brier_cal']:.4f}")
    print(f"  Brier (+PPDA):     {res_b['brier_cal']:.4f}")
    print(f"  Δ Brier:           {brier_improvement:+.4f}   "
          f"{'✅' if gate_brier else '⚠️ '} Gate ≥ 0.001")
    print(f"  ROI-Proxy Baseline:{res_a['roi_proxy']:+.4f}")
    print(f"  ROI-Proxy +PPDA:   {res_b['roi_proxy']:+.4f}")
    print(f"  Δ ROI:             {roi_improvement * 100:+.2f}pp "
          f"{'✅' if gate_roi else '⚠️ '} Gate ≥ 0.5pp")
    print(f"\n  I5-Gate (Live-Scharfschaltung): "
          f"{'✅ BESTANDEN' if gate else '⚠️  NICHT BESTANDEN — Shadow bleibt aktiv'}")
    print(f"\nBericht: {out_path.relative_to(Path.cwd())}")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2018-01-01")
    ap.add_argument("--val-since", default="2023-01-01")
    args = ap.parse_args()
    raise SystemExit(main(since=args.since, val_since=args.val_since))
