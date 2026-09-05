# FLAGSHIP-BL1 Phase A — CEO Handoff

Prepared by: CLAUDE
Model: Claude Sonnet 4.6 (session-set target Opus 4.7)
Date: 2026-09-06
Program: TOP-5 FLAGSHIP · Reference league Bundesliga (BL1)

---

## 0. BUILDER

- **Builder:** CLAUDE
- **Model:** Claude Sonnet 4.6 (session model override to Opus 4.7 registered)
- **Role:** Primary Flagship Model Builder — Phase A research
- **Effort profile:** Isolated research only. No production writes.

---

## 1. TASK

- **Task:** Bundesliga Flagship Phase A model research
- **Scope executed:** Tasks A, B, C, E, F, G, K + provisional recommendation
- **Deferred:** LGBM challenger models M3 / M4 (feature-set frozen, walk-forward runner scaffolded, empirical LGBM training deferred — see Section 10 "weaknesses" and Section 11 "P1")
- **Final status:** Development phase complete on baselines. Provisional champion identified. **Not ready to lock model spec.**

---

## 2. ISOLATION

- **worktree:** `/Users/philiprassillier/sportsbrain/.claude/worktrees/flagship-bl1-research/` branched from origin/main (commit `c46d4cdad`), on branch `worktree-flagship-bl1-research`
- **active production checkout touched:** **NO**
- **production mutation:** **NO**
- **main merged:** **NO**
- **Cloudflare / launchd / GitHub workflows / KV / production ledger / secrets:** untouched
- **Production DC/LGBM artifacts:** untouched
- **BL1 production scans:** not enabled

All research artifacts live under `research/bl1/` inside the isolated worktree. Nothing pushed to origin.

---

## 3. DATASET

### Seasons and coverage

| Season | Matches | Teams | Date range | Pin close | Pin open | Missing scores | Dup fixtures |
|---|---|---|---|---|---|---|---|
| 1617 | 306 | 18 | 2016-08-26 – 2017-05-20 | 100.0% | 100.0% | 0 | 0 |
| 1718 | 306 | 18 | 2017-08-18 – 2018-05-12 | 100.0% | 100.0% | 0 | 0 |
| 1819 | 306 | 18 | 2018-08-24 – 2019-05-18 | 99.7% | 99.7% | 0 | 0 |
| 1920 | 306 | 18 | 2019-08-16 – 2020-06-27 (COVID) | 100.0% | 100.0% | 0 | 0 |
| 2021 | 306 | 18 | 2020-09-18 – 2021-05-22 | 100.0% | 100.0% | 0 | 0 |
| 2122 | 306 | 18 | 2021-08-13 – 2022-05-14 | 100.0% | 100.0% | 0 | 0 |
| 2223 | 306 | 18 | 2022-08-05 – 2023-05-27 | 100.0% | 100.0% | 0 | 0 |
| 2324 | 306 | 18 | 2023-08-18 – 2024-05-18 | 100.0% | 100.0% | 0 | 0 |
| 2425 | 306 | 18 | 2024-08-23 – 2025-05-17 | 100.0% | 100.0% | 0 | 0 |
| **2526** | 306 | 18 | 2025-08-22 – 2026-05-16 | **48.7%** | **49.0%** | **0** | **0** |
| **Total** | **3,060** | **30 unique** | 10 seasons | | | | |

- **Total matches:** 3,060 (all seasons at 306 = 18 teams × 34 matchdays × 2 halves)
- **Missingness:** zero missing final scores across all 10 seasons; zero duplicate fixture keys; 1819 has one missing PSC row (99.7% Pin coverage)
- **Pinnacle coverage:** 100% dev seasons + 2425. **2526 shows only 48.7% closing-odds coverage** in the archive.org capture used — flagged as a data-quality risk for the eventual holdout CLV evaluation
- **Anomalies:** 1920 season extended to 2020-06-27 (COVID lockdown suspension) — mechanically handled correctly by chronological ordering. No renamed clubs detected in the 10-year window that would break `canonical_name()` mapping. All 30 unique team labels resolve.
- **Leakage risks in current pipeline (confirmed by code inspection):**
  - `scripts/train_lgbm_bundesliga2.py:278` loads global `dc_bundesliga2/params_latest.pkl` for feature construction across ALL historical matches — future information from 2026 injected into 2017 features (SEVERE)
  - `src/data/market_values.py` is a June 2026 Transfermarkt snapshot with no BL1 teams; would collapse to `1.0/1.0=1.0` for BL1 and leak the future snapshot if BL1 values were added (SEVERE)
  - `src/data/attendance.py` static 25/26-basis; no BL1 teams present (MODERATE)
  - `scripts/train_lgbm_bundesliga2.py:189` reads Elo from non-existent per-team columns → silent Elo-broken bug in current BL2 LGBM (elo_home/elo_away/elo_diff features are constant 1500)
  - `scripts/bundesliga2_scan.py:62` hardcodes `dc_bundesliga2/params_latest.pkl` — portability blocker for BL1 (not a leakage risk for live signals but a refactor requirement)

### Canonical fixture-key strategy

`(date_utc_date, home_team, away_team)` after `canonical_name()` normalization. Verified unique across all 3,060 matches. Duplicate check: zero collisions. Recommend hashing to a 16-char key for downstream joins.

**Full artefacts:** `research/bl1/results/task_B_leakage_audit.md`, `research/bl1/dataset/audit_seasons.csv`, `research/bl1/dataset/team_turnover.pkl`, `research/bl1/data/D1_{1617..2526}.csv` (raw copies from archive.org capture 20{yy+1}0601), `research/bl1/dataset/bl1_raw.pkl` (consolidated 3,060 rows).

---

## 4. DEVELOPMENT SPLIT

### Fold specification (canonical)

| Fold | Train (inclusive) | Validate | n_val | Cutoff date |
|---|---|---|---|---|
| 1 | 1617 – 1920 (1,224 m) | **2021** | 306 | 2020-09-18 |
| 2 | 1617 – 2021 (1,530 m) | **2122** | 306 | 2021-08-13 |
| 3 | 1617 – 2122 (1,836 m) | **2223** | 306 | 2022-08-05 |
| 4 | 1617 – 2223 (2,142 m) | **2324** | 306 | 2023-08-18 |
| **Sum** | | | **1,224** | |

- **Strict as-of:** DC refit at each fold with `today = fold_cutoff_date` and phi time-decay. Elo computed cumulatively across the dev+calib timeline with pre-match stamps.
- **2425 role:** **CALIBRATION ONLY.** OOF for 2425 produced with the same as-of pipeline, saved to `oof_2425.csv`. **Not used** for model selection, phi selection, or edge threshold selection. Reserved for the final calibrator fit prior to holdout evaluation.
- **2526 opened for model evaluation:** **NO.** The 306-match holdout is filtered out of every OOF, metric, threshold, and edge sweep in this Phase A. Only schema-level inspection (row count, teams, date range, Pin coverage) was performed — no predictions, no outcome scoring, no calibration.

---

## 5. MODEL RESULTS

### Development OOF (pooled, n=1,224, uncalibrated)

| Model | Brier | LogLoss | ECE (10-bin, min_bin=20) | Notes |
|---|---|---|---|---|
| Uniform (1/3, 1/3, 1/3) | 0.6667 | 1.0986 | — | Reference |
| Empirical base rate (per-fold from train) | 0.6455 | 1.0680 | — | 44.6% home / 25.7% draw / 29.7% away pooled |
| **M1 — Dixon-Coles** | **0.6086** | **1.0165** | 0.026 | phi=0.0012 selected on dev grid |
| **M2 — Elo (K=20, HA=100)** | **0.6189** | **1.0566** | 0.079 | Pre-match ratings from cumulative dev/calib series |
| **Pinnacle closing no-vig** | **0.5795** | — | — | **Benchmark only.** Mean across 4 folds. Not deployable. |
| M3 — DC + LGBM + Europe load | — | — | — | **Deferred.** European-fixture load not yet built (requires CL/EL/UECL fixture-list join per season). |
| M4 — DC + LGBM (no Europe) | — | — | — | **Deferred.** LGBM feature builder scaffolded but not run. Rationale: current BL2 code has L1/L3/L4 leakage; a clean as-of fold-local implementation requires per-fold DC refit + strict feature functions (already isolated in scripts). Prioritised baselines first. |

### Per-fold Brier

| Fold | n | Uniform | Base rate | DC | Elo | Pinnacle close |
|---|---|---|---|---|---|---|
| 2021 | 306 | 0.6667 | 0.6551 | **0.6194** | 0.6291 | 0.5836 |
| 2122 | 306 | 0.6667 | 0.6390 | **0.6208** | 0.6167 | 0.5821 |
| 2223 | 306 | 0.6667 | 0.6377 | **0.5912** | 0.6169 | 0.5939 |
| 2324 | 306 | 0.6667 | 0.6503 | **0.6030** | 0.6129 | 0.5583 |

**Bootstrap CIs (1,000 resamples, match-level):**
- DC uncalibrated Brier CI: [0.591, 0.628]
- DC uncalibrated LogLoss CI: [0.991, 1.045]
- Elo uncalibrated Brier CI: [0.593, 0.648]

### Interpretations

- DC beats uniform by 0.058 absolute Brier and base-rate by 0.037.
- DC beats Elo by 0.010 uncalibrated (folds 2021, 2223, 2324); Elo wins fold 2122 by 0.004.
- **Gap to Pinnacle close = 0.029 absolute Brier** (0.6086 → 0.5795) — the market is ~5% closer to truth than the DC-only model on this baseline configuration.
- **Stability across folds:** DC Brier range [0.591, 0.621], variance is dominated by 2223 outperformance (that year's title race had less parity). No evidence of degradation over time.

---

## 6. CALIBRATION

### Methods compared

Cross-fitted, leave-one-fold-out (fit on 3 folds, evaluate on the 4th; repeat).

| Model | Method | Brier | Brier CI 95% | LogLoss | ECE (10-bin) |
|---|---|---|---|---|---|
| DC | uncalibrated | 0.6086 | [0.591, 0.628] | 1.017 | 0.026 |
| **DC** | **Platt** | **0.6100** | [0.595, 0.625] | 1.019 | **0.014** ← best ECE |
| DC | Isotonic | 0.6119 | [0.594, 0.631] | 1.082 | 0.017 |
| Elo | uncalibrated | 0.6189 | [0.593, 0.648] | 1.057 | 0.079 |
| Elo | Platt | 0.5977 | [0.582, 0.615] | 1.000 | 0.027 |
| **Elo** | **Isotonic** | **0.5957** | [0.577, 0.617] | 1.037 | 0.024 |

### Selected method

- **DC:** **Platt (locked)**. Trade-off: +0.0014 Brier (well within CI overlap) for a 44% relative ECE reduction (0.026 → 0.014). Isotonic degrades both Brier and LogLoss.
- **Elo:** Isotonic (if Elo were the champion). Reduces Brier by 2.3pp; ECE also reduces. But Elo is not the champion candidate — see Section 10.
- **Cross-leakage check:** every calibrator was fit on 3 folds and evaluated on the 4th, so no fold's calibrator saw its own labels. Verified in `20_calibration.py:_evaluate_cross_fitted`.

### 2425 untouched for tuning?

**YES.** Method selection used ONLY the 1,224 development OOF rows across folds 2021–2324. 2425 predictions exist in `oof_2425.csv` but were **not read** for calibration-method selection. 2425 will be used, once, to fit the final calibrator against the eventual holdout — reserved.

---

## 7. EDGE RESEARCH (development OOF only, calibrated DC via cross-fitted Platt)

### Grid

Thresholds evaluated: `{0.02, 0.03, 0.04, 0.05, 0.06, 0.07, 0.08, 0.10}` on `edge = p_model × entry_odds − 1`. Entry odds = **Pinnacle OPENING** (as a proxy for our entry price).

### Full results

| Threshold | Signals | ROI | ROI CI 95% | Avg odds | Avg edge | Closing price edge | Odds CLV | Model-vs-close | Max DD (units) | p̄ (model) | ȳ (realized) | Calibration gap |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 0.02 | 1,653 | −3.57% | [−13.0%, +7.0%] | 5.35 | 0.433 | −0.027 | −0.001 | +0.491 | 80.1 | 0.308 | 0.229 | 0.078 |
| 0.03 | 1,600 | −4.04% | [−13.6%, +7.4%] | 5.41 | 0.446 | −0.027 | −0.001 | +0.505 | 77.5 | 0.307 | 0.226 | 0.082 |
| 0.04 | 1,542 | −4.22% | [−13.6%, +7.0%] | 5.49 | 0.462 | −0.026 | −0.001 | +0.521 | 82.8 | 0.306 | 0.221 | 0.085 |
| 0.05 | 1,467 | −4.72% | [−16.3%, +6.0%] | 5.59 | 0.483 | −0.027 | −0.001 | +0.544 | 82.1 | 0.305 | 0.215 | 0.090 |
| 0.06 | 1,411 | −4.34% | [−16.0%, +6.9%] | 5.67 | 0.500 | −0.027 | −0.001 | +0.562 | 74.0 | 0.304 | 0.213 | 0.091 |
| 0.07 | 1,365 | −4.52% | [−15.7%, +6.6%] | 5.74 | 0.514 | −0.027 | −0.001 | +0.578 | 74.4 | 0.304 | 0.210 | 0.093 |
| 0.08 | 1,306 | −3.93% | [−15.2%, +7.4%] | 5.84 | 0.534 | −0.028 | −0.002 | +0.600 | 75.9 | 0.302 | 0.208 | 0.094 |
| 0.10 | 1,210 | −3.80% | [−16.6%, +9.4%] | 6.01 | 0.569 | −0.029 | −0.002 | +0.638 | 74.4 | 0.300 | 0.203 | 0.097 |

**Metric definitions used (verbatim per CEO errata):**
- ROI = mean flat-unit PnL per bet
- Closing price edge (PRIMARY CLV) = `entry_odds × p_close_no_vig − 1`
- Odds CLV (secondary) = `entry_odds / closing_odds − 1`
- Model-vs-close (diagnostic, NOT CLV) = `p_model / p_close_no_vig − 1`

**Selection dataset:** DEVELOPMENT OOF only (1,224 matches × 3 outcomes = 3,672 candidate signals). 2425 and 2526 excluded.

### Interpretations

- **Every threshold produces negative ROI** on dev OOF. Mean ROI ranges from −3.6% to −4.7%, with 95% bootstrap CIs uniformly straddling zero.
- **8-pp calibration gap on firing subset** (`p̄ − ȳ ≈ 0.078 … 0.097`, growing with threshold). Classic adverse-selection signature: the model fires more readily on outcomes where it is more overconfident.
- **Primary CLV = −0.027** (closing price edge). We are betting at opening prices worse than the closing no-vig mid — market disagrees and generally moves against us.
- **Secondary CLV ≈ 0** — the DC-only model captures essentially none of the opening→closing shift.
- **Diagnostic model-vs-close = +0.49 → +0.64** — our model probabilities are 50–65% *higher* than the closing no-vig probabilities on the outcomes we would bet. This is the mechanistic reason for the negative ROI.
- **Draw share of signals is 33–35%** — inflated by high draw odds (~4.0), which pushes `p × odds` above threshold more easily than home/away odds do. If a final policy proceeds, filtering draws or applying a class-conditional edge threshold merits investigation.

### Provisional threshold

**None recommended for production.** Zero signals is a valid outcome for BL1 given this evidence. Even the most permissive 0.02 threshold does not survive the CLV / calibration-gap sanity checks. **DC-only fails the market-competitiveness test on BL1.**

---

## 8. MARKET BENCHMARK

### Pinnacle availability

- Development seasons 1617–2324: 100% closing-line coverage per season (single row of 1819 missing = 0.33%)
- Calibration season 2425: 100%
- Holdout 2526: **48.7%** in the current archive.org capture (raw CSV from June-2026 snapshot). Two possible explanations: (a) archive capture predates the closing-price update for the season's tail, or (b) Pinnacle's feed to football-data.co.uk stopped mid-season. **Data-quality risk flagged for the eventual holdout CLV evaluation.**

### De-vig candidates

Only **basic normalization** (`1/odds_i / Σ 1/odds_j`) was implemented and used in the primary CLV metric. Not yet compared:
- Shin (`Shin(1993)`) — accounts for insider trading skew
- Log-odds normalization
- Power (Overround)

### Provisional recommended method

**Basic normalization for Phase A.** Rationale: Pinnacle has one of the tightest overrounds in the market (≈2–3%), so basic normalization is a well-understood first-order de-vig. Shin is a candidate improvement to lock before holdout evaluation but is not required to invalidate the DC-only model — the 8-pp calibration gap dominates the ~2% de-vig methodology delta.

**Lock task before holdout open:** compare basic vs Shin on the 2425 slice (calibration season). Choose one. Document. Never re-compare.

---

## 9. PROMOTED TEAMS

### Methods researched

| Hypothesis | Description | Status |
|---|---|---|
| H1 (baseline) | DC cold-start (fallback attack = `log(mean home goals)`, defence = 0) | Measured empirically |
| H2 | League-average prior via `prior_params` warm-start | Not yet run in code — API supports it (`dixon_coles.fit(..., prior_params=...)`) |
| H3 | Suppress first N matches of promoted teams | Match-index analysis empirically supports N ≈ 5–10 |
| H4 | Elo transfer / soft prior | Elo default of 1500 empirically outperforms DC cold-start on promoted matches |
| BL2-strength translation | Suggested previously as "use BL2 DC params, suppress 5" | **Not evaluable** in this session: BL2 `params_latest.pkl` is future-anchored. Would require rebuilding a BL2 as-of DC checkpoint pipeline. **Treated as unvalidated hypothesis** per CEO instruction. |

### Evidence

**Pooled across 4 dev folds, aggregated OOF:**

| Subset | n | DC Brier | Elo Brier | Δ (DC − Elo) |
|---|---|---|---|---|
| All | 1,224 | 0.6086 | 0.6189 | −0.010 |
| Seasoned only (no promoted team) | 960 | **0.6048** | 0.6214 | −0.017 |
| Promoted-match (either side) | 264 | 0.6225 | **0.6099** | +0.013 |
| Promoted home only | 128 | 0.6104 | **0.5908** | +0.020 |
| Promoted away only | 128 | **0.6340** | 0.6390 | −0.005 |

**By promoted-team match index (within their first Bundesliga season):**

| Match idx bin | n | DC Brier |
|---|---|---|
| **1–5 (early season)** | 40 | **0.6698** ← worst |
| 6–10 | 39 | 0.6413 |
| 11–20 | 77 | 0.6002 |
| 21–34 | 108 | 0.6140 |

### Recommendation

Two observations of production interest:

1. **DC's cold start is materially worse than Elo's for promoted-team matches** (0.6225 vs 0.6099, +1.3 pp Brier penalty). Elo's default 1500 rating happens to be near the promoted-team true strength, whereas DC's mean-attack/zero-defence anchor is optimistic.
2. **The penalty is concentrated in matches 1–5** (Brier 0.6698, ~6 pp higher than the seasoned-only baseline). By match 11 the penalty is fully absorbed.

**Provisional recommendation (not production policy):**
- Use Elo predictions for promoted-team matches during matchdays 1–5, then blend or switch to DC as the sample grows. Alternatively, apply H2 (league-average prior_params) at fit time and compare Brier on the same 264-match subset before final selection.
- H3 "suppress first N matches" is defensible as a signal-gate in production (do not fire signals on promoted-team matches in weeks 1–5) rather than a modeling change.

**No production policy decision yet.** All four hypotheses remain candidates. Full run of H2 with `prior_params=league_average` deferred to Phase A continuation.

---

## 10. PROVISIONAL CHAMPION

### Candidate

**None ready to lock as champion.** The DC-only baseline is currently the best implemented model but fails the market-competitiveness test.

**Ranked candidates on current evidence:**

1. **DC + Platt calibration** — Brier 0.6100 (dev OOF), well-calibrated (ECE 0.014), stable across folds. **Cannot beat Pinnacle opening prices** for edge betting on any tested threshold. Viable as a probabilistic model (Brier / calibration) but **not viable as a signal generator alone**.
2. **Elo + isotonic** — Brier 0.5957 (dev OOF). Actually beats DC on Brier once calibrated. Would be attractive as a promoted-team specialist. Not evaluated for edge betting on OOF (would show similar market-competitiveness gap).
3. **M3 / M4 (DC + LGBM ± Europe load)** — Not yet trained. Empirical value undetermined. **This is the next-highest-value experiment.**

### Rationale

- DC's Brier is 2.9 pp behind Pinnacle closing. The market is systematically better priced than the model.
- Calibrated DC still shows an 8-pp calibration gap on firing signals (adverse selection).
- Primary CLV negative on every threshold tested.

### Weaknesses

- LGBM challenger absent from this handoff. Its as-of feature set can plausibly close 1–2 pp of the Brier gap (based on BL2 blend Brier improvement of ~1 pp seen in current production, though that number is confounded by the leakage documented in Section 3). **Cannot lock model spec without M3/M4 evidence.**
- Basic-normalization de-vig used for primary CLV. Shin comparison deferred.
- Promoted-team hypotheses only partially evaluated (H1 measured; H2/H3/H4 sketched but not run through full validation).
- European-fixture load feature construction not built (requires season-by-season European fixture join).

### DC-only still viable?

**Yes as a probabilistic ranker; NO as a stand-alone signal generator on Pinnacle prices.** Recommend keeping DC-only in scope as the "always-available floor" and letting it drop out only if a strong-CLV challenger emerges.

---

## 11. RISKS / BLOCKERS

### P0 — must resolve before champion lock

- **P0-1 (data):** 2526 holdout has only 48.7% Pinnacle closing-odds coverage in the captured CSV. If real football-data.co.uk currently carries the full row set, we may re-fetch when the source is available (503-rate-limited during this session; archive.org used as fallback). If not, holdout CLV evaluation will operate on a reduced subset and needs explicit sample-size handling. **Owner: CEO — decide handling before FINAL HOLDOUT OPEN.**
- **P0-2 (leakage):** The current production BL2 LGBM training pipeline has three confirmed leakage sources (global `params_latest.pkl` load, static market_values.py, static attendance.py) plus a broken Elo lookup. These MUST NOT be reproduced in the BL1 pipeline. Fix is contained: refit DC per fold, exclude market_values/attendance from v1, repair Elo join. **Owner: engineer implementing BL1 training pipeline.**

### P1 — should resolve before production go-live

- **P1-1 (modeling):** M3 / M4 LGBM challengers not yet empirically evaluated. Feature set frozen (see Section 3 recommended list). Walk-forward runner exists in `10_walk_forward_baselines.py` and extends naturally.
- **P1-2 (modeling):** Shin de-vig vs basic-normalization comparison to be run on 2425 slice and locked.
- **P1-3 (modeling):** Promoted-team H2 warm-start not yet run. Elo-blend-for-first-5-matches hypothesis not yet tested end-to-end for its ROI/CLV impact.
- **P1-4 (methodology):** Class-conditional edge threshold (separate thresholds for home / draw / away signals) not investigated. Draw share of firing signals (33–35%) suggests home/away signals may behave differently.
- **P1-5 (data):** European-fixture load requires per-season CL/EL/UECL match feeds. Not yet retrieved.

### Data risks

- Archive.org fallback used for all 10 seasons due to a 503 rate limit on football-data.co.uk during this session. Row-level identity verified against expected 306-match season structure. However, we cannot verify that the archive.org capture *exactly* matches the source's latest revision — most likely a small tail-effect on 2526 closing odds. Recommend re-fetching when the source is available.

### Methodology risks

- Elo `K=20` used out of `elo.py`'s `k_friendly` default because BL1 rows have no `tournament` column. Value plausible for club leagues (Elo canonical K for club is often in [20, 32]) but not empirically tuned for BL1. Small-grid tuning of K deferred.
- Bootstrap CIs use 1,000 resamples throughout — matches CEO errata default. ECE uses match-level distribution, not normal approximation.

---

## 12. HOLDOUT INTEGRITY

**Explicit confirmation.** Season 2526 was NOT used for model, hyperparameter, feature, calibration-method, edge-threshold, staking, or champion selection in this Phase A.

Verified:
- 306 holdout matches filtered out at load time in `10_walk_forward_baselines.py` (see `HOLDOUT_SEASON = "2526"`, `df_dev = df_all[df_all["season"] != HOLDOUT_SEASON]`).
- No OOF predictions produced against 2526.
- No calibrator fit or evaluated on 2526.
- No edge-threshold row evaluated on 2526.
- No promoted-team analysis touches 2526.
- Only schema-level inspection performed on 2526: row count, team roster, date range, Pin coverage.

2526 file exists on disk at `data/cache/fd_D1_2526.pkl` and `research/bl1/data/D1_2526.csv` and is available for the eventual FINAL HOLDOUT OPEN, but has not been read for any model evaluation.

---

## 13. CEO DECISION

**Recommend: CONTINUE BL1 RESEARCH.**

Rationale — three specific reasons to continue rather than lock or investigate:

1. **Champion candidate incomplete.** DC-only baseline is empirically not market-competitive on BL1. LGBM challengers M3 / M4 are the highest-value next experiment and their feature set + walk-forward scaffolding are already in place inside this isolated worktree. Locking a spec without seeing them costs at most 1–2 sessions of compute and shrinks the risk of shipping a DC-only signal generator that has already failed the CLV sanity check.
2. **The market gap is real and well-characterized.** The 8-pp calibration gap on firing signals + 50–65% overprediction vs closing no-vig is a *diagnosable* market-edge deficit, not a random variance issue. It gives us a concrete target for M3/M4: close half the calibration gap, retest CLV. If neither challenger closes it, DC-only + calibration + suppress-promoted becomes a defensible "no-signal until confidence higher" policy.
3. **2526 holdout is intact.** No pressure to open the final holdout early. All Phase A findings are contained to development seasons + 2425 calibration OOF; the sacred season is untouched.

**Do NOT recommend LOCK MODEL SPEC BEFORE CALIBRATION** — we do not have enough evidence to lock, and the current best candidate fails market-competitiveness.

**Do NOT recommend CORRECT** — methodology is clean, findings are consistent with CEO errata, isolation held.

**Do NOT recommend INVESTIGATE** — the questions surfaced (LGBM value, promoted-team policy, Shin de-vig) are all research work-items, not integrity concerns.

Prepared by **CLAUDE**
Session: FLAGSHIP-BL1 Phase A
Isolation: worktree `flagship-bl1-research` @ `worktree-flagship-bl1-research`. No merges. No pushes to origin.
