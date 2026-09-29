# Nations League Competition-Context Ablation Contract

Status: prepared; empirical evaluation is blocked until the canonical Builder 4
`NL_COMPETITION_STATE_DATASET_READY` artifact and row-level causal baseline
predictions are available. No synthetic fixture or score is admissible as
evaluation evidence.

## Frozen cohort and provenance

- Consume the versioned Builder 4 dataset as the sole source for historical
  league/group/rule/competition-state fields.
- Verify its schema, complete 512-fixture identity set, unique fixture IDs,
  source and record digests, and each `state_cutoff < kickoff` before joining.
- Join predictions and outcomes by exact canonical fixture identity. Reject
  missing, extra, or duplicate fixtures; do not silently shrink the shared
  historical cohort.
- Retain each B4 `record_digest`; reject absent digests, duplicate fixture
  identities, and any `state_cutoff >= kickoff`.
- Generate the primary causal baseline from the existing offline Nations
  League walk-forward implementation and an explicitly supplied local results
  cache. No network fetch is permitted. Preserve its source digest and strict
  cutoff audit. Builder 1 causal GBT predictions are an optional, separate
  second comparison and are read-only.

## Feature contract

Each pre-match row contains `league_tier`, `group`, `matchday`, home/away
matches played, points, goal difference, home-minus-away points difference,
remaining group matches, and points to qualification/relegation boundaries.
The default model input is explicitly enumerated in
`CONTEXT_NUMERIC_FEATURES` and `CONTEXT_CATEGORICAL_FEATURES`; it includes the
rank interval rather than an invented exact rank, math-consequence flags, and
`league_tier`/`group`. Unavailable values are imputed inside each training fold
with a separate missingness indicator; no validation-fold statistics are used.
Derived flags cover mathematically qualified/eliminated, promoted, and
relegated; whether a win is required, a draw is sufficient, or a loss
eliminates the explicitly declared competition-defined goal; and whether a
fixture is mathematically consequential because possible qualification,
promotion, or relegation-safety status changes by target result. They are
computed from the complete edition-specific group schedule, explicit rules,
and strictly pre-kickoff results. Points-only scenario enumeration treats ties
optimistically for possibility and conservatively for clinched states; it does
not simulate unmodeled future goal differences. Unresolved table ties keep a
rank range/unknown; an exact position is never invented. Subjective motivation
and unsupported near-must-win labels are excluded. No B5 production or
signal-detector hook is allowed.

## Paired causal evaluation

- For each baseline (primary existing causal DC; optional causal GBT), compare
  a baseline-only expanding-window multinomial head with an otherwise identical
  head that additionally sees the point-in-time competition context.
- Report raw causal baseline forecast metrics separately from the paired
  baseline-only head used for the primary ablation comparison.
- For prediction timestamp `t`, training data is strictly earlier than `t`;
  all fixtures at the same kickoff timestamp share one fold and cannot train
  one another. Context fitting, encoding, and scaling are fit inside that fold.
- Only exact shared out-of-sample rows enter either side. Report warm-up and
  prediction coverage against all 512 target fixtures.
- Primary scores: multiclass Brier, multiclass log loss, 10-bin macro
  one-vs-rest ECE, home/draw/away calibration, coverage, and mean maximum
  probability (sharpness).
- Paired differences are context minus baseline. Confidence intervals use a
  seeded paired bootstrap that resamples match dates within historical edition.
- Strata: early/late group stage; mathematically consequential vs low
  constraint; League A/B/C/D (reported as insufficient below 20 fixtures); and
  observed home/draw/away outcome. Strata are descriptive, never substitutes
  for the full-cohort primary result.

## Predeclared interpretation thresholds

- Minimum 100 paired out-of-sample fixtures and at least 80% coverage, otherwise
  `NL_CONTEXT_BLOCKED`.
- A material regression guard is +0.01 multiclass log loss, +0.02 ECE, or +0.03
  absolute calibration gap in any outcome class.
- `NL_CONTEXT_UPLIFT_SUPPORTED` requires lower Brier, a date-cluster 95% CI
  entirely below zero, no calibration/log-loss regression, and the full-cohort
  direction to survive removal of below-20-count categories.
- A lower Brier point estimate that passes the regression guards but lacks
  confidence-interval support and survives small-stratum sensitivity is
  `NL_CONTEXT_PROMISING_NOT_CONFIRMED`. A gain that disappears when a
  below-20-count category is removed is not a global uplift candidate and is
  classified `NL_CONTEXT_NO_GAIN`.
- Non-improvement is `NL_CONTEXT_NO_GAIN`; material scoring/calibration
  regression is `NL_CONTEXT_REGRESSION`.

The thresholds are research decision rules, not production or activation
criteria. Every generated audit and Markdown report will state the exact B4
artifact SHA-256, baseline source digest, cohort match count, and exclusions.
