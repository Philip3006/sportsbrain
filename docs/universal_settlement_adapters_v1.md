# Universal settlement adapters v1

This layer is an offline evidence boundary. It consumes prediction records and
already authoritative result records; it does not call a provider, publish a
signal, train a model, activate a workflow, or write the betting ledger.

## Stable flow

```text
existing signal/prediction record
  -> src.learning.adapters.prediction_from_record()
  -> PredictionSnapshotV1
  -> AuthoritativeResultV1
  -> src.learning.settlement.settle_predictions()
  -> OutcomeAttachmentV1
  -> optional CausalTrainingRowV1
```

The prediction adapter requires fixture, competition, model, prediction-time,
feature-cutoff, market, and source-record provenance. Missing provenance is
reported as an unconvertible migration record; no value is invented.

Nations League INITIAL and REFINEMENT records become distinct immutable
prediction snapshots. They can reference the same fixture and therefore settle
against the same result, but neither record is overwritten. Tennis uses the
existing pure settlement semantics, including void/cancelled/retired handling.
Bundesliga 2, generic football, and Top-5 candidate records use the same
provider-free football adapter. Top-5 records must remain `no_bet` and cannot
carry production-authority flags.

## Orchestrator and writes

`settle_predictions()` validates the complete in-memory batch first. It rejects
conflicting fixture/result identity, unsafe causality, and attachment binding
errors. It is read-only by default. Persistence requires both an explicitly
constructed append-only store and `write=True`; the CLI has no default store
path and no implicit provider call.

Prediction evidence is captured separately through
`capture_predictions()` and `JsonlPredictionEvidenceStore`. This keeps the
pre-result snapshot immutable and prevents a result-only import from silently
dropping a prediction. It has the same explicit-write and replay rules as the
outcome store.

The CLI is:

```text
python3 scripts/universal_settlement.py
```

With no inputs it performs a migration preview of the known JSON/JSONL source
locations and reports missing or malformed sources without writing. An offline
settlement requires explicit `--prediction-source SOURCE=PATH`, one or more
canonical `--result-jsonl` paths, and `--settled-at`. Add `--write --store
PATH` only when an operator explicitly wants to append validated evidence.

## Migration preview

`preview_jsonl()` reports eligible, convertible, malformed/unconvertible,
already represented, duplicate, and conflicting records. Legacy rows without
model or timestamp provenance remain unconvertible. The preview never creates a
prediction snapshot and never alters the source history.

## Coverage

`build_coverage_report()` reports totals, settlement states, settlement lag,
unresolved prediction age, result conflicts, and deterministic segments by
sport, competition, model family, release, and phase. It is measurement only;
it cannot authorize retraining, provider authority, activation, or publication.
