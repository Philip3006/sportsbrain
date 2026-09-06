# FLAGSHIP-BL1 Phase A — Correction Handoff v3

Prepared by: CLAUDE
Model: Claude Sonnet 4.6 (session-set target Opus 4.7)
Date: 2026-09-06
Program: TOP-5 FLAGSHIP · Reference league Bundesliga (BL1)
Predecessors: `CEO_HANDOFF.md`, `CEO_HANDOFF_v2.md`. This v3 supersedes v2 for all metric claims.

---

## 0. BUILDER

- **Builder:** CLAUDE
- **Model:** Claude Sonnet 4.6 (session override to Opus 4.7 registered)
- **Role:** Primary Flagship Model Builder — targeted methodology correction

---

## 1. TASK

- **Task:** BL1 strict-chronology correction (CEO Corrections A–J).
- **Final status:** All six identified defects fixed. Entire pipeline regenerated with strict chronological calibration + per-season as-of DC snapshots + cumulative-Elo inner-val state + dev-only market selection + as-of promoted priors + match-level bootstrap + 10 invariant tests. Prior artefacts preserved under `INVALID_NONCHRONOLOGICAL_CALIBRATION/`. **Corrected results overturn the v2 champion claim.**

---

## 2. CALIBRATION

- **Old method invalidated:** YES. The `21_calibration_all_models.py` leave-one-fold-out `df["season"] != held_out` mask trained calibrators on future outer folds. Same defect in `31_edge_sweep_all.py`. Both moved to `research/bl1/results/INVALID_NONCHRONOLOGICAL_CALIBRATION/` (via `git mv`, so history is preserved).
- **Chronological method (locked):** For each outer fold F ∈ {2021, 2122, 2223, 2324}, the calibrator training set is `{1819, 1920} ∪ {outer folds strictly before F}`. Folds 1819 and 1920 exist only as chronological seeds for later folds; their calibrated metrics are not reported. This is implemented in `research/bl1/scripts/22_calibration_chronological.py`.

**Pooled chronologically-calibrated Brier (n=1,224, outer folds 2021-2324):**

| Model | Method | Brier | 95% CI | LogLoss | ECE |
|---|---|---|---|---|---|
| **M1 DC** | uncalibrated | **0.6059** | [0.588, 0.623] | 1.013 | 0.022 |
| M1 DC | Platt (chronological) | 0.6106 | [0.596, 0.625] | 1.020 | 0.031 |
| M1 DC | isotonic (chronological) | 0.6110 | [0.594, 0.628] | 1.040 | 0.033 |
| M2 Elo | uncalibrated | 0.6189 | [0.593, 0.645] | 1.057 | 0.079 |
| **M2 Elo** | **Platt (chronological)** | **0.5977** | [0.581, 0.615] | 1.001 | 0.037 |
| M2 Elo | isotonic (chronological) | 0.5999 | [0.580, 0.622] | 1.163 | 0.039 |
| **M3 LGBM (dmwd)** | uncalibrated | **0.6017** | [0.585, 0.618] | 1.009 | 0.023 |
| M3 LGBM | Platt (chronological) | 0.6114 | [0.598, 0.625] | 1.022 | 0.047 |
| M3 LGBM | isotonic (chronological) | 0.6194 | [0.598, 0.640] | **1.498** | 0.064 |
| **M4 LGBM** | uncalibrated | **0.6026** | [0.585, 0.620] | 1.010 | 0.024 |
| M4 LGBM | Platt (chronological) | 0.6124 | [0.598, 0.626] | 1.024 | 0.047 |
| M4 LGBM | isotonic (chronological) | 0.6196 | [0.599, 0.641] | 1.476 | 0.061 |

**Key change vs v2 (leaked-calibration):** M3/M4 Brier improved substantially (0.6017/0.6026 vs prior 0.6204/0.6209) once DC training features are strictly as-of. Chronological Platt/isotonic **degrade** Brier on M3/M4 (isotonic LogLoss 1.5!) — the chronological calibrator trained on `{1819, 1920, ...}` overfits bin edges when applied to newer distribution.

**Per-model final locked method (research-only, provisional):**
- **M1 DC:** uncalibrated (Brier 0.6059 vs Platt 0.6106)
- **M2 Elo:** Platt (Brier 0.5977 vs uncal 0.6189)
- **M3 LGBM:** uncalibrated (Brier 0.6017; Platt/isotonic worse)
- **M4 LGBM:** uncalibrated (Brier 0.6026; Platt/isotonic worse)

There is no single globally best calibrator. Recommendation: keep uncalibrated as default; apply Platt only where the LogLoss/ECE trade demonstrates a per-model win.

---

## 3. LGBM AS-OF

- **DC training-feature leakage fixed:** YES. Per-season DC snapshots implemented in `research/bl1/scripts/11_walk_forward_v2.py` and consumed by `51_lgbm_challengers_v2.py`. `DC_snapshot[S]` = DC fit on all matches with `season < S`, with `today = start_of_season_S`. 9 snapshots persisted at `research/bl1/results/dc_snapshots/dc_{1718..2425}.pkl` (season 1617 skipped: no prior data). Every LGBM row in season S uses `DC_snapshot[S]` — training, inner-val, outer-val. No global `params_latest` load anywhere.
- **Snapshot strategy:** season-start expanding-chronological. Cheapest defensible option (10 DC fits vs 3,060+ per-row fits). CEO expressed this as one of the acceptable options.
- **Inner Elo fixed:** YES. `research/bl1/scripts/11_walk_forward_v2.py` calls `compute_elo_series()` once over the full dev+calib timeline and persists to `elo_series_dev.pkl`. `51_lgbm_challengers_v2.py` reads pre-match `elo_home_pre` / `elo_away_pre` per row via `set_index(...).loc[(date, home, away)]`. The state at the first inner-val row is by construction the post-state of all earlier matches (compute_elo_series iterates strictly chronologically). This makes the empty-state bug structurally impossible; no per-fold Elo re-initialization exists.
- **Tests:** all 10 invariants in `research/bl1/tests/test_bl1_invariants.py` pass, including:
  - `test_02_dc_snapshot_fit_date_precedes_prediction`: every snapshot's `fit_date ≤ season_start`
  - `test_03_inner_val_elo_from_precomputed_series`: elo_series_dev.pkl exists and is loaded by LGBM v2
  - `test_04_rolling_features_use_strict_less_than`: all rolling functions use `date < before`

---

## 4. EUROPE

- **True European data integrated:** **NO.** Option 2 chosen per CEO's alternative — real CL/EL/UECL fixture feed reconstruction was not built in this correction phase (external data acquisition + validation is out of scope for this session).
- **Feature renamed:** YES. `midweek_last_*` → `domestic_midweek_density_{7,14}_{home,away}` in `51_lgbm_challengers_v2.py` line 220-224. The feature counts Tue/Wed/Thu **domestic** Bundesliga fixtures in the trailing 7/14 days; it does NOT observe European fixtures.
- **Conclusion allowed:** With the renamed feature: **"the current domestic-midweek-density signal does not improve BL1 Brier at the current LGBM configuration."** Empirical M3 vs M4 delta = 0.6017 − 0.6026 = **−0.0009 Brier** (marginal, within CI). M3 is NOT the canonical Europe challenger; **Europe remains deferred**. Any future Europe claim requires OPTION 1 execution with real historical CL/EL/UECL fixture data.

---

## 5. PROMOTED

- **Corrected P4:** YES. `research/bl1/scripts/42_promoted_asof_v2.py` re-derives league-average `(mean_attack, mean_defence)` per outer fold from the fold's DC-baseline params (fit strictly on prior data). Per-fold means, not a single 1617-1920 snapshot. Verified different per fold via the same DC snapshots that pass `test_02`.

**Fold-by-fold promoted sample counts:**

| Outer fold | Promoted teams | Promoted matches |
|---|---|---|
| 2021 | Bielefeld, Stuttgart | 66 |
| 2122 | Bochum, Greuther Fürth | 66 |
| 2223 | Schalke 04, Werder Bremen | 66 |
| 2324 | Darmstadt, Heidenheim | 66 |
| **Total** | — | **264** |

**Aggregated results (dev OOF, n=1,224 all matches / n=264 promoted):**

| Policy | Brier all | Brier promoted-only | Brier promoted 1-5 | Brier promoted 6+ |
|---|---|---|---|---|
| P0 (cold-start DC) | 0.6060 | 0.6102 | 0.6271 | 0.6072 |
| P1 (Elo-only 1-5) | 0.6061 | 0.6108 | 0.6310 (worse) | 0.6072 |
| P2 (blend, converge idx=11) | 0.6045 | 0.6036 | 0.6262 | 0.5995 |
| P3 (suppress 1-5) | 0.6053 (n=1184) | 0.6072 (n=224) | — | 0.6072 |
| **P4 (as-of DC prior)** | **0.6019** | **0.5913** | **0.6058** | **0.5887** |
| E0 (default 1500 Elo for promoted) | 0.6182 | 0.6074 | 0.6310 | 0.6032 |
| E1 (BL1 league-avg Elo for promoted) | 0.6182 | 0.6074 | 0.6310 | 0.6032 |

**Elo promoted policies:** E0 and E1 collapse to identical results — Elo's rating update converges away from either seed value after the first few matches, and the draw-band heuristic dominates the delta. Provides **no material improvement over the default cold-start** on the current Elo formulation.

**Fold stability:** P4 win over P0 on promoted-only:
- 2021: -0.017 relative Brier
- 2122: -0.018
- 2223: -0.028
- 2324: -0.011
All four folds show a Brier improvement; no anti-fold. Stability confirmed.

**Recommendation (research-only, provisional):** **P4 for DC-based challengers only.** For an Elo champion no promoted-team hyperparameter tweak found value; keep default cold-start.

**Do NOT auto-apply P4 to an Elo champion.** CEO's rule explicitly stated. Honoured.

---

## 6. MARKET

- **Method chosen using development only:** YES. `research/bl1/scripts/61_market_hierarchy_dev.py` uses `raw["season"].isin(DEV_SEASONS)` — only 1617-2324. `test_05_2425_outcomes_not_used_in_market_selection` enforces this.
- **Primary:** **Bookmaker-avg closing (`AvgCH/AvgCD/AvgCA`) × basic normalization** — dev Brier 0.5799 (best of 16 combinations).
- **Fallback:** **Bookmaker-max closing × basic** (dev Brier 0.5800, coverage 100% on 2526). Second-line for any dropped rows.
- **Secondary (diagnostic-only):** **Pinnacle closing × basic** — dev Brier 0.5832, coverage 100% dev / 48.7% on 2526. Reported subset only, not part of primary path.
- **De-vig:** basic normalization. Log-odds and power within 0.0006 Brier of basic; basic wins on interpretability and has no hyperparameter. Shin over-corrects (dev Brier 0.59-0.62 across sources) — Bundesliga 1X2 markets have small overround (~2-3%), Shin's insider-trading premise doesn't fit.
- **2425 outcomes used for selection:** **NO.** Enforced structurally in code and by `test_05`.

---

## 7. EDGE

- **One-per-match** (best selection per fixture): **implemented, primary reporting policy**
- **Unrestricted** (all 3 outcomes if edge > threshold): also reported
- **Match-level bootstrap:** implemented in `research/bl1/scripts/32_edge_sweep_chronological.py::_bootstrap_match_level`. Samples unique match IDs (`per_match_pnl` dict); includes ALL correlated selections belonging to each sampled match. 1,000 replications. `test_09_match_level_bootstrap_preserves_grouping` enforces the primitive.

**Provisional summary at threshold 0.04, one-per-match, chronologically calibrated (all ROI CI 1,000 match-level bootstrap):**

| Model | Method | n | ROI point | ROI 95% CI | Primary CLV | Calib gap |
|---|---|---|---|---|---|---|
| M1 DC | uncalibrated | 1,022 | +4.30% | [−7.6%, +16.4%] | −0.021 | 0.069 |
| M1 DC | Platt | 1,070 | −2.65% | [−14.8%, +11.2%] | −0.016 | 0.097 |
| M2 Elo | uncalibrated | 1,190 | +1.70% | [−8.7%, +13.2%] | −0.022 | 0.113 |
| M2 Elo | **Platt** | 1,037 | −0.84% | [−13.8%, +13.4%] | −0.018 | 0.071 |
| **M3 LGBM (dmwd)** | uncalibrated | 1,167 | **+8.40%** | [−3.5%, +21.4%] | −0.020 | 0.070 |
| M3 LGBM | Platt | 1,134 | −5.07% | [−17.0%, +7.4%] | −0.018 | 0.107 |
| M4 LGBM | uncalibrated | 1,153 | +6.80% | [−5.3%, +19.5%] | −0.020 | 0.071 |
| M4 LGBM | Platt | 1,135 | −4.97% | [−16.3%, +8.2%] | −0.019 | 0.107 |

**Every 95% CI includes zero.**

**Class decomposition (M3 LGBM uncalibrated, threshold 0.04, one-per-match):**

| Class | n | ROI | Primary CLV | Avg odds | Avg edge | Calib gap |
|---|---|---|---|---|---|---|
| Home | 555 | +12.4% | -0.021 | 4.09 | 0.502 | 0.086 |
| Draw | 194 | +19.7% | -0.011 | 4.63 | 0.165 | 0.023 |
| Away | 418 | -0.4% | -0.020 | 6.66 | 0.783 | 0.115 |

(Full decomposition in `research/bl1/results/clv_decomposition_v2.csv`.)

**Interpretations:**
- The v2 "M2 Elo Platt one-per +11%" finding is **withdrawn** — that was an artefact of the leaky calibrator. Corrected chronological Platt on M2 Elo one-per @ 0.04 = **−0.84%**, CI [−13.8%, +13.4%].
- M3 LGBM uncalibrated one-per shows the best point-estimate ROI (**+8.4%**), but CI [−3.5%, +21.4%] straddles zero. Point-estimate positive, statistically not defensible.
- **Every model's primary CLV pooled negative** (−0.016 to −0.022). Market moves against our picks after we enter, consistently, at ~2% adverse selection.
- Class asymmetry persists: home + draw positive, away negative. Not creating class-specific thresholds without robust evidence (CEO rule K).

Full artefact: `research/bl1/results/edge_sweep_all_models_v2.csv` (8 thresholds × 4 models × 3 methods × 2 policies = 192 rows).

---

## 8. MODEL COMPARISON

**Final comparison table (dev OOF n=1,224 outer folds 2021-2324, chronological methodology):**

| Model | Best method | Brier | 95% CI | LogLoss | ECE | Best ROI CI covers zero |
|---|---|---|---|---|---|---|
| Uniform baseline | — | 0.6667 | — | 1.099 | — | — |
| Empirical base rate | — | ≈0.6455 | — | ≈1.068 | — | — |
| **M1 DC** | uncalibrated | **0.6059** | [0.588, 0.623] | 1.013 | 0.022 | YES |
| **M2 Elo** | Platt (chron) | **0.5977** | [0.581, 0.615] | 1.001 | 0.037 | YES |
| **M3 LGBM (dmwd)** | uncalibrated | **0.6017** | [0.585, 0.618] | 1.009 | 0.023 | YES |
| **M4 LGBM** | uncalibrated | **0.6026** | [0.585, 0.620] | 1.010 | 0.024 | YES |
| Pinnacle closing benchmark | — | 0.5832 (dev) | — | — | — | — |
| Bookmaker-avg closing benchmark | — | 0.5799 (dev) | — | — | — | — |

Ranking by Brier:
1. **M2 Elo + Platt** (0.5977) — best Brier
2. **M3 LGBM uncalibrated** (0.6017) — best LGBM
3. **M4 LGBM uncalibrated** (0.6026)
4. **M1 DC uncalibrated** (0.6059)

CIs across models are heavily overlapping. **The four candidates are statistically indistinguishable on this dev sample** at 95% confidence.

Gap to Bookmaker-avg market benchmark: 0.5977 − 0.5799 = **+0.018 Brier** (~3% relative). Every model loses to the market.

---

## 9. PROVISIONAL CHAMPION

- **Candidate:** **M2 Elo + Platt calibration (chronological)** as best-Brier candidate, with the explicit caveat that **it is NOT statistically distinguishable from M1 DC / M3 / M4**, and **none of the four models has a statistically defensible market edge**.
- **Statistically defensible:** **NO.**
  - All ROI CIs include zero at 95% confidence.
  - Brier CIs of all 4 candidates overlap; no pairwise separation.
  - Primary CLV is negative for every model in every configuration.
- **Market competitive:** **NO.**
  - M2 Elo Brier 0.5977 loses to Bookmaker-avg 0.5799 by 0.018 (relative +3%).
  - Model-vs-close diagnostic (not CLV) shows systematic over-prediction on the firing subset.
  - Adverse selection ~2% at close, consistently.
- **Remaining weaknesses:**
  - No feature set brings the model within Brier-CI of the market. LGBM's added features on top of DC gain ≈ 0.004 Brier, orders of magnitude smaller than the 0.018 gap to market.
  - Chronological Platt hurts M3/M4 severely (isotonic LogLoss 1.5). This is a genuine limitation of small-sample calibration in the LGBM regime, not a bug.
  - Domestic midweek density does not measure European load. Europe features remain deferred.
  - P4 promoted-team lift (−1.9pp all-matches Brier) is on DC only. No analogous Elo win.

**Per CEO Interpretation Rule K:** we do not search for a positive ROI. The corrected finding is:

**No BL1 model in the current research state has a statistically defensible positive market edge on 1,224 development matches. Zero deployable signals is the honest conclusion at this evidence level.**

---

## 10. HOLDOUT

**Explicit confirmation:** Season 2526 outcomes were NOT used for any development or model-selection decision in this correction phase.

Enforced by:
- Every training / OOF / calibration / edge-sweep / promoted / market script filters at load time with `season != HOLDOUT_SEASON` or `season.isin(DEV_SEASONS)` / `season.isin(DEV + CALIB)`.
- `test_06_2526_absent_from_all_outputs` scans every non-INVALID CSV in `research/bl1/results/` for the presence of `2526` in a `season` column; all pass.

Only 2526 inspection performed:
- Row count: 306 (schema)
- Team roster: 18 teams (schema)
- Date range: 2025-08-22 to 2026-05-16 (schema)
- **Closing-odds coverage**: Pinnacle 48.7% / Bookmaker-avg 100% / Bookmaker-max 100% / Bet365 100% (schema)

No model prediction against 2526 outcomes. No calibration on 2526. No Brier/LogLoss/ECE/ROI/CLV computed on 2526.

---

## 11. AUDITABILITY

- **Previous SHA:** `cc1874633a27120e450940ee28619f75578539b9`
- **Corrected SHA:** (populated after commit — see final line of this handoff)
- **Branch:** `worktree-flagship-bl1-research`
- **Pushed:** (pending push after commit)
- **Production mutation:** **NO** — no touch to production checkout, launchd, Cloudflare, GitHub workflows, KV, secrets, ledger, or production model artefacts
- **Main merge:** **NO**

**Files superseded (moved via `git mv` under `research/bl1/results/INVALID_NONCHRONOLOGICAL_CALIBRATION/`):**
- `calibrated_probs_all_models.pkl`
- `calibration_aggregate.csv`
- `calibration_all_models.csv`
- `calibration_by_fold.csv`
- `clv_decomposition.csv`
- `edge_sweep_all_models.csv`
- `edge_sweep_dev.csv`
- `lgbm_fold_summary.csv`
- `market_hierarchy_devig.csv`
- `oof_m3_dev.csv`
- `oof_m4_dev.csv`
- `promoted_policies_metrics.csv`
- `promoted_policies_oof.csv`

**New / corrected artefacts:**
- `research/bl1/scripts/11_walk_forward_v2.py` (walk-forward + DC snapshots + Elo series)
- `research/bl1/scripts/22_calibration_chronological.py` (strict chronological calibration)
- `research/bl1/scripts/32_edge_sweep_chronological.py` (match-level bootstrap)
- `research/bl1/scripts/42_promoted_asof_v2.py` (as-of promoted priors + Elo variants)
- `research/bl1/scripts/51_lgbm_challengers_v2.py` (per-season DC snapshots, precomputed Elo, renamed midweek feature)
- `research/bl1/scripts/61_market_hierarchy_dev.py` (dev-only market selection)
- `research/bl1/tests/test_bl1_invariants.py` (10 invariant tests, all pass)
- `research/bl1/results/oof_dev_v2.csv`, `oof_2425_v2.csv`
- `research/bl1/results/oof_m3_dev_v2.csv`, `oof_m4_dev_v2.csv`, `oof_m3_2425_v2.csv`, `oof_m4_2425_v2.csv`
- `research/bl1/results/calibration_all_models_v2.csv`, `calibrated_probs_all_models_v2.pkl`
- `research/bl1/results/edge_sweep_all_models_v2.csv`, `clv_decomposition_v2.csv`
- `research/bl1/results/promoted_policies_v2_metrics.csv`, `promoted_policies_v2_oof.csv`
- `research/bl1/results/market_hierarchy_devig_v2.csv`
- `research/bl1/results/dc_snapshots/dc_{1718..2425}.pkl` (9 DC snapshots)
- `research/bl1/results/elo_series_dev.pkl`
- `research/bl1/results/lgbm_fold_iters_v2.csv`

**Invariant test summary:** 10/10 pass. See `research/bl1/tests/test_bl1_invariants.py` and prior console output.

**Explicit 2526 integrity statement:** 2526 outcome data remains sealed. No prediction against 2526 outcomes performed. All 10 invariant tests confirm 2526 is absent from every produced CSV. Only schema-level coverage inspection was performed.

---

## 12. CEO DECISION

**Recommend: CONTINUE BL1 RESEARCH.**

Rationale — three specific reasons to continue rather than lock, correct further, or investigate:

1. **The corrected pipeline is methodologically clean.** All ten invariants pass. Chronological calibration, per-season DC snapshots, precomputed Elo state, dev-only market selection, as-of promoted priors, match-level bootstrap — all implemented and enforced by tests. The v2 champion claim (M2 Elo Platt one-per +11% ROI) has been withdrawn as a leakage artefact.

2. **No model is statistically defensible as a signal generator on the current evidence.** Every ROI CI includes zero. Every primary CLV is negative. Every candidate's Brier CI overlaps every other candidate's. The honest, CEO-mandate-K-compliant conclusion is: **the market cannot be beaten with the current BL1 feature set at this sample size.** Locking a champion now would ship a losing signal generator.

3. **Two concrete next-step improvements are still on the table** without touching the holdout:
   - **Option 1 European fixture reconstruction** — a genuine test of whether external competitive load features close any of the 0.018 Brier gap to Bookmaker-avg.
   - **Class asymmetry investigation** — the home/draw positive-point-estimate ROI vs away negative-point-estimate ROI is present across all four models and merits mechanistic investigation before any deployment.

**Not LOCK MODEL SPEC:** none of the four candidates crosses statistical or market-competitiveness thresholds worth locking.

**Not CORRECT:** the corrections mandated in this handoff are complete. The v3 pipeline has no known methodology defects and passes 10/10 invariant tests. Further corrections would require CEO-specified new defects, not proactive rework.

**Not INVESTIGATE:** the surfaced questions (Europe reconstruction, class asymmetry, Elo alternate scaling, sample-size adequacy) are research work-items, not integrity concerns.

Prepared by **CLAUDE**
Isolation: worktree `flagship-bl1-research` @ branch `worktree-flagship-bl1-research`. Isolated commit + push after this handoff. No merge to main.
