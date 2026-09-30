# Nations League forward-evidence campaign

`src/analysis/nations_league_forward_campaign.py` is the governance envelope
for accumulating real forward-shadow Nations League evidence. It does not
contact providers, issue authority, activate production, publish, place bets,
or promote a model.

## Campaign contract

`create_forward_campaign(...)` binds one campaign to:

- `UEFA Nations League` and one edition;
- the frozen `nations_league_v1` model digest;
- a UTC campaign start;
- the canonical fixture manifest and its deterministic SHA-256 digest;
- the frozen INITIAL (22–26 hour) and REFINEMENT (60–120 minute) policies;
- `no_bet=true` and `signal_status=SHADOW_ONLY`.

The evaluation contract has its own deterministic digest. Once a prediction is
recorded, `update_forward_campaign(...)` rejects model, policy, or evaluation
contract changes. Predictions and settlements are append-only, and a
settlement remains bound to the prediction's model digest and evidence class.
If a frozen promotion-criteria contract exists, only its digest is bound to the
campaign; the criteria body is never silently replaced.

## Evidence classes and completeness

Each record is explicitly classified as `REAL_OBSERVED` or `SYNTHETIC_ONLY`.
Synthetic records are test/replay material only and are excluded from all real
campaign metrics and counts. A fixture manifest must explicitly state
`initial_eligible` and `refinement_eligible`; an `administrative` or
`cancelled` exception is retained separately and cannot be treated as a missed
capture. The summary reports eligible, captured, missed, settled, unsettled,
and exception counts independently for both lifecycle phases.

Missed windows are not inferred away. A missing capture for an eligible fixture
is a counted miss. No prediction is created by this module to fill a gap.

## Metrics and evidence states

The summary reuses the frozen #227 settlement metrics and exposes separate
`overall`, `INITIAL`, and `REFINEMENT` sections. Each contains sample count,
multiclass Brier score, log loss, calibration/ECE, and secondary accuracy.

Default states are:

- `NO_FORWARD_EVIDENCE` when there are no `REAL_OBSERVED` predictions;
- `FORWARD_EVIDENCE_ACCUMULATING` after real predictions exist, until a
  separately frozen review contract is supplied.

`INSUFFICIENT_FORWARD_EVIDENCE` and `PROMOTION_REVIEW_ELIGIBLE` may only be
provided by an explicitly frozen external criteria evaluation whose digest is
bound to the campaign. The repository
has no default forward promotion threshold, so this module invents none and
never promotes automatically.

The machine-readable summary carries a digest. The operator Markdown view is
derived from that summary and is informational only.

## Real campaign handoff

The intended real flow is:

1. freeze the fixture manifest and create the campaign;
2. append provider-backed predictions at the existing #227 lifecycle gates;
3. append result-safe settlements without rewriting predictions;
4. build the summary and keep INITIAL/REFINEMENT separate;
5. submit the summary for independent human promotion review, if applicable.

The tests use deterministic in-memory records, explicitly marked as
`SYNTHETIC_ONLY` except where a `REAL_OBSERVED` path is exercised for logic
coverage. They are not campaign evidence and are never written as production
forward evidence.
