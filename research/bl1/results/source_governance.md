# BL1 Market-Source Governance

Purpose: distinguish RESEARCH BENCHMARKS from the OPERATIONAL MARKET INPUT. Per CEO §8, the operational choice must NOT be selected merely on marginal dev Brier — it must weight timing meaning, production reproducibility, coverage, reliability, deterministic fallback, and holdout safety.

## Research benchmarks (reported, never operational)

Every downstream comparison of "how good is the pre-closing market as a probabilistic model?" is reported across all four bookmaker sources. This gives evidence of source-robustness of any research conclusion.

| Source | Dev Brier (chronological earlier-fold selection) | Notes |
|---|---|---|
| Pinnacle pre-closing (`PSH/PSD/PSA`) | 0.5753 – 0.5839 across folds | Best raw Brier in most folds. Single-provider risk. |
| Bookmaker-average pre-closing (`AvgH/AvgD/AvgA`) | 0.5771 – 0.5865 | Market-wide aggregate. |
| Bookmaker-max pre-closing (`MaxH/MaxD/MaxA`) | 0.5769 – 0.5865 | Same rank as avg. |
| Bet365 pre-closing (`B365H/B365D/B365A`) | 0.5745 – 0.5837 | Marginally best on chronological selection. |

Per-fold selection table: `m5_source_selection_by_fold.csv`. Bet365 wins the strictly-earlier-chronological Brier tournament in every outer fold. Difference vs Bookmaker-average is < 0.001 Brier in every fold.

## Operational market input (LOCKED — canonical policy)

**Canonical operational source: Bookmaker-average pre-closing (`AvgH / AvgD / AvgA`) with basic normalization.**

Implemented in `research/bl1/scripts/canonical_market.py`. Consumed by M5, M6, and M7. Invariant test 13 verifies M6 output at alpha=1.0 equals M5 output by construction.

### Multi-criteria justification for Bookmaker-average over Bet365

| Criterion | Bet365 pre-closing | Bookmaker-avg pre-closing | Verdict |
|---|---|---|---|
| Dev Brier margin | 0.5745 – 0.5837 | 0.5771 – 0.5865 | Bet365 marginally better (< 0.001 Brier) |
| **2526 coverage** | 100% | **100%** | tie |
| **Single-provider risk** | HIGH (one bookmaker feed) | LOW (aggregate of many) | **Avg wins** |
| Timing consistency | Single provider's snapshot timing | Cross-provider aggregate — less sensitive to a single feed's cadence | **Avg wins** |
| Deterministic fallback | Reduces to Bet365-only if that feed goes down | Reduces to remaining active providers | **Avg wins** |
| Production reproducibility | Requires Bet365 API access | Uses whatever bookmaker feed is available (weighted average) | **Avg wins** |
| Holdout safety | Same for both | Same for both | tie |
| Post-2025-07-23 Pinnacle reliability warning | Not affected | Not affected (avg excludes rows where any single provider is missing but the aggregate handles gracefully) | tie |

**Decision rule:** the < 0.001 Brier margin does not justify accepting single-provider risk on the operational input. Bookmaker-average is chosen.

**Anti-selection guarantee:** the decision cannot be optimized by cherry-picking a source with better dev Brier. Even Pinnacle pre-closing (the actually-best-Brier source in fold 2021) would fail the coverage criterion on 2526 (49.0%).

## Post-2025-07-23 Pinnacle reliability warning (retained)

Football-Data.co.uk's Pinnacle feed has documented reliability issues since July 2025. 2526 Pinnacle pre-closing coverage: 49.0%. 2526 Pinnacle closing coverage: 48.7%. This warning is orthogonal to the canonical-policy decision (which chose bookmaker-avg for multi-provider risk reduction, not because of Pinnacle specifically), but reinforces it.

## De-vig methodology (unchanged from v3)

Basic normalization. Locked in v3 `61_market_hierarchy_dev.py` via dev-only selection. Rationale: Shin over-corrects Bundesliga 1X2 markets (Brier 0.62-0.64); log-odds and power are within 0.0006 Brier of basic; basic wins on interpretability and no free hyperparameter.

## What this document does NOT lock

- The M5-vs-closing gap (matched paired): 0.0025 Brier — closing wins, CI excludes zero.
- The M6 alpha choice: v5-correction chronological OOF selects α ∈ {0.5, 0.7, 0.7, 0.8} across the four outer folds. When the canonical market source is bookmaker-avg (not Pinnacle), Elo adds enough signal to shift α away from 1.0. But M6 pooled Brier (0.5876) is still WORSE than M5 (0.5822), meaning the OOF-selected α overfits.
- The signal-time contract. See `signal_time_contract.md`. Production BL1 is not yet configured — the "operational" designation here is aspirational until BL1 launches with a real signal-time entry.

## Change control

Modification of `canonical_market.py::CANONICAL_PRECLOSE_SOURCE` requires:
1. CEO authorization
2. Full contamination test rerun and PASS
3. Full paired-bootstrap regeneration
4. Downstream edge-sweep + class-asymmetry regeneration
5. New handoff

No change may be justified on marginal dev Brier alone.
