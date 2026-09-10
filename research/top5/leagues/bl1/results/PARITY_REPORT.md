# BL1 Parity Verification — Top-5 framework vs BL1 v7 reference

**Frozen BL1 v7 reference SHA:** `569741b4ad571a38492e4d7cfd014cf82daec396`
**Top-5 framework branch:** `feat/top5-framework`

## Result — PARITY PROVEN on M5 / M6 / matched-preclose-vs-close

| Metric | BL1 v7 reference | Top-5 framework (BL1) | Delta |
|---|---|---|---|
| M5 pooled Brier | 0.58222548547 | 0.5822254854702447 | 0 (bit-identical) |
| M5 n | 1224 | 1224 | 0 |
| M6 pooled Brier | 0.58222548547 | 0.5822254854702447 | 0 (bit-identical) |
| Matched delta Brier | 0.002463916151019152 | 0.002463916151019152 | 0 |
| Matched CI lo | 9.2265e-05 | 9.2265e-05 | 0 |
| Matched CI hi | 4.8175e-03 | 4.8175e-03 | 0 |
| Matched n | 1224 | 1224 | 0 |

## Framework parity gate — PASSED

The generic framework reproduces BL1 v7 M5 / M6 / matched-preclose-vs-close
to full float64 precision. Zero methodology change. The refactor itself does
not alter the frozen BL1 result.

## Scope of this parity gate

Covered: M5, M6 at alpha=1.0 (which is what BL1 v7 selects chronologically),
matched preclose vs close. These depend only on raw dataset + canonical market
policy + optional in-line Elo. They are the critical path for market baseline
research.

Not covered in this pass: M1 (Dixon-Coles), M2 (Elo standalone), M3/M4 (LGBM
challengers), M7 (LGBM residual). Those use BL1-specific precomputed artefacts
(`elo_series_dev.pkl`, `dc_snapshots/`, calibrated model pickles). Porting
them into the generic framework is a Phase-2 extension.
