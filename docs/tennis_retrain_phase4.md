# Tennis Retrain — Phase 4 Runbook

Nach den Feature-Erweiterungen (Phase 1–3 + Hebel 1/3) müssen Elo-Snapshot und LGBM-Modell neu gebaut werden, damit die neuen Signale ins Live-System einfließen.

## Reihenfolge

### 1. Elo-Snapshot (Historie 2010→2026)

Der Sackmann-Loader zieht jetzt 2010–2026 statt 2019–2026 (+ optional Challenger/ITF für Elo-Seeding).

```bash
# Standard (ATP+WTA Main-Tour)
python3 scripts/tennis_retrain.py

# Mit Challenger/ITF-Seeding (langsamer, ca. 3-5 Min):
python3 scripts/tennis_retrain.py --include-sub-tour   # noch nicht verdrahtet — TODO
```

**Erwartung:** ~450k–600k Rows, ~5–8k Spieler in overall-Pool. Top-10 vergleichen mit vorherigem Snapshot — Abweichung >30 Elo-Punkte auf Top-30 = Bug.

### 2. LGBM-Retrain (mit neuen Features)

```bash
python3 scripts/tennis_train.py
```

FEATURE_COLUMNS ist jetzt 64 Cols (vorher 41). `tennis_lgbm.load()` liest `feature_columns.json`, d.h. alter Snapshot bleibt kompatibel bis Retrain durch ist. Nach dem Retrain überschreibt der neue Snapshot Cols + Metadata.

**Gate:** Brier-Improvement ≥ 0.003 vs. Elo-Baseline. Bei Fail kein Persist — bisheriges Modell bleibt aktiv.

### 3. Backtest 2025-Sample

```bash
python3 scripts/tennis_full_backtest.py --year 2025
```

Ziel-Metriken:
- Brier ↓ ≥ 0.005 vs. vor Phase 4
- LogLoss ↓
- ROI +2pp auf ATP + WTA gemeinsam

### 4. Shadow-Live (2 Wochen)

Neue Predictions parallel zu alten loggen. Nur wenn Shadow-Bilanz positiv → Rollout.

## Was jetzt live wirkt (auch ohne Retrain)

- **Line-Movement / CLV Filter**: `src/tennis/line_movement.py` — Utility für Sharp-Money-Detection, nutzbar im Scanner via `line_move_confirms_edge()`
- **Style-Cluster**: `src/tennis/style_cluster.py` — rule-based, `style_matchup_edge()` als post-hoc Bias (±3pp)
- **In-Play Momentum**: `src/tennis/momentum.py` — für Live-Predictions Set 2+, `momentum_prob_adjustment()` mit cap ±5pp

Diese drei Module sind fertig einsetzbar unabhängig vom LGBM-Retrain — Integration in Live-Scanner ist eigener Schritt.
