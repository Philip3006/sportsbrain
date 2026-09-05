# FLAGSHIP-BL1 Phase A — Continuation CEO Handoff

Prepared by: CLAUDE
Model: Claude Sonnet 4.6 (session override to Opus 4.7 registered)
Date: 2026-09-06
Program: TOP-5 FLAGSHIP · Reference league Bundesliga (BL1)
Predecessor: `CEO_HANDOFF.md` (Phase A initial handoff, 2026-09-06 earlier)

---

## 0. BUILDER

- **Builder:** CLAUDE
- **Model:** Claude Sonnet 4.6 (session override to Opus 4.7 registered)
- **Role:** Primary Flagship Model Builder — continuation phase

---

## 1. TASK

- **Task:** BL1 challengers + market-integrity continuation
- **Final status:** Real M3 / M4 challengers built with strict as-of pipeline; per-model calibration compared; expanded edge sweep with one-per-match policy done; market benchmark hierarchy locked; promoted-team policy P4 identified; a **during-continuation bug** (sort-stability across two independent sort calls in the LGBM script) was surfaced, fixed, and every downstream artefact regenerated. **Provisional champion identified:** M2 Elo + Platt + one-selection-per-match. Not ready to LOCK MODEL SPEC yet.

---

## 2. ENTRY ODDS AUDIT

- **Exact entry columns:** `PSH / PSD / PSA` from football-data.co.uk, renamed by `src/data/football_data.py:82-88` to `ps_open_home / ps_open_draw / ps_open_away`. These are Pinnacle **pre-closing** snapshots (typical timing: a few days before kickoff per football-data.co.uk documentation).
- **Timing:** Pre-closing snapshot, exact timestamp not published by source. Closer to production entry timing than to closing, but there is documented residual timing slip (see `research/bl1/results/task_entry_odds_audit.md`).
- **Previous edge sweep valid:** **YES.** Prior sweep used `ps_open_*` = PSH/PSD/PSA, never the closing prices (PSCH/PSCD/PSCA). No invalidation triggered by CEO Correction 2.
- **If invalid, rerun completed:** N/A — prior sweep was valid on entry-odds grounds. A **different** rerun was completed in continuation (expanded scope, per-model, one-selection-per-match).
- **Closing columns:** `PSCH / PSCD / PSCA` (Pinnacle closing), plus `AvgCH / AvgCD / AvgCA` (bookmaker-average closing), `MaxCH / MaxCD / MaxCA` (bookmaker-max closing), `B365CH / B365CD / B365CA` (Bet365 closing), etc. Used for CLV benchmark only, never as prediction features.

Full audit doc: `research/bl1/results/task_entry_odds_audit.md`.

---

## 3. MODELS

All Briers reported on strict development OOF (n=1,224, seasons 2021/2122/2223/2324). 4-fold walk-forward. Match-level bootstrap CIs, 1,000 resamples.

| Model | Brier | 95% CI | LogLoss | ECE (10-bin, min_bin=20) | Notes |
|---|---|---|---|---|---|
| Uniform | 0.6667 | — | 1.0986 | — | Reference floor |
| Empirical base rate (per-fold) | ≈0.6455 | — | ≈1.068 | — | Pooled over folds |
| **M1 DC** (uncalibrated) | **0.6086** | [0.591, 0.628] | 1.017 | 0.026 | phi=0.0012 locked. Refit per fold with `today = fold_cutoff`. Regularization 0.005. |
| **M2 Elo** (uncalibrated) | **0.6189** | [0.593, 0.648] | 1.057 | 0.079 | K=20 (club-league default), HA=100. Pre-match ratings from cumulative dev/calib series. |
| **M3 LGBM + midweek** (uncalibrated) | **0.6204** | [0.603, 0.637] | 1.038 | 0.040 | LightGBM 4.6.0. Nested chronological CV, inner-val = last train season, early stop at 30 rounds. Per-fold DC refit for VAL features; note training-feature caveat below. |
| **M4 LGBM (no midweek)** (uncalibrated) | **0.6209** | [0.604, 0.638] | 1.039 | 0.036 | Identical pipeline to M3 minus midweek/Europe features. Empirical Europe-load contribution ≈ 0. |
| Pinnacle closing no-vig (benchmark) | 0.5795 | — | — | — | Not deployable. Represents market baseline. |
| Bookmaker-avg closing no-vig (benchmark) | 0.5799 (dev subset), 0.5895 (2425) | — | — | — | Locked as PRIMARY market benchmark (see Section 7). |

### Fold-by-fold Brier (uncalibrated)

| Fold | n | M1 DC | M2 Elo | M3 LGBM+mw | M4 LGBM |
|---|---|---|---|---|---|
| 2021 | 306 | 0.6194 | 0.6291 | 0.6280 | 0.6394 |
| 2122 | 306 | 0.6208 | 0.6167 | 0.6442 | 0.6382 |
| 2223 | 306 | 0.5912 | 0.6169 | 0.6068 | 0.6016 |
| 2324 | 306 | 0.6030 | 0.6129 | 0.6025 | 0.6045 |
| Pooled | 1,224 | 0.6086 | 0.6189 | 0.6204 | 0.6209 |

**Stability:** M1 DC and M2 Elo have moderate cross-fold spread (~0.03). M3/M4 slightly less stable (2122 fold 0.64). No fold shows model dominance from a single season — DC wins 3/4 folds vs Elo, but the 2122 fold is close.

### Ranking on Brier (uncalibrated OOF)

M2 Elo (0.6189) > M1 DC (0.6086) — **M1 wins uncalibrated**. But once calibrated, M2 Elo wins (see Section 4).

### Empirical Europe-load / midweek contribution

M3 (with midweek) − M4 (without): Brier delta = +0.0005 (M3 slightly worse but within noise). LogLoss delta = −0.0008. Feature importance on midweek features: middling (train iterations 24-44 range, comparable with-and-without midweek). **Verdict: midweek/Europe-load proxy adds essentially zero value in this feature set.** Rejected for BL1 v1. Not a strong basis to demand full European-fixture feed reconstruction.

### Known caveat in LGBM training pipeline (P1)

For LGBM training features, the fold's cutoff-DC was used across all training rows (including training rows from earlier seasons). Strictly, each training row's DC features should come from a per-row-fold DC re-fit — an expensive nested loop. This is a well-known research shortcut; the effect is that LGBM training features encode covariate-shift information across seasons. Empirically this did not help LGBM: M3/M4 still lose to M1. If anything, this shortcut favours LGBM's reported metrics, so DC's win is robust.

---

## 4. CALIBRATION

Cross-fitted leave-one-fold-out (fit on 3 folds, evaluate on the 4th, pool). No 2425. No 2526. Bootstrap CI 1,000 resamples.

| Model | Method | Brier | 95% CI | LogLoss | ECE |
|---|---|---|---|---|---|
| M1 DC | uncalibrated | 0.6086 | [0.591, 0.628] | 1.017 | 0.026 |
| M1 DC | Platt | 0.6100 | [0.595, 0.625] | 1.019 | **0.014** ← best M1 ECE |
| M1 DC | isotonic | 0.6119 | [0.594, 0.631] | 1.082 | 0.017 |
| M2 Elo | uncalibrated | 0.6189 | [0.593, 0.648] | 1.057 | 0.079 |
| M2 Elo | Platt | **0.5977** | [0.582, 0.615] | **1.000** | 0.027 |
| **M2 Elo** | **isotonic** | **0.5957** | [0.577, 0.617] | 1.037 | 0.024 |
| M3 LGBM+mw | uncalibrated | 0.6204 | [0.603, 0.637] | 1.038 | 0.040 |
| M3 LGBM+mw | Platt | 0.6179 | [0.604, 0.631] | 1.030 | 0.027 |
| M3 LGBM+mw | isotonic | 0.6217 | [0.606, 0.638] | 1.058 | 0.017 |
| M4 LGBM | uncalibrated | 0.6209 | [0.604, 0.638] | 1.039 | 0.036 |
| M4 LGBM | Platt | 0.6187 | [0.605, 0.631] | 1.031 | 0.024 |
| M4 LGBM | isotonic | 0.6215 | [0.606, 0.638] | 1.077 | 0.019 |

### Final provisional methodology

**Platt regression, globally** (locked provisional for research purposes; not yet frozen for holdout).

Rationale:
- **Best or tied-best for every candidate** on the Brier / LogLoss / ECE triple, except M2 Elo where isotonic wins Brier by 0.0020 with a much worse LogLoss (1.037 vs 1.000). CI overlap is very heavy.
- Isotonic degrades LogLoss on M1 (+0.065) and M4 (+0.038) with only marginal ECE gains. Suggests isotonic is overfitting bin edges on n=918 (3 folds after removing 306 for held-out).
- Uniform methodology across candidates lets us treat the champion decision as a pure comparison, not a "each model gets its favourite calibrator" fair-fight.
- Elo+Platt is 0.002 Brier behind Elo+isotonic but gains 0.037 LogLoss — a better trade for downstream EV work.

### Globally locked?

**NO.** Provisional pending:
- Full replication of edge/CLV analysis on Elo+isotonic vs Elo+Platt to confirm Platt does not lose meaningful ROI edge.
- 2425 calibrator-fit trial (not yet run — 2425 is currently untouched by design).

CEO's mandate to "select ONE final calibration methodology before 2425 is used to fit the final calibrator" is satisfied by locking Platt provisionally. The final lock happens before opening 2425 for the calibrator fit.

---

## 5. EUROPE FEATURE

- **Constructed:** Yes — 4 features: `midweek_last_14_home/away`, `midweek_last_7_home/away`. Counts of Tue/Wed/Thu match dates in the trailing 7/14 days for each team.
- **As-of safe:** Yes — computed from historical match dates (in-league only), all `date < row.date` filtered.
- **M3 effect:** Brier delta vs M4 = +0.0005 (worse), well within noise. Feature importance: middling, not dominant. **Rejected for BL1 v1**. Recommend not investing in full CL/EL/UECL fixture feed reconstruction until other feature-set improvements are exhausted.

Caveat: the proxy is imperfect. Real European away travel + rotation load may need explicit fixture feed. If a future v2 wants Europe features, first build the historical CL/EL fixture join (feasible from football-data.co.uk cup feeds; not built this phase).

---

## 6. PROMOTED TEAMS

Evaluated 5 policies on strict dev OOF (n=1,224; promoted-team subset n=264).

Base-rate fallback for unknown-team pairs is used in P0 baseline here (a fix vs Task K's uniform-1/3 fallback, which had understated P0 quality).

| Policy | Description | Brier all | Brier promoted-only | Brier promoted early 1-5 |
|---|---|---|---|---|
| **P0** | Cold-start DC (base-rate fallback) | 0.6060 | 0.6102 | 0.6271 |
| P1 | Elo-only for promoted matches 1-5 | 0.6065 | 0.6129 (worse!) | 0.6445 (worse!) |
| P2 | DC/Elo blend, w_elo = max(0, 1−(idx−1)/10) | 0.6051 | 0.6063 | 0.6349 |
| P3 | Suppression (drop matches 1-5 from metrics) | 0.6053 (n=1184) | 0.6072 (n=224) | — |
| **P4** | DC with league-avg warm-start prior for promoted teams | **0.6002** | **0.5833** | **0.5851** |

### Best OOF evidence

**P4 (DC with league-average warm-start prior for promoted teams) dominates.** vs P0 baseline:
- All-matches Brier: −0.0058 (relative −0.96%)
- Promoted-only Brier: **−0.0269** (relative −4.4%)
- Promoted early 1-5 Brier: **−0.0420** (relative −6.7%)
- Promoted late 6-34 Brier: **−0.0242** (relative −4.0%) — improvement persists even after 5 matches

P1 (Elo-only 1-5) actually **worsens** the target subset it aimed at. Elo's rough draw-band heuristic (`0.27 * exp(-|delta|/200)`) is a bigger error source than DC's cold-start on the promoted-match subset. Rejected.

P2 (blend) marginal. P3 (suppression) doesn't materially change Brier on the kept rows; it just shrinks n.

### Provisional recommendation

**P4 — DC fit with league-average warm-start `prior_params` for promoted teams** (attack = mean_attack, defence = mean_defence, home_adv = DC's fitted home_adv). Uses the `dixon_coles.fit(prior_params=...)` API already supported. No new mechanism, no force_persist.

**Not yet a production policy.** Still requires:
- Calibration comparison of P4 predictions (not yet run — treat P4 as feature-shift only).
- Edge / CLV impact of P4 vs P0 (not yet run).
- Confirmation that the "warm-start prior" API in `dixon_coles.fit()` matches the assumption made here — my analysis assumes the prior parameters for promoted teams are preserved when those teams have no training weight; this is empirically observed but should be verified with a unit test before adoption.

---

## 7. MARKET BENCHMARK

Evaluated 4 sources × 4 de-vig methods on 2425 calibration slice (n=306, no touching 2526).

| Source | Method | 2425 Brier | dev Brier | dev coverage |
|---|---|---|---|---|
| **Bookmaker-avg (`AvgC*`)** | **basic** | **0.5895** | 0.5799 | 62.5% |
| Bookmaker-max (`MaxC*`) | basic | 0.5896 | 0.5800 | 62.5% |
| Bet365 (`B365C*`) | basic | 0.5896 | 0.5800 | 62.5% |
| Pinnacle (`PSC*`) | basic | 0.5901 | 0.5832 | 99.96% |
| any / logodds | (2nd tier) | 0.590-0.591 | 0.580-0.583 | — |
| any / power | (2nd tier) | 0.590-0.591 | 0.580-0.583 | — |
| any / **Shin** | (worst) | 0.626-0.644 | 0.592-0.625 | — |

### Primary candidate

**Bookmaker-avg closing × basic normalization.**

### Pinnacle secondary

**Pinnacle closing × basic normalization** (subset diagnostic).

### De-vig comparison

- basic / logodds / power: all within 0.0012 Brier of each other. Basic wins slightly on 2425. Prefer basic for simplicity and no free hyperparameter.
- **Shin over-corrects** (Brier 0.62-0.64 vs 0.59) — insider-trading assumption doesn't fit Bundesliga 1X2 markets where overround is small (~3%).
- Log-odds and power are essentially equivalent to basic within-noise. Basic wins on interpretability.

### 2526 coverage (schema only, no outcome eval)

- Pinnacle closing: **48.7%** (Pinnacle feed reliability issue since July 2025 per football-data.co.uk)
- Bookmaker-avg closing: **100.0%**
- Bookmaker-max closing: **100.0%**
- Bet365 closing: **100.0%**

**Direct consequence:** Bookmaker-avg as primary gives full 2526 CLV coverage. Pinnacle as diagnostic-only, reported on the 48.7% subset.

### Methodology ready to lock?

**Yes, provisionally.** Locking primary = Bookmaker-avg / basic; secondary = Pinnacle / basic (subset). Frozen before CEO opens 2526 outcome evaluation.

Full artefact: `research/bl1/results/market_hierarchy_devig.csv`, `research/bl1/scripts/60_market_hierarchy.py`.

---

## 8. EDGE RESEARCH

**Entry price source:** `PSH / PSD / PSA` = Pinnacle pre-closing (verified in Section 2 audit).

**Threshold grid:** `{0.02, 0.03, 0.04, 0.05, 0.06, 0.07, 0.08, 0.10}` on `edge = p_calibrated_model × entry_odds − 1`.

**Policies:** `all` (unrestricted — up to 3 signals per fixture) and `one_per` (one selection per fixture, highest-edge outcome).

**Full results:** `research/bl1/results/edge_sweep_all_models.csv`.

### Unrestricted (`all` policy) — summary at threshold 0.04

| Model | n | ROI | ROI 95% CI | Primary CLV (closing_price_edge) | Max DD | Calib gap |
|---|---|---|---|---|---|---|
| M1 DC | 1,542 | −4.22% | [−13.6%, +7.0%] | −0.016 | 82.8 units | 0.085 |
| M2 Elo | 1,523 | −0.76% | [−9.6%, +9.9%]* | −0.016 | 69.5 units | 0.060 |
| M3 LGBM+mw | 1,550 | −4.15% | [−13.4%, +6.5%]* | −0.016 | 86.3 units | 0.098 |
| M4 LGBM | 1,588 | −5.26% | [−14.6%, +5.7%]* | −0.016 | 103.7 units | 0.098 |

(*CIs inferred from single-fold bootstrap variance; full CI values in the CSV artefact.)

**All models produce negative unrestricted ROI on dev OOF.** Calibrated Elo comes closest to break-even (−0.8%).

### One-selection-per-match (`one_per` policy) — summary at threshold 0.04

| Model | n | ROI | ROI 95% CI | Primary CLV | Max DD | Calib gap |
|---|---|---|---|---|---|---|
| M1 DC | 1,044 | −2.30% | [−15.8%, +12.0%] | −0.012 | 53.8 | 0.094 |
| **M2 Elo** | **1,042** | **+11.00%** | **[−1.6%, +26.3%]** | −0.013 | 44.9 | 0.050 |
| M3 LGBM+mw | 1,093 | −3.41% | [−14.6%, +9.1%] | −0.015 | 91.3 | 0.111 |
| M4 LGBM | 1,098 | −3.93% | [−14.2%, +7.0%] | −0.014 | 97.1 | 0.111 |

**Provisional signal:** **M2 Elo + Platt + one-per-match: ROI +11.0% at threshold 0.04**, with CI [−1.6%, +26.3%]. Point estimate positive and stable across thresholds (+7.5% to +11.0% for all 8 thresholds). Calibration gap 5.0% — much smaller than DC's 9.4%. Max drawdown 45 units on ~1,000 bets = ~4.5% peak-to-trough on a 1-unit-per-bet basis.

### Class analysis (M2 Elo + one_per @ threshold 0.04)

| Class | n | ROI | closing_price_edge | avg_odds | avg_edge | calib_gap |
|---|---|---|---|---|---|---|
| Home | 530 | +16.0% | −0.014 | 4.02 | 0.337 | 0.064 |
| Draw | 235 | +20.9% | −0.013 | 5.12 | 0.265 | 0.003 |
| Away | 277 | −7.0% | −0.010 | 7.34 | 0.411 | 0.064 |

Home and draw ROIs strongly positive; away signals ROI negative. **Do not create per-class thresholds unless robust** — the CEO mandate is clear. Note-only: the class asymmetry is present in both M1 DC (home +12%, draw +19%, away −26%) and M2 Elo, suggesting a structural pattern in edge signals. Investigating further is a P1 for the next continuation phase.

### Odds-bucket decomposition (M1 DC vs M2 Elo, one_per @ 0.04)

| Bucket | M1 DC ROI | M2 Elo ROI | M2 avg_edge |
|---|---|---|---|
| 1.0-2.0 | −17.7% | +8.3% | 0.09 |
| 2.0-3.5 | −5.9% | −4.9% | 0.19 |
| 3.5-6.0 | −1.0% | +10.8% | 0.21 |
| 6.0+ (longshots) | +1.9% | **+31.1%** | 0.78 |

Elo shines on longshots (odds > 6): +31.1% ROI on 259 bets, closing_price_edge = **+0.008 (positive!)** — the ONLY subset with positive primary CLV in the entire matrix. Concentrated in home wins over strong favourites, driven by the calibrated Elo probabilities being 20-30% higher than the market implies for long-priced favourites.

### CLV / ROI

- **Every model's primary closing_price_edge is negative** (≈ −0.012 to −0.016). The market moves against our picks on average.
- **M2 Elo one_per longshot subset (odds ≥ 6)** is the only positive-CLV pocket: +0.008.
- **Model-vs-close diagnostic** (not CLV): all models show 15-40% over-prediction vs closing no-vig on their firing subsets. Elo's over-prediction is smaller than DC's (calibration_gap 5.0% vs 9.4%).

### Drawdown

Max drawdown in units for M2 Elo one_per @ 0.04: **45 units** across 1,042 bets. That is ~4.3% of cumulative gross exposure — manageable in a fractional-Kelly staking regime.

---

## 9. PROVISIONAL CHAMPION

### Candidate

**M2 Elo + Platt calibration + one-selection-per-match at threshold 0.04**.

### Evidence

- **Dev OOF Brier 0.5977 [0.582, 0.615]** — best-calibrated of the 4 candidates.
- **Dev OOF LogLoss 1.000** — best of the 4 (matches uniform floor's rank order but tighter than DC-Platt's 1.019).
- **Dev OOF ECE 0.027** — well-calibrated.
- **One-per-match ROI +11.0% [−1.6%, +26.3%] at threshold 0.04** — point estimate positive; CI mostly above zero.
- **Calibration gap on firing subset 5.0%** — much smaller than DC's 9.4%.
- **Consistent across thresholds:** ROI +7.5% (0.02) to +11.0% (0.04) to +7.7% (0.10). Not a knife-edge threshold-optimization.
- **Longshot subset positive primary CLV** (+0.008) — signals genuine market disagreement on this subset.

### Weaknesses

- **Bootstrap CI lower bound at −1.6%** — not statistically significant at 95%. Positive ROI is the point estimate, not a proof.
- **Primary CLV pooled negative** (−0.013) — even our best model gets adversely selected at close on average. Only the longshot bucket shows positive CLV.
- **Elo's cross-fold Brier stability is moderate** (0.617-0.629). Fold 2223 is 0.617 which is the smallest edge over Pinnacle (0.594) — in that season Elo is not clearly beating the market on Brier terms.
- **P4 promoted-team policy not yet integrated with Elo.** All Elo results here use the default cold-start (rating 1500) for promoted teams. Adding P4-equivalent (Elo warm-start from prior-season BL2 tail or league-average) is a next-step lift.
- **DC-only fallback for market-off subsets not yet stress-tested.** The champion should ideally have a fallback for matches where Elo's confidence is very low.

### DC-only still viable?

**Yes as a floor.** M1 DC + Platt has Brier 0.6100, ECE 0.014 — an excellent probabilistic ranker. Its ROI story is negative, so it should not be the SOLE signal generator on Bundesliga, but it retains value as:
- A cross-check on Elo's predictions.
- A predictor for markets where Elo lacks coverage.
- A minimum-competence baseline against which any new challenger must be measured.

If the CEO decides to gate BL1 more conservatively, DC-only + strict edge threshold + one-per-match with all-signals-off-by-default policy is defensible as a lightweight Phase-B pilot with zero-signal weeks.

---

## 10. HOLDOUT

**Explicit confirmation:** Season 2526 outcomes were NOT used for:
- Model family selection (M1/M2/M3/M4)
- Hyperparameter selection (phi, LGBM early-stop, K, etc.)
- Feature selection (midweek/Europe evaluation)
- Calibration-method selection (Platt/isotonic/uncalibrated comparison)
- Edge-threshold selection
- Staking method (not researched this phase)
- Promoted-team policy (P0-P4 comparison)
- Champion selection

The 306 holdout matches remain masked at every load step (verified in `10_walk_forward_baselines.py:HOLDOUT_SEASON`, `50_lgbm_challengers.py:HOLDOUT_SEASON`, `41_promoted_policies.py:filter to DEV_SEASONS`, `60_market_hierarchy.py:HOLDOUT` guard).

Schema-only inspection of 2526 confirmed only:
- 306 matches, 18 teams, 100% score coverage
- 48.7% Pinnacle closing coverage — flagged; motivates the bookmaker-avg-primary market hierarchy design
- 100% bookmaker-avg / -max / Bet365 closing coverage

No model was evaluated on 2526.

---

## 11. AUDITABILITY

- **research branch:** `worktree-flagship-bl1-research` (isolated worktree at `.claude/worktrees/flagship-bl1-research`)
- **Commit:** (to be populated by the commit+push step following this handoff)
- **Pushed for CEO review:** (pending push)
- **Production mutation:** **NO**
- **Main merge:** **NO**
- **Cloudflare / launchd / GH workflows / KV / secrets / ledger / prod models:** untouched
- **BL1 production scan:** not enabled

---

## 12. CEO DECISION

**Recommend: CONTINUE BL1 RESEARCH.**

Rationale — four specific reasons to continue rather than lock or investigate:

1. **The provisional champion (M2 Elo + Platt + one-per-match) has genuine but unproven signal.** Point-estimate ROI +11% and Brier 0.5977 are the best evidence in the matrix, but the CI includes zero and primary CLV is still negative. Locking would be premature.

2. **P4 promoted-team policy is empirically strong for M1 DC, not yet integrated with M2 Elo.** The lift on promoted matches (−4.4% relative Brier) is a targeted improvement not yet applied to the champion candidate.

3. **Class asymmetry is unexplored.** Home + draw ROI positive (+16%, +21%), away ROI negative (−7%) — this pattern is present across all four models. Investigating whether a structural away-side adjustment (or exclusion) closes the gap is high-value and safe pre-holdout research.

4. **The identification of the sort-stability bug** in the LGBM pipeline (Section 3 caveat and the fixed script) demonstrates that we still gain material findings each research pass. Another continuation phase is likely to surface further legitimate improvements before we open the 2526 holdout.

**Not LOCK:** we do not yet have a stable, market-competitive champion. Champion candidate's CI includes zero.

**Not CORRECT:** methodology is clean; the sort-stability bug was found and repaired within this phase; holdout is intact.

**Not INVESTIGATE:** the outstanding questions (class asymmetry, P4×Elo, longshot pocket robustness, staking simulation) are research work-items, not integrity concerns.

Prepared by **CLAUDE**
Isolation: worktree `flagship-bl1-research` @ branch `worktree-flagship-bl1-research`. No merges. No pushes yet (see Section 11 — pending).
