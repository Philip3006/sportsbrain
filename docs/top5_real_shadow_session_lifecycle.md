# Top-5 real-shadow session lifecycle

This lifecycle is a durable, NO-BET evidence boundary for real observations.
It is not a scheduler, provider client, publisher, ledger writer, or production
activation path.

## Contract

The session accepts only a provider-neutral `NormalizedProviderObservation`.
Builder 4 adapters serialize the observation and an independent accepted
`top5-provider-cascade-validation-*` receipt. The session does not import or
select a provider router. An eligible observation is rejected unless the
receipt explicitly allows prediction input and names the same provider.

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
default runtime store. `OFFLINE_REPLAY` and unknown modes are rejected.

The state machine is `CREATED -> OBSERVING -> PREDICTIONS_RECORDED ->
AWAITING_RESULTS`, with explicit partial/result/closing states before
`COMPLETE`; empty or unusable input becomes `FAILED_CLOSED`. Results may be
`FINAL`, `POSTPONED`, `CANCELLED`, or `ABANDONED`. Final results cannot precede
kickoff, and closing timestamps must remain between signal capture and kickoff.

## Durable artifacts

Session JSON is written through `RealShadowSessionStore` to the external
runtime state namespace `football/top5/shadow_sessions/`. It is not a ledger,
offline replay artifact, or active-checkout file. Resume validates the session
schema, every digest, every identity link, and append-only history before a
new artifact can be written. The manifest contains counts, league coverage,
provider identities, pending results, safety markers, and a deterministic
session digest.

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
