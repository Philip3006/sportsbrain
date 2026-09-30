# Unified live model lifecycle

This contract separates a stable **algorithm** from a mutable, causally-built
**model state**. It is a technical lifecycle only: it grants neither provider,
publication, betting, nor ledger authority.

## Generic release contract

`src/models/live_model_lifecycle.py` provides these immutable, canonical JSON
objects:

- `TrainingSnapshot`: family, algorithm version/digest, causal training digest,
  cutoff, rows, result watermark and feature-schema digest.
- `ModelRelease`: the state digest built from that snapshot, a parent release
  reference and deterministic release identity.
- `RetrainReceipt`: only `RESULT_WATERMARK_ADVANCED` can create it.
- `ActivationReceipt` and `ActiveModelPointer`: a pointer change is written via
  `os.replace`, keeping the former release reference for rollback.

The only technical state machine is `TRAINING → VALIDATED → ACTIVE`, with an
independent `REJECTED` terminal state. Validation deliberately has no forward
sample-count gate; it verifies causal/provenance invariants only. A failed or
malformed release cannot move the active pointer. Rollback is an explicit new
pointer receipt to a previously immutable release.

## Causal rule

Every family must use results whose safe availability precedes its training
cutoff. The cutoff, watermark, source digest, schema digest and resulting state
digest remain bound in the release. A later result requires a new state/release;
it cannot edit historic predictions or releases.

## Nations League v1.1

`src/analysis/nations_league_live_release.py` adapts the frozen
`nations_league_v1_1` algorithm. It only accepts an exact #229 canonical input
state with `READY` completeness. It seals:

- algorithm digest: the immutable v1.1 model specification;
- training state: current causal training rows, sealed result extension and Elo
  state digest;
- release: a fresh deterministic identity after a valid new result watermark.

The seven INITIAL captures committed before this lifecycle remain byte-for-byte
unchanged. `historical_live_projection()` creates a new public representation
that references each old record ID and digest; it never rewrites it.

`INITIAL` (22–26 hours) and `REFINEMENT` (60–120 minutes) are prediction
presentation stages. A REFINEMENT can supersede the user-facing view while the
INITIAL remains immutable history. They are neither betting instructions nor a
provider authority decision.

`src/notifications/nations_league_live_public.py` is a stable allowlisted
serializer for the live representation. It requires `no_bet=true` and
`betting_authorized=false`. It does not publish anything itself.

## Existing family inventory and boundaries

The machine-readable inventory is
`results/audits/model_lifecycle_inventory_v1.json`.

- Tennis LGBM remains on its existing daily workflow and artifact location.
- Bundesliga 2 Elo/DC remains on its existing daily workflow and artifact
  location.
- Top-5 retains its separate provider/controlled-activation authority chain.
- The World Cup auto-retrain workflow remains disabled.

No existing scheduler is enabled, changed or adopted by this PR. There is no
default runtime pointer path: integration must choose an operator-owned runtime
location through an explicit later activation/runtime change.

## Observability

For every attempted release record the family, algorithm/state digests, causal
cutoff, result watermark, release ID, validation receipt and activation receipt.
Alert on rejected validation, an unavailable pointer, a stale watermark or a
failed atomic pointer write. These signals do not automatically activate,
publish or bet.
