# Top-5 real-shadow session lifecycle

This lifecycle is a durable, NO-BET evidence boundary for real observations.
It is not a scheduler, provider client, publisher, ledger writer, or production
activation path.

## Contract

The session accepts only a provider-neutral `NormalizedProviderObservation`.
For `REAL_OBSERVED`, the observation must carry the exact serialized
Builder-2 `Builder2QualificationReceiptV1` and its canonical observation
envelope. Builder 1 validates that receipt through the shared
`validate_builder1_qualification_receipt()` seam and aligns the local record
to the canonical fixture, provider event/request, observation, normalized
record, cascade, capture, adapter, and qualification identities. Builder 1
cannot issue or recreate the receipt. A copied receipt, a locally constructed
`accepted=true` mapping, TEST_FIXTURE, or OFFLINE_REPLAY evidence fails closed.

Each accepted observation is bound to:

- one of the five registered Top-5 leagues;
- canonical `h2h_1x2` odds and strict fixture identity;
- an explicit caller-supplied signal-time experiment;
- M5 and the frozen Research SHA;
- `REAL_OBSERVED` provenance, distinct from `OFFLINE_REPLAY`.

The prediction is immutable after creation. Repeating the same observation is
idempotent; a conflicting observation, result, or closing attachment fails
closed. Results and closing odds are append-only attachments. Closing odds are
captured for evaluation only and can never enter prediction inputs.

`REAL_OBSERVED` is required for a real session. Deterministic local fixtures
must carry `TEST_FIXTURE` and use the explicit CLI `--fixture-mode`; fixture
sessions require an explicit test output path and cannot be written to the
default runtime store. `TEST_FIXTURE` carries no qualification receipt,
`OFFLINE_REPLAY` is rejected, and unknown modes are rejected.

The state machine is `CREATED -> OBSERVING -> PREDICTIONS_RECORDED ->
AWAITING_RESULTS`, with explicit partial/result/closing states before
`COMPLETE`; empty or unusable input becomes `FAILED_CLOSED`. Persisted status
must agree with the artifacts present and cannot regress. Results may be
`FINAL`, `POSTPONED`, `CANCELLED`, or `ABANDONED`. Final results cannot precede
kickoff, and closing timestamps must remain between signal capture and kickoff.

## Durable artifacts

Session JSON is written through `RealShadowSessionStore` to the external
runtime state namespace `football/top5/shadow_sessions/`. It is not a ledger,
offline replay artifact, or active-checkout file. Resume validates the session
schema, every digest, every identity link, and append-only history before a
new artifact can be written. The session core is immutable across save/resume,
including experiment timing, model/research/integration identity, fixture mode,
and safety flags. Each prediction is bound to that core and the session's
signal-time contract. The manifest contains counts, honest rejected coverage,
provider identities, pending results, safety markers, and a deterministic
session digest. Network request counts and quota cost units remain separate;
zero-network preflight is represented by `network_request_count=0`.

`build_shadow_evidence()` emits Builder 2's `top5-shadow-evidence-v1`
contract, including provider/cascade trace, signal-time, quota-cost, health,
prediction, result, closing, and failure records. The producer validates the
serialized payload with the independent consumer contract before returning it;
it does not import Builder 4's provider implementation.

## CLI seam

The future CLI accepts normalized JSON and explicit timing values:

```text
python3 scripts/top5_real_shadow_session.py \
  --observations normalized.json \
  --session-key shadow-session:example \
  --integration-sha <integration-sha> \
  --experiment-id shadow-experiment:example \
  --created-at 2026-09-16T12:00:00Z \
  --min-lead-minutes 30 \
  --max-lead-minutes 180 \
  --max-odds-age-seconds 300 \
  --kickoff-tolerance-seconds 0 \
  --dry-run
```

No provider call is made by this interface. A real run remains NO-BET,
unpublished, unregistered, and outside the ledger and scheduler.

After Builder-2 qualification, the accepted intake directory can be consumed
without reshaping the observation by hand:

```text
python3 scripts/top5_real_shadow_session.py \
  --b2-intake-dir /absolute/external/b2-evidence/<intake-id> \
  --session-key shadow-session:<run-id> \
  --integration-sha <exact-implementation-sha> \
  --experiment-id shadow-experiment:<id> \
  --created-at 2026-09-16T12:00:00Z \
  --min-lead-minutes <experiment-value> \
  --max-lead-minutes <experiment-value> \
  --max-odds-age-seconds <experiment-value> \
  --kickoff-tolerance-seconds <experiment-value> \
  --output /absolute/external/top5-shadow/session.json \
  --evidence-output /absolute/external/top5-shadow/evidence.json
```

The adapter validates the shared `Builder2QualificationReceiptV1` against the
canonical observation and preserves the exact provider, fixture, request,
capture, qualification, and controlled-run bindings. After result and closing
attachments are complete, the resulting local artifacts are consumed by the
existing audit and measurement commands:

```text
python3 scripts/top5_shadow_audit.py session \
  /absolute/external/top5-shadow/session.json \
  --evidence /absolute/external/top5-shadow/evidence.json \
  --format json > /absolute/external/top5-shadow/audit.json

python3 scripts/top5_shadow_measure.py session \
  /absolute/external/top5-shadow/session.json \
  --evidence /absolute/external/top5-shadow/evidence.json \
  --format json > /absolute/external/top5-shadow/measurement.json
```

## What this does not prove

This lifecycle does not select a provider, validate provider quality by itself,
approve signal-time values, establish shadow performance, authorize a model for
production, or authorize Controlled Activation. It does not access sealed
research data, create bets, publish signals, or write the financial ledger.

## Rollback

The branch is isolated and has no production activation or runtime installation
step. Before merge, discard the branch/PR through normal repository governance.
If a future merge is approved, revert the single lifecycle commit; no ledger
migration or artifact rewrite is required.
