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


def _build_odds_index(
    fuzzy_days: int = 7,
) -> list[dict]:
    """
    Sammelt Closing-1X2-Quoten aus den verfügbaren Quellen und gibt eine
    Liste von Einträgen zurück. Lookup im Backtest matcht (home, away) +
    Datum mit Toleranz `fuzzy_days` Tagen — verhindert, dass eine alte
    WM-Quote auf ein Re-Match Jahre später angewendet wird.
    """
    from src.data.football_data_intl import fetch_wc_odds
    from src.data.betexplorer import load_odds_lookup

    entries: list[dict] = []
    frames = []
    try:
        frames.append(fetch_wc_odds())
    except Exception as e:
        print(f"  Warnung: fetch_wc_odds() fehlgeschlagen ({e})")
    try:
        be = load_odds_lookup()
        if not be.empty:
            frames.append(be)
    except Exception:
        pass

    for df in frames:
        if df is None or df.empty:
            continue
        for _, r in df.iterrows():
            h = r.get("close_home") if pd.notna(r.get("close_home", float("nan"))) else r.get("home_odds")
            d = r.get("close_draw") if pd.notna(r.get("close_draw", float("nan"))) else r.get("draw_odds")
            a = r.get("close_away") if pd.notna(r.get("close_away", float("nan"))) else r.get("away_odds")
            try:
                ho, do, ao = float(h), float(d), float(a)
            except (TypeError, ValueError):
                continue
            if min(ho, do, ao) <= 1.0:
                continue
            try:
                dt = pd.to_datetime(r.get("date"), errors="coerce")
            except Exception:
                dt = pd.NaT
            entries.append({
                "home_team": str(r["home_team"]),
                "away_team": str(r["away_team"]),
                "date": dt,
                "odds": (ho, do, ao),
            })
    return entries


def _lookup_odds(
    entries: list[dict],
    home: str,
    away: str,
    match_date: pd.Timestamp,
    fuzzy_days: int = 7,
) -> tuple[float, float, float] | None:
    candidates = [e for e in entries if e["home_team"] == home and e["away_team"] == away]
    if not candidates:
        return None
    # Datum-Match bevorzugt (fuzzy), Fallback: bei NaT akzeptieren wir nur exact-team-match
    with_date = [e for e in candidates if pd.notna(e["date"])]
    if with_date and pd.notna(match_date):
        target = pd.Timestamp(match_date)
        within = [e for e in with_date if abs((e["date"] - target).days) <= fuzzy_days]
        if within:
            within.sort(key=lambda e: abs((e["date"] - target).days))
            return within[0]["odds"]
        return None  # Date verfügbar, aber kein Fenster-Match → ablehnen (kein Stale-Match)
    # Keine Datum-Info → akzeptiere nur, wenn Quelle eindeutig (1 Eintrag pro Team-Paar)
    if len(candidates) == 1:
        return candidates[0]["odds"]
    return None


def _market_aware_roi(
    cal_probs: np.ndarray,
    y_val: np.ndarray,
    val_matches: pd.DataFrame,
    odds_index: list[dict],
    min_edge: float = 0.03,
    stake: float = 1.0,
) -> dict[str, float]:
    """
    Markt-aware Edge-Betting: für jedes Val-Match × Outcome (home/draw/away) prüft
    edge = p_model × decimal_odds − 1; wenn > min_edge → €stake-Bet. Quote = echte
    Closing-1X2-Quote wo verfügbar, sonst Match übersprungen (kein Self-Pricing).

    cal_probs Spalten: [p_away, p_draw, p_home] (LGBM-Label-Order 0/1/2).
    """
    n_matches_with_odds = 0
    pnl_total = 0.0
    n_bets = 0
    for i in range(len(cal_probs)):
        row = val_matches.iloc[i]
        odds = _lookup_odds(odds_index, str(row["home_team"]), str(row["away_team"]), row["date"])
        if odds is None:
            continue
        n_matches_with_odds += 1
        h_o, d_o, a_o = odds
        p_away, p_draw, p_home = float(cal_probs[i, 0]), float(cal_probs[i, 1]), float(cal_probs[i, 2])
        for p, o, outcome in ((p_home, h_o, 2), (p_draw, d_o, 1), (p_away, a_o, 0)):
            if p * o - 1.0 > min_edge:
                won = int(y_val[i]) == outcome
                pnl_total += (o - 1.0) * stake if won else -stake
                n_bets += 1
    total_stake = n_bets * stake
    return {
        "roi_market": float(pnl_total / total_stake) if total_stake > 0 else 0.0,
        "n_bets_market": int(n_bets),
        "total_pnl": float(pnl_total),
        "n_matches_with_odds": int(n_matches_with_odds),
    }


def _train_and_eval(
    X: pd.DataFrame,
    y: pd.Series,
    train_mask: np.ndarray,
    val_mask: np.ndarray,
    label: str,
    val_matches: pd.DataFrame | None = None,
    odds_index: dict | None = None,
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

    # Self-priced ROI-Proxy (Vergleichbarkeit zwischen Runs)
    picks = np.argmax(cal_probs, axis=1)
    correct = (picks == y_val.values).astype(int)
    inv_p = 1.0 / np.clip(cal_probs[np.arange(len(picks)), picks], 0.05, 0.95)
    pnl = correct * (inv_p - 1.0) - (1 - correct) * 1.0
    roi_proxy = float(pnl.mean()) if len(pnl) else 0.0

    out = {
        "brier_raw": float(brier_raw),
        "brier_cal": float(brier_cal),
        "roi_proxy": roi_proxy,
        "n_val": int(len(X_val)),
        "n_features": int(X.shape[1]),
    }

    # Markt-aware Edge-Betting (Sprint 2): echte Closing-Quoten
    if val_matches is not None and odds_index:
        market = _market_aware_roi(cal_probs, y_val.values, val_matches, odds_index)
        out.update(market)
    return out


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
    val_matches = matches[val_mask].reset_index(drop=True)

    print("Closing-Odds-Index bauen (Sprint 2: market-aware ROI)...")
    odds_index = _build_odds_index()
    matched = sum(
        1 for _, r in val_matches.iterrows()
        if _lookup_odds(odds_index, str(r["home_team"]), str(r["away_team"]), r["date"]) is not None
    )
    print(f"  {len(odds_index)} Quoten-Einträge insgesamt · {matched} Val-Matches mit valider Closing-Quote")

    print("\nRun A — Baseline (ohne PPDA)...")
    X_a, y = build_training_matrix(matches, all_matches, elo_series, dc_snapshot_map)
    res_a = _train_and_eval(X_a, y, train_mask, val_mask, label="A: no-PPDA",
                            val_matches=val_matches, odds_index=odds_index)

    print("\nRun B — mit PPDA-Shadow-Feature...")
    X_b, _ = build_training_matrix(
        matches, all_matches, elo_series, dc_snapshot_map,
        ppda_df=ppda_df, force_ppda=True,
    )
    res_b = _train_and_eval(X_b, y, train_mask, val_mask, label="B: +PPDA",
                            val_matches=val_matches, odds_index=odds_index)

    brier_improvement = res_a["brier_cal"] - res_b["brier_cal"]
    roi_improvement = res_b["roi_proxy"] - res_a["roi_proxy"]
    market_roi_improvement = res_b.get("roi_market", 0.0) - res_a.get("roi_market", 0.0)
    gate_brier = brier_improvement >= 0.001
    gate_roi = roi_improvement >= 0.005
    gate_market_roi = market_roi_improvement >= 0.005
    gate = gate_brier and gate_market_roi  # I5-Gate jetzt gegen markt-awaren ROI

    summary = {
        "as_of": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "since": since,
        "val_since": val_since,
        "baseline": res_a,
        "with_ppda": res_b,
        "brier_improvement": brier_improvement,
        "roi_improvement_proxy_pp": roi_improvement * 100.0,
        "roi_improvement_market_pp": market_roi_improvement * 100.0,
        "gate_brier_passed": gate_brier,
        "gate_roi_proxy_passed": gate_roi,
        "gate_roi_market_passed": gate_market_roi,
        "i5_gate_passed": gate,
        "n_market_bets_baseline": res_a.get("n_bets_market", 0),
        "n_market_bets_ppda": res_b.get("n_bets_market", 0),
    }

    out_dir = RESULTS_DIR / "audits"
    out_dir.mkdir(parents=True, exist_ok=True)
    today = _dt.date.today().isoformat()
    out_path = out_dir / f"g1_ppda_backtest_{today}.json"
    out_path.write_text(json.dumps(summary, indent=2))

    print("\n" + "=" * 70)
    print("G1 PPDA Backtest — Zusammenfassung")
    print("=" * 70)
    print(f"  Brier (Baseline):     {res_a['brier_cal']:.4f}")
    print(f"  Brier (+PPDA):        {res_b['brier_cal']:.4f}")
    print(f"  Δ Brier:              {brier_improvement:+.4f}   "
          f"{'✅' if gate_brier else '⚠️ '} Gate ≥ 0.001")
    print(f"  ROI-Proxy Baseline:   {res_a['roi_proxy']:+.4f}")
    print(f"  ROI-Proxy +PPDA:      {res_b['roi_proxy']:+.4f}")
    print(f"  Δ ROI (Proxy):        {roi_improvement * 100:+.2f}pp"
          f" {'✅' if gate_roi else '⚠️ '} Gate ≥ 0.5pp")
    if res_a.get("n_bets_market"):
        print(f"  --- Markt-aware (echte Closing-Quoten) ---")
        print(f"  ROI Baseline:         {res_a['roi_market']:+.4f} "
              f"(n={res_a['n_bets_market']} Bets, edge>3%)")
        print(f"  ROI +PPDA:            {res_b['roi_market']:+.4f} "
              f"(n={res_b['n_bets_market']} Bets, edge>3%)")
        print(f"  Δ ROI (Markt):        {market_roi_improvement * 100:+.2f}pp"
              f" {'✅' if gate_market_roi else '⚠️ '} Gate ≥ 0.5pp")
    else:
        print("  Markt-aware ROI: keine passenden Closing-Quoten gefunden.")
    print(f"\n  I5-Gate (Brier ∧ Markt-ROI): "
          f"{'✅ BESTANDEN' if gate else '⚠️  NICHT BESTANDEN — Shadow bleibt aktiv'}")
    print(f"\nBericht: {out_path.relative_to(Path.cwd())}")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2018-01-01")
    ap.add_argument("--val-since", default="2023-01-01")
    args = ap.parse_args()
    raise SystemExit(main(since=args.since, val_since=args.val_since))
