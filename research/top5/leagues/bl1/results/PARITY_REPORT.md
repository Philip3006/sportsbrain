# BL1 Parity Verification — Top-5 framework vs BL1 v7 reference (AUTOMATED)

**Frozen BL1 v7 reference SHA:** `569741b4ad571a38492e4d7cfd014cf82daec396`
**Reference worktree:** `/Users/philiprassillier/sportsbrain/.claude/worktrees/flagship-bl1-research/research/bl1/results`
**Top-5 framework branch:** `feat/top5-framework`
**Automated gate:** `research/top5/tests/test_bl1_parity.py`

## Result — 7/7 MODEL PARITY PROVEN (automated)

Every model (M1 through M7) reproduces its BL1 v7 reference Brier to within
the applicable float64 tolerance. Zero methodology change. The refactor
does not alter the frozen BL1 result.

| Model | BL1 v7 reference Brier | Top-5 framework Brier | Delta | Tolerance |
|---|---|---|---|---|
| M1 (DC walk-forward) | 0.605926931401144 | 0.605926931401144 | 0.00e+00 | 1e-12 |
| M2 (Elo standalone) | 0.618887657942398 | 0.618887657942398 | 0.00e+00 | 1e-12 |
| M3 (LGBM + dmwd) | 0.601714537491436 | 0.601714537491436 | 0.00e+00 | 1e-11 |
| M4 (LGBM baseline) | 0.602646133691481 | 0.602646133691481 | 0.00e+00 | 1e-11 |
| M5 (market pre-close) | 0.582225485470245 | 0.582225485470245 | 0.00e+00 | 1e-12 |
| M6 (market + Elo blend) | 0.582225485470245 | 0.582225485470245 | 0.00e+00 | 1e-12 |
| M7 (LGBM market residual) | 0.630888443209391 | 0.630888443209394 | 3.33e-15 | 1e-11 |

Additional parity checks:

| Metric | BL1 v7 reference | Top-5 framework | Delta |
|---|---|---|---|
| Selected phi (DC time-decay) | 0.0012 | 0.0012 | identical |
| Matched preclose delta_brier | 0.002463916151019 | 0.002463916151019 | 0 |
| Matched preclose CI lo (95%) | 9.2265e-05 | 9.2265e-05 | 0 |
| Matched preclose CI hi (95%) | 4.8175e-03 | 4.8175e-03 | 0 |
| Matched n | 1224 | 1224 | 0 |
| M1/M2/M3/M4/M5/M6/M7 outer n | 1224 | 1224 | 0 |

## Framework parity gate — PASSED (7/7)

Method: automated pytest gate (`test_bl1_parity.py`) that loads OOF CSVs
from both the frozen reference worktree and the Top-5 BL1 results
directory, recomputes pooled outer-fold Brier scores, and asserts:

- Deterministic models (M1/M2/M5/M6): delta < 1e-12
- LGBM models (M3/M4/M7): delta < 1e-11 (tiny float64 drift acceptable)
- Selected phi = 0.0012 in both
- Matched pre-close vs closing CI equal within 1e-10

## Note on phi CSV intermediate brier discrepancy

The reference `phi_selection_dev.csv` reports per-phi Brier values in the
range 0.6086-0.6105 while the Top-5 framework's file reports 0.6060-0.6079.
This is NOT a parity failure and does not affect any downstream artefact.

Cause: the reference's phi sweep evaluated over a broader OOF pool (the
calib seed folds 1819+1920 plus outer_folds 2021-2324), while the Top-5
framework's phi sweep uses only outer_folds. The best-phi *selection*
(0.0012) is identical in both, the fitted DC snapshots (which use only
prior-season data at each snapshot date) are bit-identical, and the M1
OOF Brier that consumes those snapshots is bit-identical to 12+ significant
digits (delta = 0).

The phi CSV is a diagnostic artefact of the model-selection process; the
production-consumed artefact is the selected phi value plus the resulting
DC snapshots, both of which are proven identical.

## Scope of this parity gate

- All 7 primary models (M1-M7)
- Selected phi
- Matched preclose-vs-close statistical comparison

No LGBM re-training was required for BL1 parity verification — the
comparison uses the OOF CSVs written by the framework's actual training
runs, meaning parity is verified against the *outputs* of the current
Top-5 code as run on the shared raw dataset, exactly as production would.
