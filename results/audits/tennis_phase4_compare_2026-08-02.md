# Tennis Phase-4 Before/After — 2026-08-02

**Was verglichen:** Elo-Walk-Forward-Backtest auf identisches Test-Fenster (2022–2025), einmal mit **7 Jahren Historie (2019–2025, 32.7k Matches)** und einmal mit **13 Jahren Historie (2013–2025, 66.7k Matches)** nach Phase-1-Backfill.

**Was NICHT verglichen:** LGBM-Modell (nutzt bisheriges 41-Col-Snapshot, neue 23 Features wirken erst nach Retrain), Style-Cluster, Momentum, Line-Movement (post-hoc/live-only Module ohne historic Playback-Daten).

---

## Match-Winner Kategorie-Deltas

| Kategorie              | N (b→a)     | ROI b   | ROI a   | Δ ROI    | Brier b | Brier a | Verdict-Δ         |
|------------------------|-------------|--------:|--------:|---------:|--------:|--------:|-------------------|
| atp250/ATP/clay        | 526→620     |  +2.1%  |  −3.9%  |  **−6.0**| 0.2517  | 0.2514  |                    |
| atp250/ATP/grass       | 194→228     |  −4.0%  |  −4.0%  |    ±0    | 0.2484  | 0.2535  |                    |
| atp250/ATP/hard        | 674→728     |  −0.8%  |  −6.4%  |  **−5.6**| 0.2490  | 0.2488  | SHADOW→BLACKLIST   |
| atp500/ATP/clay        | 111→144     |  −0.2%  |  +0.1%  |   +0.3   | 0.2421  | 0.2396  |                    |
| atp500/ATP/grass       |  91→114     | +25.4%  | +12.9%  |  −12.5   | 0.2493  | 0.2538  |                    |
| atp500/ATP/hard        | 331→384     |  −9.1%  |  −9.2%  |   −0.1   | 0.2397  | 0.2415  |                    |
| grand_slam/ATP/grass   | 161→207     |  −3.6%  |  +0.1%  |  **+3.7**| 0.2463  | 0.2435  | SHADOW→SHADOW (↑)  |
| grand_slam/WTA/grass   | 197→239     |  +0.1%  |  −3.8%  |   −3.9   | 0.2427  | 0.2451  |                    |
| m1000/ATP/clay         | 344→419     |  −9.6%  |  −8.1%  |   +1.5   | 0.2434  | 0.2462  |                    |
| m1000/ATP/hard         | 610→655     |  −1.0%  |  −0.2%  |   +0.8   | 0.2430  | 0.2456  |                    |
| tour_final/ATP/hard    |  24→24      | +43.3%  | +42.2%  |   −1.1   | 0.2540  | 0.2560  |                    |
| tour_final/WTA/hard    |  36→34      |  −7.3%  | −11.3%  |   −4.0   | 0.2727  | 0.2719  |                    |
| wta1000/WTA/clay       | 321→394     |  +7.6%  |  +3.0%  |   −4.6   | 0.2427  | 0.2447  | LIVE→SHADOW        |
| wta1000/WTA/hard       | 901→1027    |  −7.1%  |  −6.8%  |   +0.3   | 0.2476  | 0.2451  |                    |
| wta250/WTA/clay        | 344→401     |  −6.2%  |  −6.7%  |   −0.5   | 0.2392  | 0.2388  |                    |
| wta250/WTA/grass       | 215→261     | +17.2%  |  +7.6%  |  **−9.6**| 0.2533  | 0.2519  | LIVE→LIVE          |
| wta250/WTA/hard        | 733→791     |  −6.1%  |  −6.7%  |   −0.6   | 0.2498  | 0.2460  |                    |
| wta500/WTA/clay        | 154→214     |  +4.8%  |  −0.3%  |   −5.1   | 0.2480  | 0.2426  | **LIVE→SHADOW**    |
| wta500/WTA/grass       | 118→144     |  +9.6%  |  +3.0%  |   −6.6   | 0.2471  | 0.2440  | LIVE→SHADOW        |
| wta500/WTA/hard        | 502→552     |  −2.1%  |  −3.7%  |   −1.6   | 0.2435  | 0.2441  |                    |
| **Gewichtet (Σ)**      | **6587→7580** | **−1.62%** | **−3.60%** | **−1.98** | **0.2464** | **0.2461** | 5/9/6 → 2/11/7 |

---

## Interpretation

**Nüchternes Ergebnis:** Reine Historie-Verlängerung (2019→2013) hat den **Elo-Walk-Forward marginal verschlechtert** (−2pp ROI, Brier praktisch gleich −0.0003). Ursachen:

1. **Größeres Test-Fenster** → 993 zusätzliche Value-Bets werden gefunden (7580 vs 6587). Viele davon in schwachen Kategorien (WTA hard, ATP250 clay) → ziehen Gewichtungs-ROI runter.
2. **Elo-Decay** (10% p.a.) macht 2013er-Matches nach 12 Jahren fast bedeutungslos, aber sie belasten dennoch den Rating-Startwert für Comeback-Spieler.
3. **Brier verbessert sich um 0.0003** — Modell wird *leicht kalibrierter*, gewinnt aber gegen bessere Marktquoten weniger (Sharp-Market pricing in).

**Wichtigste Erkenntnis:** Historie allein bringt bei reinem Elo keine Verbesserung. Die eigentlichen Gains von Phase 4 kommen aus Signal-Kombinationen, die dieser Test **nicht abdeckt**:

| Signal                      | Woraus die Verbesserung erwartet wird                          | Wann messbar                       |
|-----------------------------|----------------------------------------------------------------|------------------------------------|
| LGBM +23 Features           | Form-Hot/Stable, Quality, Biometrie, Uncertainty, Altitude     | Nach `tennis_train.py` (Retrain)   |
| Bayesian-Weighted-Elo       | Wenig Historie → geringeres Gewicht statt naiver Elo=1500      | Live-Scanner (feature vorhanden)   |
| Style-Cluster (±3pp)        | Serve-Bot vs Counter-Puncher etc.                              | Post-hoc Scanner-Integration       |
| Line-Movement Confirms      | Filtert Contrarian-Bets die Markt später widerspricht          | Sobald Historic-Odds-Snapshots     |
| In-Play Momentum            | Set 2+ dynamische Adjustment (max ±5pp)                        | Live-Scanner (In-Play-Feed)        |

---

## Empfehlung

Erwartete ROI-Improvements aus Phase 4 lassen sich **nicht** aus reinem Elo-Backtest ablesen. Nächste Schritte:

1. **LGBM-Retrain ausführen** (`python3 scripts/tennis_train.py`) — misst direkten Effekt der 23 neuen Features gegen Elo-Baseline via internem Brier-Gate ≥0.003.
2. **Historic Line-Movement-Sammlung starten** — Cron der jeden pre-match snapshot (T-24h, T-1h, T-0) speichert; nach 4 Wochen ist erste CLV-Statistik machbar.
3. **Style-Cluster als Post-Hoc-Bias in `ensemble.py`** einbauen (max ±3pp Verschiebung nach LGBM-Prediction) — direkt live testbar.

**Was wir heute wissen:** Reine Datenerweiterung hilft nicht. Die Genauigkeit steigt nur wenn die neuen Features aktiv im Modell/Ensemble genutzt werden — genau dort setzt Phase 4 an.
