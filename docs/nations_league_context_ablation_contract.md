# UEFA Nations League Safe Competition-Context Ablation

Status: research-only. The runner requires Builder 4's exact
`NL_COMPETITION_STATE_READY` artifact and pinned dataset/coverage digests. It
admits only values whose per-record field status is `SAFE_EXACT` or `SAFE_BOUND`;
`UNRESOLVED` and `NOT_APPLICABLE` values remain unavailable and never become
sporting outcomes. Missing advanced rules do not block this limited safe subset.
No synthetic fixture or prediction is admissible as empirical evidence.

## Frozen source and causality contract

- Bind the canonical B4 competition-state dataset, coverage audit, fixture
  timeline, exact dataset/coverage digests, and exact source commit/PR. Validate
  canonical dataset, coverage, timeline, and per-record digests; require 512
  unique `uefa-nl:` identities, 512 verified kickoff times, and 512 exact
  kickoff cutoffs.
- Validate the READY safe-consumability gate and complete status map on every
  record before projection. A status outside the four-value contract blocks the
  run; only `SAFE_EXACT` and `SAFE_BOUND` permit a value into the feature frame.
- The B4 `state_cutoff` is the target kickoff instant. It is an exclusive
  information boundary: every prior result used in a table must independently
  join to the exact B4 timeline, have `result_safe_available_at < kickoff`, and
  reproduce the stored points, matches, and goal totals. Equal-time, same-day
  unsafe, later, and unresolved administrative results are not used in standings.
- Verify all B4 canonical IDs against the exact fixture timeline. Join the
  local results cache to the B4 cohort by edition, validation period, date,
  home team, and away team; verify scores and the actual neutral flag. Never
  refresh or fetch this cache during evaluation.
- Two B4 records in the current #215 artifact are administratively decided and
  have no played-match result-safe timestamp. Keep both in source-identity and
  provenance accounting, but exclude them from primary scoring and from the DC
  training cache. Report their exact IDs; do not present an award score as a
  played-match prediction target.
- The primary baseline is the existing offline event-level Dixon-Coles
  walk-forward forecast. Each edition/block fit uses competitive results dated
  strictly before that block's first target date. Verify every forecast's
  training maximum date and cutoff against its target fixture.
- The context head uses expanding-window OOS rows; both variants share the same
  historical rows and kickoff folds. Same-kickoff fixtures cannot train one
  another. Scaling, missing-value handling, categories, and model fitting occur
  inside each training fold. Warm-up exclusions remain explicit.
- A certified causal GBT comparison is optional only when current main or an
  independently supplied artifact contains complete, identity-bound causal
  row-level forecasts. Do not substitute a frozen/leaky diagnostic replay.

## Included safe context

Only the following B4-derived or directly computed pre-kickoff fields may enter
the context model:

- League tier and group from the canonical fixture mapping.
- Home/away points and matches played; points difference.
- Goals for, goals against, and goal difference before kickoff.
- Points per game when at least one prior match exists.
- Remaining group matches from the score-free official timeline.
- Points-only unique rank where proven; unresolved points ties remain missing
  for rank bounds and are represented only by an explicit points-tie indicator.
- Promotion/relegation *possibility bounds* only when that participant field is
  per-record field status is `SAFE_BOUND` or `SAFE_EXACT` and the nested state
  explicitly says `points_bounds_only_tiebreaks_preserved_as_unresolved`.
  These are not exact outcome labels. Unresolved or not-applicable fields remain
  missing and receive fold-fitted missingness indicators.

The runner explicitly excludes inferred matchday, official UEFA fixture IDs as
predictors, Article-15 and other unresolved tie-breaks, disciplinary/access-list
rules without source data, unresolved C-League allocation, exact
qualification/promotion/relegation labels not proven by the artifact, exact
must-win/draw-sufficient/loss-eliminates labels, and subjective motivation.
`UNRESOLVED` and `NOT_APPLICABLE` values are explicitly omitted rather than
encoded as categories or false outcomes. These exclusions do not prevent the
safe-subset ablation.

## Predeclared descriptive strata

- Group phase: `early_by_matches_played` when both sides have at most two prior
  matches; `late_by_matches_played` when both have at least four; otherwise
  `middle_by_matches_played`. This is not an inferred UEFA matchday.
- Points gap: `level` (0), `tight_1_to_3` (1–3), or `wide_4_plus` (4+).
- Points-bound constraint: a supported points-bound path is closed for at least
  one fixture participant, no supported bound path is closed, or unavailable.
- League tier, edition, and observed home/draw/away outcome.

Strata with fewer than 20 OOS rows are marked insufficient. They are descriptive
and cannot substitute for the full paired cohort or support a global uplift.

## Metrics and interpretation

Report on identical paired OOS rows: multiclass Brier, multiclass log loss,
10-bin macro one-vs-rest ECE, home/draw/away calibration, coverage, mean maximum
probability (sharpness), and secondary argmax accuracy. Context-minus-baseline
paired date-cluster bootstrap intervals are stratified within edition (95% CI).

`NL_SAFE_CONTEXT_GAIN` requires a lower Brier point estimate, a paired Brier
95% interval entirely below zero, no material log-loss/ECE/class-calibration
regression, sufficient full-cohort coverage, and no gain confined to small
strata. Inconclusive or non-improving Brier is
`NL_SAFE_CONTEXT_NO_CLEAR_GAIN`; material scoring/calibration regression is
`NL_SAFE_CONTEXT_REGRESSION`; failed data, causal, or coverage gates are
`NL_SAFE_CONTEXT_BLOCKED`.

Artifacts are written to `results/audits/nations_league_context_ablation_v1.json`
and `.md`. The JSON binds B4 dataset/coverage/timeline digests, exact B4 source
commit/PR, local result-cache SHA-256, current main SHA, evaluated IDs, warm-up
IDs, excluded administrative IDs, feature contract, and paired predictions'
provenance. The only possible recommendation is research-only inclusion in a
later stacker test. No production, signal detector, activation, publication,
betting, scheduler, or ledger path is changed.

Reproduction requires the runner's `--expected-dataset-digest` and
`--expected-coverage-digest` arguments to match the reviewed B4 READY inputs.
