# WM 2026 Model Snapshot (Roadmap I2)

Eingefroren am **2026-07-31** nach Turnier-Ende (2026-07-19).

## Zweck

Kanonisches Referenzmodell für spätere Vergleiche:
- WM-Modell vs. Liga-Modell (Bundesliga-Start 2026-08-15)
- I3-Retrain-Verifikation (Brier-Gate)
- Post-Mortem-Analysen (I8 Market-Feedback, J4 CONMEBOL-Bias)

## Inhalt

| Datei | Quelle | Beschreibung |
|-------|--------|--------------|
| `dc_params_final.pkl` | `models/dixon_coles/params_20260720.pkl` | Letzte DC-Params vor WM-Finale |
| `dc_current_elo.json` | `models/dixon_coles/current_elo.json` | Elo-Rating aller Teams zum Turnier-Ende |
| `dc_lifecycle.json` | `models/dixon_coles/lifecycle.json` | Training-Historie |
| `model.pkl` | `models/lgbm/model.pkl` | LightGBM 1X2-Modell |
| `stacker.pkl` | `models/lgbm/stacker.pkl` | Meta-Stacker (DC+LGBM+Elo) |
| `calibrators.pkl` | `models/lgbm/calibrators.pkl` | Isotonic-Calibrators pro Klasse |
| `cluster_calibrators.pkl` | `models/lgbm/cluster_calibrators.pkl` | Per-Konföderation Isotonic |
| `conformal.pkl` | `models/lgbm/conformal.pkl` | Conformal-Prediction-Kalibrierung |
| `feature_columns.json` | `models/lgbm/feature_columns.json` | 88 Features (Spalten-Order) |
| `stacker_features.json` | `models/lgbm/stacker_features.json` | Stacker-Input-Schema |
| `gate.json` | `models/lgbm/gate.json` | Live-Gate-Status (Brier, ROI) |
| `anchor.json` | `models/lgbm/anchor.json` | Kalibrierungs-Anker (Baseline-Snapshot) |
| `metadata.json` | (generiert) | Ledger-Metriken + WM-Aggregat |

## Rollback

Wenn I3-Retrain schlechter performt:
```bash
cp models/snapshots/wm2026/dc_params_final.pkl models/dixon_coles/params_$(date +%Y%m%d).pkl
cp models/snapshots/wm2026/model.pkl models/lgbm/model.pkl
cp models/snapshots/wm2026/stacker.pkl models/lgbm/stacker.pkl
cp models/snapshots/wm2026/calibrators.pkl models/lgbm/calibrators.pkl
cp models/snapshots/wm2026/cluster_calibrators.pkl models/lgbm/cluster_calibrators.pkl
```

## Metriken (aus metadata.json)

Siehe `metadata.json` für Ledger-Aggregate, Brier-Score, ROI, CLV-Mean.
