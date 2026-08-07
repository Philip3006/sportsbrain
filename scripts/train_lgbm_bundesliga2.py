"""Phase C2 — LGBM-Ensemble für 2. Bundesliga.

Trainiert LightGBM über 10-Saison D2-CSV mit Walk-Forward-CV (5 Folds).
Features: DC-Attack/Defense-Parameter + Elo-Diff + Rolling-Form + H2H + Restdays.
Gate: Blend muss DC-only Brier um ≥ 1pp verbessern. Bei Fehlschlag --force-persist Flag.

Speichert:
  models/lgbm_bundesliga2/model.pkl
  models/lgbm_bundesliga2/calibrators.pkl   (Isotonic je Outcome)
  models/lgbm_bundesliga2/gate.json
  models/lgbm_bundesliga2/feature_columns.json

Usage:
  python scripts/train_lgbm_bundesliga2.py
  python scripts/train_lgbm_bundesliga2.py --force-persist   # auch bei Gate-Fail speichern
  python scripts/train_lgbm_bundesliga2.py --seasons 2122,2223,2324
"""
from __future__ import annotations

import argparse
import json
import pickle
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from src.config import DATA_CACHE, MODELS_DIR, canonical_name
from src.models import dixon_coles
from src.models.elo import compute_elo_series, ELO_DEFAULT

_OUT_DIR = MODELS_DIR / "lgbm_bundesliga2"
_SEASONS_DEFAULT = ["1617", "1718", "1819", "1920", "2021", "2122", "2223", "2324", "2425", "2526"]


def _load_history(seasons: list[str]) -> pd.DataFrame:
    from src.data.football_data import fetch_season
    frames = []
    for s in seasons:
        try:
            df = fetch_season("D2", s)
            if df is not None and not df.empty:
                df["season"] = s
                frames.append(df)
                print(f"  D2 {s}: {len(df)} matches")
        except Exception as exc:
            print(f"  WARN D2 {s}: {exc}")
    if not frames:
        raise RuntimeError("Keine D2-Daten geladen — train_lgbm_bundesliga2.py abgebrochen")
    return pd.concat(frames, ignore_index=True).sort_values("date").reset_index(drop=True)


def _rolling_form(df: pd.DataFrame, team: str, before_date: pd.Timestamp, n: int = 6) -> float:
    """Punkte pro Spiel in letzten n Matches vor Datum."""
    mask = ((df["home_team"] == team) | (df["away_team"] == team)) & (df["date"] < before_date)
    recent = df[mask].tail(n)
    if recent.empty:
        return 1.0  # neutral default
    pts = []
    for _, r in recent.iterrows():
        if r["home_team"] == team:
            if r["home_score"] > r["away_score"]:
                pts.append(3.0)
            elif r["home_score"] == r["away_score"]:
                pts.append(1.0)
            else:
                pts.append(0.0)
        else:
            if r["away_score"] > r["home_score"]:
                pts.append(3.0)
            elif r["away_score"] == r["home_score"]:
                pts.append(1.0)
            else:
                pts.append(0.0)
    return float(np.mean(pts))


def _goals_avg(df: pd.DataFrame, team: str, before_date: pd.Timestamp,
               side: str, n: int = 5) -> float:
    """Durchschnittliche Tore geschossen (side='for') oder kassiert (side='against') in letzten n Spielen."""
    mask = ((df["home_team"] == team) | (df["away_team"] == team)) & (df["date"] < before_date)
    recent = df[mask].tail(n)
    if recent.empty:
        return 1.3  # Liga-Ø ca. 1.3 Tore/Spiel
    goals = []
    for _, r in recent.iterrows():
        is_home = r["home_team"] == team
        if side == "for":
            goals.append(float(r["home_score"] if is_home else r["away_score"]))
        else:
            goals.append(float(r["away_score"] if is_home else r["home_score"]))
    return float(np.mean(goals))


def _venue_form(df: pd.DataFrame, team: str, before_date: pd.Timestamp,
                venue: str, n: int = 5) -> float:
    """Form nur in Heim- (venue='home') oder Auswärtsspielen (venue='away')."""
    if venue == "home":
        mask = (df["home_team"] == team) & (df["date"] < before_date)
    else:
        mask = (df["away_team"] == team) & (df["date"] < before_date)
    recent = df[mask].tail(n)
    if recent.empty:
        return 1.0
    pts = []
    for _, r in recent.iterrows():
        if venue == "home":
            pts.append(3.0 if r["home_score"] > r["away_score"]
                       else (1.0 if r["home_score"] == r["away_score"] else 0.0))
        else:
            pts.append(3.0 if r["away_score"] > r["home_score"]
                       else (1.0 if r["away_score"] == r["home_score"] else 0.0))
    return float(np.mean(pts))


def _home_win_rate(df: pd.DataFrame, team: str, before_date: pd.Timestamp, n: int = 10) -> float:
    """Heimsiegrate (Anteil Siege) in letzten n Heimspielen."""
    mask = (df["home_team"] == team) & (df["date"] < before_date)
    recent = df[mask].tail(n)
    if recent.empty:
        return 0.4
    wins = (recent["home_score"] > recent["away_score"]).sum()
    return float(wins / len(recent))


def _h2h(df: pd.DataFrame, home: str, away: str, before_date: pd.Timestamp, n: int = 5) -> float:
    """Home-Winrate in letzten n H2H-Matches."""
    mask = (
        ((df["home_team"] == home) & (df["away_team"] == away)) |
        ((df["home_team"] == away) & (df["away_team"] == home))
    ) & (df["date"] < before_date)
    recent = df[mask].tail(n)
    if recent.empty:
        return 0.4
    wins = sum(
        1 for _, r in recent.iterrows()
        if (r["home_team"] == home and r["home_score"] > r["away_score"]) or
           (r["home_team"] == away and r["away_score"] > r["home_score"])
    )
    return wins / len(recent)


def _days_rest(df: pd.DataFrame, team: str, before_date: pd.Timestamp) -> float:
    mask = ((df["home_team"] == team) | (df["away_team"] == team)) & (df["date"] < before_date)
    prev = df[mask]
    if prev.empty:
        return 14.0
    last = pd.Timestamp(prev["date"].max())
    return float((before_date - last).days)


def _build_features(
    row: pd.Series,
    df_history: pd.DataFrame,
    dc_params,
    elo_series: pd.DataFrame,
) -> dict | None:
    home = str(row["home_team"])
    away = str(row["away_team"])
    date = pd.Timestamp(row["date"])

    if home not in dc_params.attack or away not in dc_params.attack:
        return None

    # DC features
    dc_probs = dixon_coles.predict_match(home, away, dc_params)
    feat: dict = {
        "dc_p_home": dc_probs["p_home"],
        "dc_p_draw": dc_probs["p_draw"],
        "dc_p_away": dc_probs["p_away"],
        "dc_attack_home": dc_params.attack.get(home, 1.0),
        "dc_defence_home": dc_params.defence.get(home, 1.0),
        "dc_attack_away": dc_params.attack.get(away, 1.0),
        "dc_defence_away": dc_params.defence.get(away, 1.0),
        "dc_lambda_home": dc_params.attack.get(home, 1.0) * dc_params.defence.get(away, 1.0),
        "dc_lambda_away": dc_params.attack.get(away, 1.0) * dc_params.defence.get(home, 1.0),
    }

    # Elo features
    elo_snap = elo_series[elo_series["date"] < date] if "date" in elo_series.columns else elo_series
    elo_h = ELO_DEFAULT
    elo_a = ELO_DEFAULT
    if not elo_snap.empty:
        for col_h in [home, f"{home}_elo"]:
            if col_h in elo_snap.columns:
                vals = elo_snap[col_h].dropna()
                if not vals.empty:
                    elo_h = float(vals.iloc[-1])
                    break
        for col_a in [away, f"{away}_elo"]:
            if col_a in elo_snap.columns:
                vals = elo_snap[col_a].dropna()
                if not vals.empty:
                    elo_a = float(vals.iloc[-1])
                    break
    feat["elo_home"] = elo_h
    feat["elo_away"] = elo_a
    feat["elo_diff"] = elo_h - elo_a

    # Form
    feat["form_home"] = _rolling_form(df_history, home, date, 6)
    feat["form_away"] = _rolling_form(df_history, away, date, 6)
    feat["form_diff"] = feat["form_home"] - feat["form_away"]

    # H2H
    feat["h2h_home_wr"] = _h2h(df_history, home, away, date, 5)

    # Rest days
    feat["rest_home"] = _days_rest(df_history, home, date)
    feat["rest_away"] = _days_rest(df_history, away, date)
    feat["rest_diff"] = feat["rest_home"] - feat["rest_away"]

    # Venue-Form & Momentum
    feat["home_venue_form"]    = _venue_form(df_history, home, date, "home", 5)
    feat["away_venue_form"]    = _venue_form(df_history, away, date, "away", 5)
    feat["momentum_home"]      = _rolling_form(df_history, home, date, 3) - _rolling_form(df_history, home, date, 6)
    feat["momentum_away"]      = _rolling_form(df_history, away, date, 3) - _rolling_form(df_history, away, date, 6)
    feat["home_win_rate_home"] = _home_win_rate(df_history, home, date, 10)

    # Spielerform-Proxy: Tore geschossen/kassiert (letzte 5 Spiele)
    feat["goals_scored_5_home"]    = _goals_avg(df_history, home, date, "for",     5)
    feat["goals_conceded_5_home"]  = _goals_avg(df_history, home, date, "against", 5)
    feat["goals_scored_5_away"]    = _goals_avg(df_history, away, date, "for",     5)
    feat["goals_conceded_5_away"]  = _goals_avg(df_history, away, date, "against", 5)

    # Heimzuschauer-Heimvorteil
    from src.data.attendance import get_attendance_ratio
    feat["home_attendance_ratio"]  = get_attendance_ratio(home)

    # Kaderwert (Transfermarkt squad market values — wichtiger Stärkeindikator)
    from src.data.market_values import get_market_value_ratio, get_market_value_log_ratio
    feat["market_value_ratio"]     = get_market_value_ratio(home, away)
    feat["market_value_log_ratio"] = get_market_value_log_ratio(home, away)

    return feat


def _label(row: pd.Series) -> int:
    """0=Away, 1=Draw, 2=Home."""
    h, a = int(row["home_score"]), int(row["away_score"])
    if h > a:
        return 2
    if h == a:
        return 1
    return 0


def _brier_multiclass(y_true: np.ndarray, probs: np.ndarray) -> float:
    n = len(y_true)
    total = 0.0
    for i in range(n):
        for k in range(3):
            total += (probs[i, k] - (1 if y_true[i] == k else 0)) ** 2
    return total / n


def main(seasons: list[str] = _SEASONS_DEFAULT, force_persist: bool = False) -> None:
    print("=== 2.BL LGBM Training ===")
    _OUT_DIR.mkdir(parents=True, exist_ok=True)

    df_all = _load_history(seasons)
    print(f"Total: {len(df_all)} matches über {df_all['season'].nunique()} Saisons")

    # Elo-Series aus Saison-Daten
    print("Computing Elo...")
    try:
        elo_series = compute_elo_series(df_all)
    except Exception as exc:
        print(f"  Elo-Series Fehler: {exc} — verwende Default")
        elo_series = pd.DataFrame()

    # DC-Params laden
    dc_path = MODELS_DIR / "dc_bundesliga2" / "params_latest.pkl"
    if not dc_path.exists():
        raise RuntimeError("DC-Modell fehlt — erst train_dc_bundesliga2.py")
    dc_params = dixon_coles.load(dc_path)

    # Feature-Matrix bauen
    print("Building feature matrix...")
    rows, labels = [], []
    for _, r in df_all.iterrows():
        if pd.isna(r.get("home_score")) or pd.isna(r.get("away_score")):
            continue
        feat = _build_features(r, df_all, dc_params, elo_series)
        if feat is None:
            continue
        rows.append(feat)
        labels.append(_label(r))

    X = pd.DataFrame(rows).fillna(0.0)
    y = np.array(labels)
    feature_cols = list(X.columns)
    print(f"Feature matrix: {len(X)} rows × {len(feature_cols)} features")

    if len(X) < 100:
        print("WARN: Zu wenige Trainings-Samples (<100) — Training abgebrochen")
        return

    # Walk-Forward CV (5 Folds)
    print("Walk-forward CV...")
    try:
        import lightgbm as lgb
    except ImportError:
        print("FEHLER: lightgbm nicht installiert — pip install lightgbm")
        return

    n = len(X)
    fold_size = n // 5
    dc_briers, blend_briers = [], []

    for fold in range(5):
        val_start = (fold + 1) * fold_size
        if val_start >= n:
            break
        val_end = min(val_start + fold_size, n)
        X_train = X.iloc[:val_start]
        y_train = y[:val_start]
        X_val = X.iloc[val_start:val_end]
        y_val = y[val_start:val_end]

        m = lgb.LGBMClassifier(
            n_estimators=200, learning_rate=0.05, num_leaves=31,
            min_child_samples=20, class_weight="balanced",
            objective="multiclass", num_class=3, n_jobs=1, verbose=-1,
        )
        m.fit(X_train, y_train)
        lgbm_probs = m.predict_proba(X_val)  # shape (n, 3): Away=0, Draw=1, Home=2

        # DC baseline Brier
        dc_probs_val = np.array([[
            X_val.iloc[i]["dc_p_away"],
            X_val.iloc[i]["dc_p_draw"],
            X_val.iloc[i]["dc_p_home"],
        ] for i in range(len(X_val))])
        # Blend 50/50 initial
        blend_probs = 0.5 * dc_probs_val + 0.5 * lgbm_probs

        dc_briers.append(_brier_multiclass(y_val, dc_probs_val))
        blend_briers.append(_brier_multiclass(y_val, blend_probs))

        print(f"  Fold {fold+1}: DC Brier={dc_briers[-1]:.4f} Blend Brier={blend_briers[-1]:.4f}")

    dc_brier_mean = float(np.mean(dc_briers))
    blend_brier_mean = float(np.mean(blend_briers))
    improvement = dc_brier_mean - blend_brier_mean
    gate_passed = improvement >= 0.01
    dc_weight = 0.5  # initial; refined after 30+ settled signals

    print(f"\nCV: DC={dc_brier_mean:.4f} Blend={blend_brier_mean:.4f} Δ={improvement:.4f}")
    print(f"Gate: {'PASS' if gate_passed else 'FAIL'} (min Δ=0.01, force={force_persist})")

    if not gate_passed and not force_persist:
        print("Gate nicht bestanden — kein Persist. Nutze --force-persist für Saison-1 Warmstart.")
        return

    # Finales Modell auf allen Daten
    print("Fitting final model on all data...")
    final_model = lgb.LGBMClassifier(
        n_estimators=300, learning_rate=0.05, num_leaves=31,
        min_child_samples=20, class_weight="balanced",
        objective="multiclass", num_class=3, n_jobs=1, verbose=-1,
    )
    final_model.fit(X, y)

    # Isotonic Calibrators (je Outcome-Klasse)
    from sklearn.isotonic import IsotonicRegression
    probs_all = final_model.predict_proba(X)
    calibrators = {}
    for k, outcome in enumerate(["away", "draw", "home"]):
        p_k = probs_all[:, k]
        actual_k = (y == k).astype(float)
        iso = IsotonicRegression(out_of_bounds="clip")
        iso.fit(p_k, actual_k)
        calibrators[outcome] = iso
    print("Calibrators: Isotonic (away/draw/home)")

    # Speichern
    with open(_OUT_DIR / "model.pkl", "wb") as f:
        pickle.dump(final_model, f)
    with open(_OUT_DIR / "calibrators.pkl", "wb") as f:
        pickle.dump(calibrators, f)
    with open(_OUT_DIR / "feature_columns.json", "w") as f:
        json.dump(feature_cols, f, indent=2)

    gate = {
        "dc_brier": dc_brier_mean,
        "blend_brier": blend_brier_mean,
        "improvement_vs_dc": improvement,
        "dc_weight": dc_weight,
        "min_improvement_required": 0.01,
        "gate_passed": gate_passed,
        "force_persist": force_persist,
        "n_train": len(X),
        "seasons": seasons,
        "trained_at": datetime.utcnow().isoformat(),
        "reason": (
            f"blend improves DC-only by {improvement:.4f} (≥0.01)" if gate_passed
            else f"blend Δ={improvement:.4f} < 0.01 — force_persist={force_persist}"
        ),
    }
    with open(_OUT_DIR / "gate.json", "w") as f:
        json.dump(gate, f, indent=2)

    print(f"\n✅ Gespeichert in {_OUT_DIR}/")
    print(f"   model.pkl | calibrators.pkl | gate.json | feature_columns.json")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--force-persist", action="store_true")
    ap.add_argument("--seasons", type=str, default=None,
                    help="Komma-separierte Saison-Codes, z.B. 2122,2223,2324")
    args = ap.parse_args()
    seasons = args.seasons.split(",") if args.seasons else _SEASONS_DEFAULT
    main(seasons=seasons, force_persist=args.force_persist)
