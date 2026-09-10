# BL1 Market-Source Governance (v6)

Purpose: distinguish RESEARCH BENCHMARKS from a CANDIDATE OPERATIONAL MARKET POLICY. Per CEO v6 §7, the operational choice for BL1 production is NOT locked — BL1 has no production scan pipeline, no launchd plist, no signal-time contract, and no reproducible Football-Data bookmaker-average feed in production. The choice made here is a **canonical research baseline** for M5/M6/M7 evaluation, not a production commitment.

## Research benchmarks (reported, never operational)

Every downstream comparison of "how good is the pre-closing market as a probabilistic model?" is reported across all four bookmaker sources. This gives evidence of source-robustness of any research conclusion.

| Source | Dev Brier (chronological earlier-fold selection) | Notes |
|---|---|---|
| Pinnacle pre-closing (`PSH/PSD/PSA`) | 0.5753 – 0.5839 across folds | Best raw Brier in most folds. Single-provider risk. |
| Bookmaker-average pre-closing (`AvgH/AvgD/AvgA`) | 0.5771 – 0.5865 | Market-wide aggregate. |
| Bookmaker-max pre-closing (`MaxH/MaxD/MaxA`) | 0.5769 – 0.5865 | Same rank as avg. |
| Bet365 pre-closing (`B365H/B365D/B365A`) | 0.5745 – 0.5837 | Marginally best on chronological selection. |

Per-fold selection table: `m5_source_selection_by_fold.csv`. Bet365 wins the strictly-earlier-chronological Brier tournament in every outer fold. Difference vs Bookmaker-average is < 0.001 Brier in every fold.

## Canonical research market baseline

**Canonical research baseline: Bookmaker-average pre-closing (`AvgH / AvgD / AvgA`) with basic normalization.**

Implemented in `research/bl1/scripts/canonical_market.py`. Consumed by M5, M6, and M7 through the shared `apply_policy()` entry point so all three market-anchored models evaluate on the same row set with identical missing-market behaviour.

### Multi-criteria justification (research baseline)

| Criterion | Bet365 pre-closing | Bookmaker-avg pre-closing | Verdict |
|---|---|---|---|
| Dev Brier margin | 0.5745 – 0.5837 | 0.5771 – 0.5865 | Bet365 marginally better (< 0.001 Brier) |
| **2526 coverage** (schema-only) | 100% | **100%** | tie |
| **Single-provider risk** | HIGH | LOW (aggregate) | **Avg** |
| Timing consistency | Single provider's snapshot | Cross-provider aggregate | **Avg** |
| Deterministic fallback | Reduces to one feed if that provider fails | Reduces to remaining active providers | **Avg** |
| Reproducibility for RESEARCH | Requires Bet365 access | Uses whatever bookmaker feed is available | **Avg** |
| Holdout safety | Same | Same | tie |

**Decision rule:** the < 0.001 Brier margin does not justify accepting single-provider risk on the research baseline. Bookmaker-average is chosen.

**Anti-selection guarantee:** the baseline cannot be optimized by cherry-picking a source with better dev Brier. Even Pinnacle pre-closing (the actually-best-Brier source in fold 2021) would fail the coverage criterion on 2526 (49.0%).

## What this document does NOT lock

- **Production market source and fallback for BL1 → UNLOCKED.** BL1 is not registered for production. No launchd plist. No scan script. No signal-time contract. No reproducible bookmaker-average feed for BL1 in production. Any future BL1 operational source decision requires:
  - a defined BL1 signal-time contract
  - a reproducible live source at that time
  - validated source/fallback semantics
  - a new source-governance revision
- The M5-vs-closing gap on this DEV sample (matched paired): 0.0025 Brier — closing wins observed sample. This does NOT imply a monotonic Brier trajectory for intermediate T-N prices.
- The M6 alpha choice: v6 chronological OOF selects α ∈ {0.5, 0.7, 0.7, 0.8}. M6 pooled Brier is worse than M5, so the OOF-selected alpha overfits.
- Signal-time contract: see `signal_time_contract.md`. BL1 signal-time is not defined.

## Post-2025-07-23 Pinnacle reliability warning (retained)

Football-Data.co.uk's Pinnacle feed has documented reliability issues since July 2025. 2526 Pinnacle pre-closing coverage: 49.0%. 2526 Pinnacle closing coverage: 48.7%. This warning is orthogonal to the research-baseline decision (which chose bookmaker-avg for multi-provider risk reduction, not because of Pinnacle specifically), but reinforces it.

## De-vig methodology (unchanged)

Basic normalization. Locked in v3 `61_market_hierarchy_dev.py` via dev-only selection. Rationale: Shin over-corrects Bundesliga 1X2 markets (Brier 0.62-0.64); log-odds and power are within 0.0006 Brier of basic; basic wins on interpretability and no free hyperparameter.

## Missing-market policy (v6)

Unified across M5, M6, M7 via `canonical_market.apply_policy()`. If the canonical source is missing for a row, the row is DROPPED from the evaluated set. This is the deterministic shared behaviour that guarantees:
- All three market-anchored models evaluate on the same rows.
- M6 at alpha=1.0 equals M5 exactly (row-set and probability), even under synthetic missing-market rows. Verified by `test_13b_missing_market_row_produces_identical_m5_and_m6_alpha1`.

## Change control

Modification of `canonical_market.py::CANONICAL_PRECLOSE_SOURCE` requires:
1. CEO authorization
2. Full contamination test rerun and PASS
3. Full paired-bootstrap regeneration
4. Downstream edge-sweep + class-asymmetry regeneration
5. New handoff

No change may be justified on marginal dev Brier alone.
