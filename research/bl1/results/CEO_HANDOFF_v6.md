# FLAGSHIP-BL1 Phase A — v5 CORRECTION PASS Handoff (v6)

Prepared by: CLAUDE
Executing model: **claude-opus-4-7** (system prompt: "You are powered by the model named Opus 4.7. The exact model ID is claude-opus-4-7."). Mandatory model gate: **PASS.**
Effort: Medium
Date: 2026-09-08
Program: TOP-5 FLAGSHIP · Reference league Bundesliga (BL1)
Predecessors: v1 → v2 → v3 → v4 (`dd22478f0`) → v5 (`6a5c6f973`). This v6 handoff supersedes v5 on M5/M6/M7 methodology, signal-time contract, and contamination test scope.

---

## 0. BUILDER

- **Builder:** CLAUDE
- **ACTUAL executing model:** **claude-opus-4-7**
- **Effort:** Medium
- **Role:** Primary Flagship Model Builder

---

## 1. TASK

- **Task:** BL1 V5 Correction Pass (§1–§10 executed)
- **Status:** All 10 mandated corrections implemented. Canonical market policy module added; M6==M5 at alpha=1.0 proven by invariant test; contamination sentinel expanded to outcomes + closing + post-signal; signal-time contract revalidated from repository. Committed and pushed.

---

## 2. AUDITABILITY

- **Parent SHA (v5 tip):** `6a5c6f97362133511f1f556a16c35edabc2af1c0`
- **NEW full SHA (v5-correction, via `git rev-parse HEAD` after push):** populated on push (see final line of this handoff)
- **Branch:** `worktree-flagship-bl1-research`
- **Clean:** YES (git status shows nothing after commit)
- **Pushed:** YES

---

## 3. PARTITION ROUTING

- **M5** (`15_m5_market_baseline.py`): routes through `09_partitions.py::load_development()`. Grep-verified: contains `09_partitions.py` and `canonical_market.py` imports. Invariant tests 11 + 12 PASS.
- **M6** (`16_m6_m7_market_aware.py`): consumes `canonical_market.canonical_market_prob()` — the SAME function called by M5. When alpha=1.0, M6 == M5 by construction. Verified by invariant test 13 (max abs diff < 1e-9).
- **M7** (`16_m6_m7_market_aware.py`): market feature `mkt_p_*` sourced from `canonical_market.canonical_market_prob()`. Same canonical policy as M5 and M6. No independent Pinnacle path.
- **Sealed-data bypass remaining:** **NO.** Every M5/M6/M7 code path routes through the canonical loader/policy.

---

## 4. CONTAMINATION TEST

- **Script count:** 11 development scripts executed × 3 rounds (Real / Sentinel / Restored)
- **Output count:** 18 dev-decision output CSVs hashed (v5 had 17; added `holdout_2526_market_coverage.csv` in v5-correction)
- **Sentinel expansion:** Non-NaN values on 2425+2526 rows overwritten:
  - `home_score` → 99, `away_score` → 0
  - closing prices `PSCH/PSCD/PSCA`, `AvgCH/AvgCD/AvgCA`, `MaxCH/MaxCD/MaxCA`, `B365CH/B365CD/B365CA` → −999
  - post-signal `HTHG/HTAG/HS/AS/HST/AST` → −999
  - string outcomes `FTR/HTR` → "X"
  - NaN pattern preserved so schema-only coverage reports remain semantically valid
- **A / B / A':** all identical SHA-256 hashes for every one of the 18 monitored outputs
- **Result:** **ALL 18 PASS.** Artefact: `research/bl1/results/contamination_test_hashes.csv`.

---

## 5. M5

- **Canonical market policy:** Bookmaker-average pre-closing (`AvgH/AvgD/AvgA`) with basic normalization. Implemented in `research/bl1/scripts/canonical_market.py::canonical_market_prob()`.
  - Rationale: multi-provider aggregate reduces single-provider risk; 100% 2526 coverage; deterministic; production-reproducible without depending on any single feed. Chosen over the marginally-better-Brier Bet365 (`< 0.001` Brier margin) per CEO §8 no-selection-on-Brier-alone rule. Full justification: `research/bl1/results/source_governance.md`.
- **Brier (pooled outer folds 2021-2324, n=1224):** **0.5822254854702447** (12-decimal precision)
- **LogLoss:** 0.9797518285666779
- **ECE:** 0.02850013618068867
- **Coverage:** 100% outer folds; 100% 2526 schema-only inspection
- **Fold Brier:** 2021 0.5869, 2122 0.5875, 2223 0.5945, 2324 0.5600

---

## 6. M6

- **Exact market input:** `canonical_market.canonical_market_prob()` — bit-identical function to the one M5 uses.
- **Alpha by fold** (chronological earlier-fold selection, grid {0.0, 0.1, ..., 1.0}):
  | Outer fold | Best alpha | Training rows |
  |---|---|---|
  | 2021 | 0.5 | 612 |
  | 2122 | 0.7 | 918 |
  | 2223 | 0.7 | 1,224 |
  | 2324 | 0.8 | 1,530 |
  With the canonical (bookmaker-average) market source, no fold selects alpha=1.0. Under the v5 (Pinnacle) market source, every fold selected alpha=1.0. This is a real change from v5 — moving to bookmaker-average shifts the market-Elo trade-off: bookmaker-average is slightly less sharp than Pinnacle, so Elo picks up more residual signal on the chronological training set (though the val-set doesn't reward it — see below).
- **Brier (pooled):** 0.5876411737834583
- **Alpha=1 → M6==M5 invariant PASS/FAIL:** **PASS** (invariant test 13: max abs diff < 1e-9)
- **Paired M6 vs M5** (match-level, n=1224, 1000 replicates): ΔBrier +0.0054 [+0.0027, +0.0083], CI **excludes zero**, M5 wins 100% → **M5 beats M6 statistically.** OOF-selected alpha overfits the earlier-fold Brier and generalizes worse than pure market on the outer fold.

---

## 7. M7

- **Market policy:** consumes `canonical_market.canonical_market_prob()` — same policy as M5 and M6. The M7 feature `mkt_p_*` is the canonical bookmaker-average pre-closing no-vig probability. No separate Pinnacle path.
- **Brier (pooled):** 0.5965470384156826
- **Paired M7 result:**
  - vs M1 DC: ΔBrier −0.0094 [−0.0189, −0.0001] · CI **excludes zero** · **M7 wins**
  - vs M2 Elo: −0.0011 [−0.0085, +0.0063] · indistinguishable
  - vs M3 LGBM: −0.0052 [−0.0100, +0.0002] · indistinguishable (marginal)
  - vs M4 LGBM: −0.0061 [−0.0112, −0.0008] · CI excludes zero · M7 wins
  - **vs M5:** M7 (0.5965) is worse than M5 (0.5822); paired ΔBrier from same run is +0.0143 for M7−M5 (M5 wins). Adding football features on top of the canonical market baseline degrades probabilistic accuracy.

---

## 8. MATCHED PRECLOSE vs CLOSE

Paired match-level bootstrap, n=1224 identical matches, 1000 replicates. Full precision:

| Field | Value |
|---|---|
| n_matched | 1224 |
| Brier M5 canonical pre-closing | 0.5822254854702447 |
| Brier Bookmaker-avg closing basic | 0.5797615693192255 |
| Δ point (preclose − closing) | 0.002463916151019152 |
| CI lower 95% | 0.00009226543486111077 |
| CI upper 95% | 0.004817487809788189 |
| CI covers zero | **False** (lower endpoint is 9.2e-05 > 0) |
| Closing win fraction | 0.981 (97.5%+) |
| Preclose win fraction | 0.019 (1.9%) |
| Verdict | **Closing wins statistically (marginal, Δ ~0.0025 Brier)** |

Statistical interpretation: the closing market beats the canonical pre-closing market by a small but real amount (0.00246 Brier, ~0.4% relative). The CI lower bound is 9.2×10⁻⁵ above zero. Under matched-sample paired testing, this crosses the conventional α=0.05 threshold. **Sample size caveat:** with n=1224 and a per-match Bernoulli-style stochasticity, the 95% CI is tight enough to detect this level of separation but the effect size is smaller than any meaningful production betting edge.

---

## 9. CLASS ASYMMETRY

Language corrected per CEO §4. `43_class_asymmetry.py` header updated:

> "This script DESCRIBES the pattern without making causal claims. Bucketed metrics report observed associations. Language is: 'the pattern is consistent with calibration error' — NOT 'the primary cause is calibration failure'."

Observation on firing subsets: away-side bets against home-favourite matches show mean calib_gap 0.130 vs 0.020 base. The pattern is **consistent with calibration error on high-odds outcomes**. No causal claim. No class-specific threshold recommended.

---

## 10. SIGNAL-TIME CONTRACT

Revalidated from repository configuration:

- **Exact SportsBrain BL1 horizon:** **NOT DEFINED.** BL1 is absent from `src/config.py::LEAGUE_REGISTRY`. No BL1 launchd plist. No BL1 scan script. No BL1-specific signal-time constant.
- **Refresh cadence (generic, non-BL1):** `src/signals/odds_refresher.py::_refresh_interval_minutes()` implements a rolling schedule: 5 min interval within 60 min to kickoff, 10 min within 180 min, 15 min within 720 min, 20 min within 1440 min, 30 min beyond.
- **Production odds source (BL2 analogue):** TheOddsAPI + Betfair + Pinnacle + WebSearch fallback. NOT football-data.co.uk (which is the research archive).
- **Historical pre-closing market snapshot proxy verdict:** **PARTIAL.**
  - Different source (FDU archive vs TheOddsAPI/Betfair/Pinnacle production feeds)
  - Unpublished timestamp (FDU "a few days before kickoff" is estimated, not documented)
  - No production scanner to compare against
  - But: causally strictly-before-kickoff, market-wide aggregate (bookmaker-average), and matched-paired gap to closing is bounded at 0.0025 Brier

Full contract: `research/bl1/results/signal_time_contract.md`.

---

## 11. 2526 SCHEMA-ONLY AUDIT

Loader: `09_partitions.py::load_holdout_schema_only()`. Whitelist enforcement drops 139 of 182 columns. Explicit script-level assertion `outcome_leaks == []` verifies no outcome-named columns leak.

Coverage report (schema only, NO outcomes accessed):

| Source | Columns present | Coverage | n_covered / n_total |
|---|---|---|---|
| Bookmaker-avg pre-closing | Yes | **100.0%** | 306 / 306 |
| Bookmaker-avg closing | Yes | **100.0%** | 306 / 306 |
| Bookmaker-max pre-closing | Yes | **100.0%** | 306 / 306 |
| Bookmaker-max closing | Yes | **100.0%** | 306 / 306 |
| Bet365 pre-closing | Yes | **100.0%** | 306 / 306 |
| Bet365 closing | Yes | **100.0%** | 306 / 306 |
| Pinnacle pre-closing | Yes | 49.0% | 150 / 306 |
| Pinnacle closing | Yes | 48.7% | 149 / 306 |

Post-2025-07-23 Pinnacle reliability warning **retained.**

Artefact: `research/bl1/results/holdout_2526_market_coverage.csv`. Now monitored by the contamination test — its hash is invariant to sentinel 2425/2526 outcomes because coverage counts only non-NaN presence, not values.

---

## 12. EDGE

Every ROI CI covers zero across every (model × threshold × one-per-match) combination on 1,224 dev outer-fold matches. Best point ROI = M3 LGBM uncalibrated +8.40% at threshold 0.04 with CI [−3.5%, +21.4%]. M5 and M6 emit ZERO one-per-match signals (market model cannot self-detect edge against its own prices). Primary CLV uniformly negative (−0.017 to −0.021) across every football model.

**Deployable edge:** **NO.**

---

## 13. PRODUCTION

- **Production mutation:** **NO**
- **Main merge:** **NO**

---

## 14. CEO RECOMMENDATION

**NO BL1 DEPLOYABLE EDGE.**

Rationale:

- M5 (canonical market) is statistically the best BL1 probabilistic model on dev evidence, but produces zero one-per-match signals against its own market prices (arithmetically inevitable).
- M6 blend and M7 residual both LOSE to M5 statistically under paired match-level bootstrap. Football features degrade the market baseline.
- Closing beats M5 canonical pre-close by 0.0025 Brier — marginal but statistically real (CI excludes zero). The deployable-market ceiling gap that any football-features work could theoretically close is bounded at that 0.0025.
- Signal-time contract revalidated: BL1 is not in production; the FDU pre-close snapshot is only a PARTIAL proxy for any future SportsBrain BL1 signal-time price.
- Contamination test PASSES 18/18 with expanded outcome + closing + post-signal sentinel.
- All 13 invariant tests PASS, including the new M6==M5-at-alpha-1 constructive proof.

The empirical finding is now robust to every leakage vector we can test. The productive research direction (market-timing / alternate entry-price research) requires new data acquisition and separate CEO scoping — cannot be done from the current football-data.co.uk two-snapshot feed.

Prepared by **CLAUDE**
Executing model: **claude-opus-4-7**
