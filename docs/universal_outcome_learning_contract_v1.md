# Universal outcome-learning contract v1

This document records the first infrastructure slice for the universal
outcome-learning loop. It is provider-neutral, append-only, and independent of
publication, betting, ledger, scheduler, and model activation. It does not
change an existing active model or train a production artifact.

## Current audit baseline

The audit was performed against `origin/main` at
`616742e07`.

| Family | Current result/training reality | Lifecycle risk |
| --- | --- | --- |
| Nations League v1.1 | Result-driven sealed input extension and generic immutable lifecycle are active. Current algorithm is causal Elo, not LightGBM. | Good result/release contract; settlement attachment is still a separate integration slice. |
| Tennis LightGBM | Daily workflow runs `scripts/tennis_train.py`; artifact paths are consumed directly. | No universal pointer/rollback; settlement and recalibration are legacy paths. |
| Tennis calibration | Weekly recalibration from settled history. | No shared immutable release/promotion contract. |
| Bundesliga 2 Elo | Daily retrain workflow writes shared cache artifacts. | No generic pointer/rollback. |
| Bundesliga 2 Dixon-Coles | Daily retrain workflow writes `params_latest`-style artifacts. | No generic pointer/rollback. |
| Bundesliga 2 LightGBM | Trainer exists, but the active retrain workflow does not verify a governed LGBM step. | Inference consumer and promotion path require audit before wiring. |
| Generic/international DC + LGBM + stacker | Legacy auto-retrain workflow is disabled. | Must not be re-enabled blindly; needs adapter and promotion mapping. |
| Top-5 | Candidate/governed B4/B1 acceptance path; retraining is not production-approved. | Settlement/evidence may be added, but authority and activation must remain separate. |

The committed lifecycle registry is authoritative for the current Nations
League active release. The other rows are an inventory, not an authorization
to retrain or activate them.

## Canonical records

The implementation adds four schemas:

### `sportsbrain-authoritative-result-v1`

An adapter-normalized result contains `result_id`, `fixture_id`, `sport`,
`competition`, `source`, `source_record_id`, `completed_at`,
`result_safe_available_at`, `actual_result`, `result_digest`, and
`provenance_digest`. `result_id` is derived from the identity fields; the
actual result and its digest must match. Result safety is strictly after event
completion.

### `sportsbrain-prediction-snapshot-v1`

An immutable pre-result record contains `prediction_id`, `signal_id`, fixture
identity, sport/competition, `model_family`, `model_release_id`, lifecycle
version, phase, prediction timestamp, feature cutoff, prediction payload, and
`prediction_digest`. The contract never rewrites this record after settlement.

### `sportsbrain-outcome-attachment-v1`

An attachment binds one prediction to one authoritative result. It carries
the prediction/model/lifecycle identities, result identity and safe timestamp,
authoritative source, actual result, market and selection, settlement state,
settled timestamp, prediction/result digests, and provenance digest. The
attachment identity is deterministic. Exact replay is a no-op; a second
attachment for the same prediction with different content is rejected.

The common resolver currently covers football 1X2. Sport adapters own
sport-specific market resolution and may be added behind the same attachment
boundary; they must return the same explicit settlement states (`won`,
`lost`, `push`, `void`, or `cancelled`).

### `sportsbrain-causal-training-row-v1`

The result label is separate from signal performance. A row binds a fixture,
sport, competition, training cutoff, feature availability timestamp, event
completion, result-safe timestamp, features, label, result identity, feature
digest, and row digest. It enforces:

* `feature_available_at <= training_cutoff`;
* `event_completed_at < result_safe_available_at <= training_cutoff`; and
* the result is carried as a supervised label, never as a feature.

Deterministic extension returns `NO_OP` when the merged dataset digest is
unchanged and `RETRAIN_REQUIRED` when a new row changes it.

## Persistence and replay

`InMemoryOutcomeStore` is the reference/test implementation. `JsonlOutcomeStore`
is an explicit-path append-only persistence adapter: it uses an OS file lock,
canonical JSON lines, `O_APPEND`, and `fsync`. It validates the complete file
before every append. It has no repository or production default path, so this
slice cannot silently mutate runtime state.

Result identity conflicts, source-record conflicts, malformed records, unknown
schemas, missing referenced results, attachment conflicts, and corrupted
digests fail closed before a write.

## Migration path

No historical file is rewritten by this slice. Migration is staged:

1. Read-only inventory and identity mapping for `signal_history.jsonl`, tennis
   settlement history, Bundesliga 2 history, Nations League prediction stores,
   lifecycle registry artifacts, and Top-5 governed evidence.
2. Adapter-specific importers validate legacy rows and emit canonical
   prediction/result records into a new external append-only store. Invalid or
   ambiguous rows become an explicit migration exception, never a guessed
   result.
3. Backfill outcome attachments with stable legacy identities where available;
   otherwise retain a documented mapping digest before generating a new
   identity.
4. Build causal training rows from pre-cutoff feature snapshots and result
   labels. Closing odds and post-event data remain excluded from inference
   features.
5. Add model-family adapters and digest-driven retrain decisions. Existing
   active artifacts remain the fallback until a candidate passes validation.
6. Only then bridge each adapter to the existing immutable model lifecycle and
   atomic active-pointer transition.

## Implementation slices

1. **Completed in this PR:** common immutable contracts, append-only replay
   semantics, causal row validation, and migration-safety tests.
2. Result-adapter protocol implementations for football, tennis, Nations
   League, Bundesliga 2, and Top-5 evidence without provider transport in the
   common layer.
3. Read-only legacy identity inventory/import preview; no history rewrite.
4. Universal settlement orchestrator with result-safe gating, coverage
   metrics, lock/concurrency policy, and append-only external state.
5. Model-family training adapters, including the missing Bundesliga 2 LGBM
   governance audit before any workflow wiring.
6. Candidate validation/release adapters using the existing lifecycle contract;
   failed training or validation leaves the prior active pointer untouched.
7. Calibration, threshold, CLV, and INITIAL-vs-REFINEMENT diagnostics fed by
   settled signals only, never used as a substitute for core result labels.

## Safety boundary

This slice performs no provider requests, reads no credentials, does not
settle live records, writes no production state, does not retrain models, and
does not change provider authority, activation, publication, betting, ledger,
scheduler, or Cloudflare behavior.
