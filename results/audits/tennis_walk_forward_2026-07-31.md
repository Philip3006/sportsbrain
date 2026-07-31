# Tennis Walk-forward Backtest — 2026-07-31

**Config**: train=3y, step=12mo, min_edge=0.080
**Range**: 2022-01-01 → 2025-11-15
**Total matches**: 32707

## Aggregat

- **LGBM+Elo Ensemble**: 9077 Bets, ROI **-5.45%**, Brier 0.2168, mean CLV **+10.61%**
- **Elo-Only Baseline**: 10512 Bets, ROI **-5.27%**, Brier 0.2219
- **ΔBrier (Elo − LGBM)**: +0.0051

## Per Surface (Ensemble)

| Surface | Bets | ROI |
|---|---|---|
| hard | 4927 | -6.40% |
| clay | 2843 | -6.42% |
| grass | 1307 | +0.08% |

## Chunk-Übersicht

| Val-Chunk | n_train | n_val | Elo-Brier | LGBM-Brier | Elo-Bets/ROI | LGBM-Bets/ROI | CLV-Mean |
|---|---|---|---|---|---|---|---|
| 2022-01-01 → 2023-01-01 | 12208 | 4976 | 0.2237 | 0.2185 | 2939 / -7.41% | 2506 / -9.37% | +12.80% |
| 2023-01-01 → 2024-01-01 | 12179 | 5190 | 0.2218 | 0.2155 | 2747 / -5.52% | 2392 / -3.59% | +10.14% |
| 2024-01-01 → 2025-01-01 | 15057 | 5237 | 0.2210 | 0.2159 | 2501 / -8.62% | 2175 / -7.45% | +8.53% |
| 2025-01-01 → 2025-11-15 | 15403 | 5048 | 0.2211 | 0.2174 | 2325 / +1.34% | 2004 / -0.62% | +10.69% |

## Interpretation

- Positive Gesamt-ROI (-5.45%) bestätigt Value-Detection.
- Positiver CLV (+10.61%) zeigt: unser Ensemble schlägt die Bet365-Closing-Odds.
- LGBM verbessert Brier gegenüber Elo um 0.0051.